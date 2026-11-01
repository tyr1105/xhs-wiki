"""用 macOS Vision 在本机识别图片文字，供解析包逐张调用。

OCR 是把图片上的文字转换成可搜索文本。这里只返回文字和置信度，不上传图片，
也不把图片路径或系统错误原文带入返回值。Vision、Foundation 和子进程都可注入，
因此格式转换与错误分支可以在不启动真实系统工具的情况下测试。
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile


class _ImageReadError(Exception):
    """标记系统无法读取图片的情况，只有这一类失败允许尝试转换格式。"""


# 桥接库延迟导入：没有 macOS Vision 的环境仍能导入模块并运行替身测试。
def _frameworks(vision, foundation):
    if vision is None:
        import Vision

        vision = Vision
    if foundation is None:
        import Foundation

        foundation = Foundation
    return vision, foundation


# PyObjC 把 Objective-C 的 NSError 输出参数变为返回元组中的第二项。
# 这里只检查错误域和错误码，不读取 localizedDescription，避免文件路径泄漏。
def _read_observations(path: Path, vision, foundation):
    request = vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(vision.VNRequestTextRecognitionLevelAccurate)
    request.setRecognitionLanguages_(["zh-Hans", "en"])
    request.setUsesLanguageCorrection_(True)
    url = foundation.NSURL.fileURLWithPath_(str(path))
    # 本机 PyObjC 12.2.2 把普通空 dict 桥接给 Vision 时可能因缺键抛异常；
    # 原生不可变 NSDictionary 表达真正的空选项，已核对安装模块的实际行为。
    options = foundation.NSDictionary.dictionary()
    handler = vision.VNImageRequestHandler.alloc().initWithURL_options_(url, options)
    succeeded, error = handler.performRequests_error_([request], None)
    if not succeeded:
        readable_errors = {
            vision.VNErrorInvalidFormat,
            vision.VNErrorInvalidImage,
            vision.VNErrorIOError,
        }
        if error is not None and error.domain() == vision.VNErrorDomain:
            if error.code() in readable_errors:
                raise _ImageReadError()
        raise ValueError("ocr_failed")
    return request.results() or []


# Vision 的矩形坐标以左下角为原点；先按顶边从高到低分行，再在同一行按横坐标排序。
# 同一行不同字号的顶边可能略有差异，用中心距离和较小字高判断，避免右侧文字抢先。
def _ordered_lines(observations) -> list[dict]:
    positioned = []
    for observation in observations:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        text = str(candidate.string()).strip()
        if not text:
            continue
        bounds = observation.boundingBox()
        x, y = float(bounds.origin.x), float(bounds.origin.y)
        height = float(bounds.size.height)
        positioned.append({
            "x": x, "top": y + height, "center": y + height / 2,
            "height": height,
            "line": {"text": text, "confidence": float(candidate.confidence())},
        })
    positioned.sort(key=lambda item: (-item["top"], item["x"]))
    rows: list[list[dict]] = []
    for item in positioned:
        if rows:
            anchor = rows[-1][0]
            tolerance = min(anchor["height"], item["height"]) / 2
            if abs(anchor["center"] - item["center"]) <= tolerance:
                rows[-1].append(item)
                continue
        rows.append([item])
    return [item["line"] for row in rows for item in sorted(row, key=lambda item: item["x"])]


# sips 是 macOS 自带的图片转换命令。转换只发生在 0700 临时目录中，生成文件以
# 0600 权限原子改名后再交给 Vision；不覆盖原件，退出上下文即删除全部中间文件。
def _converted_observations(path: Path, vision, foundation, run):
    with tempfile.TemporaryDirectory(prefix="xhs-wiki-ocr-") as name:
        folder = Path(name)
        folder.chmod(0o700)
        descriptor, temporary_name = tempfile.mkstemp(suffix=".png", dir=folder)
        os.close(descriptor)
        temporary = Path(temporary_name)
        converted = folder / "image.png"
        result = run(
            ["sips", "-s", "format", "png", str(path), "--out", str(temporary)],
            check=True, capture_output=True, timeout=60,
        )
        if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
            raise ValueError("ocr_failed")
        temporary.chmod(0o600)
        os.replace(temporary, converted)
        return _read_observations(converted, vision, foundation)


# 默认先让 Vision 直接解码 JPEG、PNG、HEIC 和 WebP；只有明确的图片读取错误才
# 转成 PNG 再识别一次。空白图片是正常结果，不能因文字为空反复转换或重试。
def recognize(image_path: Path | str, *, vision=None, foundation=None, run=None) -> dict:
    try:
        path = Path(image_path).resolve()
        if not path.is_file():
            raise FileNotFoundError()
    except (OSError, TypeError, ValueError, RuntimeError):
        raise ValueError("media_missing") from None
    try:
        vision, foundation = _frameworks(vision, foundation)
        try:
            observations = _read_observations(path, vision, foundation)
        except _ImageReadError:
            observations = _converted_observations(
                path, vision, foundation, subprocess.run if run is None else run,
            )
        lines = _ordered_lines(observations)
        return {"text": "\n".join(line["text"] for line in lines), "lines": lines,
                "engine": "apple-vision"}
    except Exception:
        # 系统和第三方异常可能携带路径或图片内容，只向流水线暴露稳定错误代码。
        raise ValueError("ocr_failed") from None
