"""验证解析层的本机 OCR：系统识别只读代码生成图片，其他分支全部使用替身。

测试素材和转换产物仅位于 pytest 临时目录，不接触个人收藏、浏览器或语音模型。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from xhs_wiki.parse import ocr


# 单个候选模拟 Vision 的字符串和置信度方法，坐标同样采用系统的左下角原点。
class FakeObservation:
    # 保存调用方给出的虚构区域和候选，不依赖任何真实媒体内容。
    def __init__(self, text, x, y, width=0.2, height=0.1, confidence=0.9):
        self.text = text
        self.bounds = SimpleNamespace(
            origin=SimpleNamespace(x=x, y=y), size=SimpleNamespace(width=width, height=height),
        )
        self.confidence = confidence

    # 返回最优候选；None 则模拟没有可读取候选的区域。
    def topCandidates_(self, count):
        assert count == 1
        if self.text is None:
            return []
        return [SimpleNamespace(string=lambda: self.text, confidence=lambda: self.confidence)]

    # 暴露与 CGRect 一样的 origin/size 结构，避免测试依赖真实桥接库。
    def boundingBox(self):
        return self.bounds


# 每次识别由 respond 决定结果，同时记录设置和输入路径，测试不会调用真实系统 API。
def fake_frameworks(respond):
    calls = []
    requests = []

    class Request:
        # 模拟 Objective-C 的分配与初始化两个步骤，便于断言请求配置。
        @classmethod
        def alloc(cls):
            return cls()

        # 每个请求保存独立结果，避免转换重试复用旧结果。
        def init(self):
            self.observations = []
            requests.append(self)
            return self

        # 三个 setter 都只记录输入，不执行真实识别。
        def setRecognitionLevel_(self, level):
            self.level = level

        # 记录语言顺序，以验证中文优先和英文兼容。
        def setRecognitionLanguages_(self, languages):
            self.languages = languages

        # 记录语言校正开关，以验证规定的识别参数。
        def setUsesLanguageCorrection_(self, enabled):
            self.correction = enabled

        # 返回 respond 提供的替身观察对象。
        def results(self):
            return self.observations

    class Handler:
        # 保留真实 handler 的工厂调用形式。
        @classmethod
        def alloc(cls):
            return cls()

        # URL 在替身中就是路径字符串；只在内存中记住路径。
        def initWithURL_options_(self, url, options):
            self.path = Path(url)
            assert options == {}
            return self

        # 用注入回调模拟成功或 NSError 返回元组。
        def performRequests_error_(self, given_requests, error):
            assert error is None
            calls.append(self.path)
            success, result = respond(self.path)
            if success:
                given_requests[0].observations = result
                return True, None
            return False, result

    vision = SimpleNamespace(
        VNRecognizeTextRequest=Request, VNImageRequestHandler=Handler,
        VNRequestTextRecognitionLevelAccurate=0, VNErrorDomain="Vision-test",
        VNErrorInvalidFormat=2, VNErrorIOError=6, VNErrorInvalidImage=13,
    )
    foundation = SimpleNamespace(
        NSURL=SimpleNamespace(fileURLWithPath_=lambda path: path),
        NSDictionary=SimpleNamespace(dictionary=lambda: {}),
    )
    return vision, foundation, calls, requests


# 测试参数不能从个人文件读取；一律在代码中生成一个占位媒体文件。
def generated_file(tmp_path, suffix=".png"):
    path = tmp_path / ("generated image" + suffix)
    path.write_bytes(b"generated test image")
    return path


# 同一行右侧较高的文字仍须排在左侧之后，下方文字须最后出现。
def test_settings_and_reading_order(tmp_path):
    observations = [
        FakeObservation("bottom", 0.1, 0.1),
        FakeObservation("right", 0.6, 0.805, confidence=0.8),
        FakeObservation("left", 0.1, 0.8),
        FakeObservation(None, 0.1, 0.5),
        FakeObservation("  ", 0.1, 0.4),
    ]
    vision, foundation, calls, requests = fake_frameworks(lambda path: (True, observations))
    path = generated_file(tmp_path)
    result = ocr.recognize(path, vision=vision, foundation=foundation)
    assert result == {
        "text": "left\nright\nbottom", "engine": "apple-vision",
        "lines": [{"text": "left", "confidence": 0.9}, {"text": "right", "confidence": 0.8},
                  {"text": "bottom", "confidence": 0.9}],
    }
    assert calls == [path.resolve()]
    assert requests[0].level == 0
    assert requests[0].languages == ["zh-Hans", "en"]
    assert requests[0].correction is True


# 直接可解码时所有任务要求的格式都绕过 sips，空结果也算正常完成。
@pytest.mark.parametrize("suffix", [".heic", ".webp", ".jpeg", ".png"])
def test_direct_read_and_empty_result(tmp_path, suffix):
    vision, foundation, calls, _ = fake_frameworks(lambda path: (True, []))

    def forbidden_run(*args, **kwargs):
        pytest.fail("不应转换可直接读取的图片")

    path = generated_file(tmp_path, suffix)
    assert ocr.recognize(path, vision=vision, foundation=foundation, run=forbidden_run) == {
        "text": "", "lines": [], "engine": "apple-vision",
    }
    assert len(calls) == 1


# 将格式读取失败转换一次；验证 argv 是列表，空格路径独立成项，权限和临时清理正确。
@pytest.mark.parametrize("suffix", [".heic", ".webp"])
def test_sips_conversion_arguments(tmp_path, monkeypatch, suffix):
    monkeypatch.setattr(ocr.tempfile, "tempdir", str(tmp_path))
    error = SimpleNamespace(domain=lambda: "Vision-test", code=lambda: 13)
    path = generated_file(tmp_path, suffix)

    def respond(candidate):
        if candidate == path.resolve():
            return False, error
        assert candidate.stat().st_mode & 0o777 == 0o600
        assert candidate.parent.stat().st_mode & 0o777 == 0o700
        return True, [FakeObservation("converted", 0.1, 0.8)]

    vision, foundation, calls, _ = fake_frameworks(respond)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert isinstance(command, list)
        assert command[:6] == ["sips", "-s", "format", "png", str(path.resolve()), "--out"]
        assert kwargs == {"check": True, "capture_output": True, "timeout": 60}
        Path(command[6]).write_bytes(b"generated converted image")
        return SimpleNamespace(returncode=0)

    result = ocr.recognize(path, vision=vision, foundation=foundation, run=run)
    assert result["text"] == "converted"
    assert len(calls) == 2
    assert len(commands) == 1
    assert not calls[1].exists()
    assert path.read_bytes() == b"generated test image"


# 非读取错误不得触发转换，防止把系统故障误当作图片格式问题反复执行。
def test_non_image_failure_is_sanitized(tmp_path):
    error = SimpleNamespace(domain=lambda: "Vision-test", code=lambda: 9)
    vision, foundation, calls, _ = fake_frameworks(lambda path: (False, error))

    def forbidden_run(*args, **kwargs):
        pytest.fail("非格式错误不能转换")

    with pytest.raises(ValueError, match="^ocr_failed$") as captured:
        ocr.recognize(generated_file(tmp_path), vision=vision, foundation=foundation, run=forbidden_run)
    assert captured.value.__suppress_context__ is True
    assert len(calls) == 1


# 转换失败统一返回错误码，并清理创建的私有临时目录。
@pytest.mark.parametrize("failure", ["exception", "empty", "returncode"])
def test_conversion_failure_is_sanitized(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(ocr.tempfile, "tempdir", str(tmp_path))
    error = SimpleNamespace(domain=lambda: "Vision-test", code=lambda: 2)
    vision, foundation, _, _ = fake_frameworks(lambda path: (False, error))

    def run(command, **kwargs):
        if failure == "exception":
            raise OSError("generated private diagnostic")
        return SimpleNamespace(returncode=1 if failure == "returncode" else 0)

    with pytest.raises(ValueError, match="^ocr_failed$"):
        ocr.recognize(generated_file(tmp_path, ".heic"), vision=vision, foundation=foundation, run=run)
    assert not list(tmp_path.glob("xhs-wiki-ocr-*"))


# 缺原件时在导入 Vision 前失败，非 macOS 上也能得到相同错误码。
def test_missing_image(tmp_path):
    with pytest.raises(ValueError, match="^media_missing$"):
        ocr.recognize(tmp_path / "missing.png")


# 循环符号链接解析会抛 RuntimeError，仍应归入稳定的原件缺失错误码。
def test_image_symlink_loop(tmp_path):
    first, second = tmp_path / "first.png", tmp_path / "second.png"
    first.symlink_to(second)
    second.symlink_to(first)
    with pytest.raises(ValueError, match="^media_missing$"):
        ocr.recognize(first)


# 项目没有依赖 Pillow；装了 PIL 就用它，缺少时改用已经安装的
# 系统 AppKit 生成相同测试图。这个降级只替换画图工具，真实 Vision 仍必须通过。
def generated_text_image(path, text="HELLO 123"):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        import AppKit
        from Foundation import NSString

        bitmap = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, 1000, 180, 8, 4, True, False, AppKit.NSCalibratedRGBColorSpace, 0, 0,
        )
        context = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(bitmap)
        AppKit.NSGraphicsContext.saveGraphicsState()
        try:
            AppKit.NSGraphicsContext.setCurrentContext_(context)
            AppKit.NSColor.whiteColor().set()
            AppKit.NSRectFill(((0, 0), (1000, 180)))
            NSString.stringWithString_(text).drawAtPoint_withAttributes_((30, 40), {
                AppKit.NSFontAttributeName: AppKit.NSFont.boldSystemFontOfSize_(80),
                AppKit.NSForegroundColorAttributeName: AppKit.NSColor.blackColor(),
            })
        finally:
            AppKit.NSGraphicsContext.restoreGraphicsState()
        data = bitmap.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
        path.write_bytes(bytes(data))
        return
    image = Image.new("RGB", (1000, 180), "white")
    font = ImageFont.load_default(size=80)
    ImageDraw.Draw(image).text((30, 30), text, font=font, fill="black")
    image.save(path)


# 这是一条调用真实系统 Vision 的测试；图片现场生成，不下载素材或模型。
def test_real_vision_generated_english(tmp_path):
    pytest.importorskip("Vision")
    path = tmp_path / "generated_english.png"
    generated_text_image(path)
    result = ocr.recognize(path)
    assert "HELLO 123" in result["text"]
    assert result["engine"] == "apple-vision"
    assert all(0 <= line["confidence"] <= 1 for line in result["lines"])
