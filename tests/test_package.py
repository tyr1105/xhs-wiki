"""验证解析包（parse/package.py）与下载器、runner 的交接契约。

本文件只用临时目录、生成的虚构元数据和假的 ASR/OCR；ASR 是自动语音识别，
OCR 是图片文字识别。测试不加载真实模型、不运行 ffmpeg、不访问网络或用户数据。
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import stat
from types import SimpleNamespace

import pytest

from xhs_wiki.parse import package


# 编号、凭证样式和文件内容均由测试代码生成，不借用任何真实收藏。
NOTE_ID = hashlib.sha256(b"x05 generated fictional note").hexdigest()[:24]
RETURN_KEYS = {
    "note_id", "title", "url", "author", "published_at", "package_md",
    "package_sha256", "coverage", "media",
}
HEADINGS = ["标题", "作者", "发布时间", "原链接", "标签", "正文", "语音转写", "图片文字"]


# 即便调用方省略模型目录，配置入口也只能落在 pytest 创建的临时目录里。
@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XHS_WIKI_HOME", str(tmp_path / "app home"))
    monkeypatch.setenv("XHS_WIKI_LOG_DIR", str(tmp_path / "logs"))


# 对齐 download_note 的真实返回结构，同时保留带空格目录以检验路径处理。
def make_download(tmp_path, kind="text", count=None):
    work = tmp_path / "work with spaces" / NOTE_ID
    work.mkdir(parents=True, mode=0o700)
    count = (0 if kind == "text" else 1) if count is None else count
    media = []
    for index in range(count):
        relative = Path("media") / NOTE_ID / f"generated {index}.{'mp4' if kind == 'video' else 'png'}"
        target = work / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        content = f"fictional media {index}".encode()
        target.write_bytes(content)
        media.append({"path": relative.as_posix(), "bytes": len(content),
                      "sha256": hashlib.sha256(content).hexdigest()})
    return work, {
        "status": "ok", "code": None, "media_kind": kind, "media": media,
        "meta": {"title": "虚构标题", "author": "测试作者", "desc": "代码生成的正文。",
                 "tags": "虚构标签", "published_at": "2026-01-02", "type": kind},
    }


class FakeASR:
    """记录提取与转写的先后顺序，模拟临时音轨而不调用任何外部程序。"""

    # 每个对象保有自己的调用记录，不让测试之间共享模型或文件状态。
    def __init__(self, text="生成的转写文本", fail=None):
        self.text = text
        self.fail = fail
        self.calls = []

    # 用字节占位符代表音轨，只验证包模块如何传递路径和清理临时文件。
    def extract_audio(self, video_path, wav_path):
        self.calls.append(("extract", Path(video_path), Path(wav_path)))
        if self.fail == "extract":
            raise RuntimeError("synthetic_private_detail")
        Path(wav_path).write_bytes(b"fictional audio")

    # 严格保留 asr.py 的关键字参数，能发现默认模型或模型目录漏传。
    def transcribe(self, wav_path, *, model, models_dir):
        self.calls.append(("transcribe", Path(wav_path), model, Path(models_dir)))
        assert Path(wav_path).read_bytes() == b"fictional audio"
        if self.fail == "transcribe":
            raise RuntimeError("synthetic_private_detail")
        return {"text": self.text, "segments": [], "model": model, "seconds": 0.0}


# 从包的两个公开文件读取结果，不依赖实现中的私有编码辅助函数。
def read_output(work):
    data = json.loads((work / "package.json").read_text(encoding="utf-8"))
    text = (work / "package.md").read_text(encoding="utf-8")
    return data, text


# 三类产物必须拥有相同的 runner 契约、章节次序、文件摘要和私有权限。
@pytest.mark.parametrize("kind,coverage", [
    ("video", "audio_transcript"), ("images", "images_ocr"), ("text", "text_only"),
])
def test_three_kinds_share_public_contract_and_private_files(tmp_path, kind, coverage):
    work, downloaded = make_download(tmp_path, kind)
    original = deepcopy(downloaded)
    fake_asr = FakeASR()
    ocr_calls = []

    # 每张图片返回可辨认的不同文本，确保处理的是清单指定的原件。
    def recognize(path):
        ocr_calls.append(Path(path))
        return {"text": "生成的图片文字", "lines": [], "engine": "fake-ocr"}

    result = package.build_package(NOTE_ID, work, downloaded, asr=fake_asr, ocr=recognize)
    data, markdown = read_output(work)
    assert set(result) == RETURN_KEYS
    assert result["note_id"] == NOTE_ID
    assert result["title"] == downloaded["meta"]["title"]
    assert result["author"] == downloaded["meta"]["author"]
    assert result["published_at"] == downloaded["meta"]["published_at"]
    assert result["url"] == f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
    assert result["package_md"] == markdown
    assert re.findall(r"^## (.+)$", markdown, flags=re.MULTILINE) == HEADINGS
    assert downloaded["meta"]["desc"] in markdown
    assert downloaded["meta"]["tags"] in markdown
    assert result["coverage"] == data["coverage"] == coverage
    assert result["media"] == data["media"] == downloaded["media"]
    assert "original_media" not in data
    assert data["media_kind"] == kind
    assert data["desc"] == downloaded["meta"]["desc"]
    assert data["type"] == downloaded["meta"]["type"]
    assert result["package_sha256"] == hashlib.sha256((work / "package.json").read_bytes()).hexdigest()
    assert data["package_md_sha256"] == hashlib.sha256((work / "package.md").read_bytes()).hexdigest()
    assert package.load_package(work) == result
    assert downloaded == original
    assert stat.S_IMODE(work.stat().st_mode) == 0o700
    for name in ("package.json", "package.md"):
        assert stat.S_IMODE((work / name).stat().st_mode) == 0o600
    assert {item.name for item in work.iterdir()} == ({"media"} if downloaded["media"] else set()) | {"package.json", "package.md"}
    if kind == "video":
        assert [call[0] for call in fake_asr.calls] == ["extract", "transcribe"]
        assert fake_asr.calls[0][1] == work / downloaded["media"][0]["path"]
        assert fake_asr.calls[1][2:] == ("whisper", tmp_path / "app home" / "models")
        assert not fake_asr.calls[0][2].exists()
        assert data["audio_transcript"] == fake_asr.text
        assert fake_asr.text in markdown
        assert ocr_calls == []
    elif kind == "images":
        assert ocr_calls == [work / downloaded["media"][0]["path"]]
        assert data["images_ocr"] == [{"path": downloaded["media"][0]["path"], "text": "生成的图片文字"}]
        assert fake_asr.calls == []
    else:
        assert fake_asr.calls == []
        assert ocr_calls == []
        assert data["audio_transcript"] == ""
        assert data["images_ocr"] == []


# 快速模式与自定义模型位置必须原样传给可注入的语音接口。
def test_video_accepts_model_choice_and_handles_each_video_in_order(tmp_path):
    work, downloaded = make_download(tmp_path, "video", count=2)
    backend = FakeASR()
    models = tmp_path / "models with spaces"
    result = package.build_package(NOTE_ID, work, downloaded, asr=backend,
                                   models_dir=models, asr_model="sensevoice")
    assert [call[0] for call in backend.calls] == ["extract", "transcribe"] * 2
    assert [call[1] for call in backend.calls[::2]] == [work / item["path"] for item in downloaded["media"]]
    assert all(call[2:] == ("sensevoice", models) for call in backend.calls[1::2])
    assert result["package_md"].count(backend.text) == 2
    assert all(not call[2].exists() for call in backend.calls[::2])


# 空结果也是成功解析；是否值得入库由引擎判断，解析器只如实记录覆盖。
@pytest.mark.parametrize("kind,coverage", [
    ("video", "audio_transcript_empty"), ("images", "images_ocr_empty"),
])
def test_empty_recognition_is_preserved_as_coverage(tmp_path, kind, coverage):
    work, downloaded = make_download(tmp_path, kind)
    result = package.build_package(NOTE_ID, work, downloaded, asr=FakeASR(" \n"),
                                   ocr=lambda path: {"text": " \n", "lines": [], "engine": "fake"})
    assert result["coverage"] == coverage
    assert package.load_package(work) == result


# 混合有字与无字的图文不能丢弃空白图片的记录，也不能标成全部为空。
def test_images_process_each_file_and_keep_empty_results(tmp_path):
    work, downloaded = make_download(tmp_path, "images", count=3)
    answers = iter(["第一张生成文字", "", "第三张生成文字"])
    calls = []

    # 使用对象接口，和直接注入 callable 的测试共同覆盖两个常用替换方式。
    def recognize(path):
        calls.append(Path(path))
        return {"text": next(answers), "lines": [], "engine": "fake"}

    result = package.build_package(NOTE_ID, work, downloaded,
                                   ocr=SimpleNamespace(recognize=recognize))
    data, markdown = read_output(work)
    assert calls == [work / item["path"] for item in downloaded["media"]]
    assert result["coverage"] == "images_ocr"
    assert [item["text"] for item in data["images_ocr"]] == ["第一张生成文字", "", "第三张生成文字"]
    assert markdown.index("第一张生成文字") < markdown.index("第三张生成文字")


# 标题、正文、识别文字都可能携带分享链接，签名不得通过任一输出字段漏出。
@pytest.mark.parametrize("kind", ["video", "images", "text"])
def test_signed_urls_are_removed_from_all_persisted_text(tmp_path, kind):
    work, downloaded = make_download(tmp_path, kind)
    token = hashlib.sha256(b"generated signature for x05 test").hexdigest()
    parameter = "_".join(["xsec", "token"])
    signed = f"https://example.invalid/note?{parameter}={token}#private-fragment"
    downloaded["url"] = signed
    for key in downloaded["meta"]:
        downloaded["meta"][key] = f"生成字段 {signed}"
    downloaded["meta"]["ignored_private_field"] = token
    result = package.build_package(NOTE_ID, work, downloaded, asr=FakeASR(signed),
                                   ocr=lambda path: {"text": signed, "lines": [], "engine": "fake"})
    all_text = (work / "package.json").read_text() + (work / "package.md").read_text() + json.dumps(result)
    assert token not in all_text
    assert parameter not in all_text
    assert "private-fragment" not in all_text
    assert "ignored_private_field" not in all_text
    assert result["url"] == f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
    assert "https://example.invalid/note" in result["package_md"]


# 列表式标签也要保留其文字，但测试不锁死实现选择的标签归一化表示。
def test_list_tags_are_kept_as_text(tmp_path):
    work, downloaded = make_download(tmp_path)
    downloaded["meta"]["tags"] = ["生成标签甲", "生成标签乙"]
    result = package.build_package(NOTE_ID, work, downloaded)
    assert all(tag in result["package_md"] for tag in downloaded["meta"]["tags"])


# 解析失败先于写包：既有的好包不能被失败重跑覆盖，异常不能带第三方细节。
@pytest.mark.parametrize("failure", ["extract", "transcribe", "ocr"])
def test_backend_failures_have_safe_codes_and_preserve_old_package(tmp_path, failure):
    work, original = make_download(tmp_path)
    old = package.build_package(NOTE_ID, work, original)
    media = work / "media" / ("generated.png" if failure == "ocr" else "generated.mp4")
    media.parent.mkdir(mode=0o700)
    media.write_bytes(b"fictional media")
    downloaded = deepcopy(original)
    downloaded["media_kind"] = "images" if failure == "ocr" else "video"
    downloaded["media"] = [{"path": media.relative_to(work).as_posix(), "bytes": media.stat().st_size,
                            "sha256": hashlib.sha256(media.read_bytes()).hexdigest()}]

    # 对外错误码应盖住底层异常，不能把调试细节串进台账或页面。
    def failed_ocr(path):
        raise RuntimeError("synthetic_private_detail")

    code = "ocr_failed" if failure == "ocr" else "asr_failed"
    with pytest.raises(ValueError, match=f"^{code}$") as caught:
        package.build_package(NOTE_ID, work, downloaded, asr=FakeASR(fail=failure), ocr=failed_ocr)
    assert caught.value.__suppress_context__
    assert package.load_package(work) == old
    assert {item.name for item in work.iterdir()} == {"media", "package.json", "package.md"}


# 已下载媒体缺失或内容被替换时，不能用新摘要悄悄接纳它。
@pytest.mark.parametrize("change", ["missing", "bytes", "digest", "empty", "empty_manifest"])
def test_missing_or_changed_media_is_rejected_before_backend(tmp_path, change):
    work, downloaded = make_download(tmp_path, "video")
    target = work / downloaded["media"][0]["path"]
    if change == "missing":
        target.unlink()
    elif change == "bytes":
        downloaded["media"][0]["bytes"] += 1
    elif change == "digest":
        target.write_bytes(b"X" * target.stat().st_size)
    elif change == "empty":
        target.write_bytes(b"")
        downloaded["media"][0].update(bytes=0, sha256=hashlib.sha256(b"").hexdigest())
    else:
        downloaded["media"] = []
    backend = FakeASR()
    with pytest.raises(ValueError, match="^media_missing$"):
        package.build_package(NOTE_ID, work, downloaded, asr=backend)
    assert backend.calls == []
    assert not (work / "package.json").exists()
    assert not (work / "package.md").exists()


# 路径和符号链接边界在打开文件前检查，避免读取工作目录以外的内容。
@pytest.mark.parametrize("escape", ["absolute", "parent", "wrong_root", "file_link", "parent_link"])
def test_media_cannot_escape_its_directory(tmp_path, escape):
    work, downloaded = make_download(tmp_path, "images")
    target = work / downloaded["media"][0]["path"]
    outside = tmp_path / "outside.png"
    outside.write_bytes(target.read_bytes())
    if escape == "absolute":
        downloaded["media"][0]["path"] = str(outside)
    elif escape == "parent":
        downloaded["media"][0]["path"] = "media/../../../outside.png"
    elif escape == "wrong_root":
        downloaded["media"][0]["path"] = "outside.png"
        (work / "outside.png").write_bytes(outside.read_bytes())
    elif escape == "file_link":
        target.unlink()
        target.symlink_to(outside)
    else:
        (work / "media" / "linked").symlink_to(tmp_path, target_is_directory=True)
        downloaded["media"][0]["path"] = "media/linked/outside.png"
    calls = []
    with pytest.raises(ValueError, match="^media_missing$"):
        package.build_package(NOTE_ID, work, downloaded, ocr=lambda path: calls.append(path))
    assert calls == []
    assert outside.read_bytes() == b"fictional media 0"


# JSON 元数据、原件清单和 Markdown 分别受摘要保护，恢复不会默默接受损坏。
@pytest.mark.parametrize("change", ["markdown", "title", "media", "json_syntax", "json_whitespace", "missing_json", "missing_md"])
def test_load_rejects_independent_package_changes(tmp_path, change):
    work, downloaded = make_download(tmp_path, "images")
    package.build_package(NOTE_ID, work, downloaded, ocr=lambda path: {"text": "生成文字"})
    json_path, md_path = work / "package.json", work / "package.md"
    if change == "markdown":
        md_path.write_text(md_path.read_text() + "\nchanged\n")
    elif change in ("title", "media"):
        content = json.loads(json_path.read_text())
        if change == "title":
            content["title"] = "已改变的标题"
        else:
            content["media"][0]["bytes"] += 1
        json_path.write_text(json.dumps(content, ensure_ascii=False))
    elif change == "json_syntax":
        json_path.write_text("{broken")
    elif change == "json_whitespace":
        json_path.write_bytes(json_path.read_bytes() + b"\n")
    elif change == "missing_json":
        json_path.unlink()
    else:
        md_path.unlink()
    with pytest.raises(ValueError, match="^package_changed$"):
        package.load_package(work)


# 已确认入库的原件可被清理，恢复解析包不应再次依赖这些媒体文件。
def test_load_still_works_after_original_media_cleanup(tmp_path):
    work, downloaded = make_download(tmp_path, "video")
    result = package.build_package(NOTE_ID, work, downloaded, asr=FakeASR())
    for item in downloaded["media"]:
        (work / item["path"]).unlink()
    assert package.load_package(work) == result


# 原子写是先完成私有临时文件再替换，观察实际系统调用而非只检查最终权限。
def test_writes_use_private_temporary_files_and_atomic_replacement(tmp_path, monkeypatch):
    work, downloaded = make_download(tmp_path)
    replace = package.os.replace
    replaced = []

    # 替换发生时临时内容已完整，且位于同一目录，避免跨磁盘移动退化成复制。
    def checked_replace(source, destination):
        source, destination = Path(source), Path(destination)
        assert source.parent == destination.parent == work
        assert source != destination
        assert source.read_bytes()
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        replaced.append(destination.name)
        return replace(source, destination)

    monkeypatch.setattr(package.os, "replace", checked_replace)
    result = package.build_package(NOTE_ID, work, downloaded)
    assert sorted(replaced) == ["package.json", "package.md"]
    assert package.load_package(work) == result


# 首次替换失败不破坏旧包，且未发布的临时文件应被清除。
def test_atomic_write_failure_keeps_old_package_without_temporary_files(tmp_path, monkeypatch):
    work, downloaded = make_download(tmp_path)
    result = package.build_package(NOTE_ID, work, downloaded)
    downloaded["meta"]["title"] = "尚未写出的新标题"

    # 使用假的磁盘错误，测试只检查固定错误码，不输出原始路径或诊断字符串。
    def fail_replace(source, destination):
        raise OSError("synthetic_private_detail")

    monkeypatch.setattr(package.os, "replace", fail_replace)
    with pytest.raises(ValueError, match="^package_write_failed$"):
        package.build_package(NOTE_ID, work, downloaded)
    assert package.load_package(work) == result
    assert {item.name for item in work.iterdir()} == {"package.json", "package.md"}


# 两个文件之间中断无法保证成对替换，但恢复必须识别不一致，绝不能入库半份新包。
def test_second_replacement_failure_is_detected_on_resume(tmp_path, monkeypatch):
    work, downloaded = make_download(tmp_path)
    package.build_package(NOTE_ID, work, downloaded)
    old_json = (work / "package.json").read_bytes()
    downloaded["meta"]["title"] = "中断时只发布了新 Markdown"
    replace = package.os.replace

    # 保留真实的第一次替换，精确模拟 JSON 清单发布前磁盘出错。
    def fail_second_replace(source, destination):
        if Path(destination).name == "package.json":
            raise OSError("synthetic_private_detail")
        return replace(source, destination)

    with monkeypatch.context() as patches:
        patches.setattr(package.os, "replace", fail_second_replace)
        with pytest.raises(ValueError, match="^package_write_failed$"):
            package.build_package(NOTE_ID, work, downloaded)
    assert (work / "package.json").read_bytes() == old_json
    assert {item.name for item in work.iterdir()} == {"package.json", "package.md"}
    with pytest.raises(ValueError, match="^package_changed$"):
        package.load_package(work)
    repaired = package.build_package(NOTE_ID, work, downloaded)
    assert package.load_package(work) == repaired


# 不可解析的下载状态不应生成看似成功的纯文字包。
@pytest.mark.parametrize("change", ["failed", "kind", "meta"])
def test_invalid_download_shape_is_rejected(tmp_path, change):
    work, downloaded = make_download(tmp_path)
    if change == "failed":
        downloaded["status"] = "failed"
    elif change == "kind":
        downloaded["media_kind"] = "unknown"
    else:
        downloaded["meta"] = []
    with pytest.raises(ValueError, match="^download_invalid$"):
        package.build_package(NOTE_ID, work, downloaded)
    assert not (work / "package.json").exists()


# 编号会进入规范链接，先校验格式防止路径或任意文本被当成笔记编号。
def test_invalid_note_id_is_rejected(tmp_path):
    work, downloaded = make_download(tmp_path)
    with pytest.raises(ValueError, match="^note_id_invalid$"):
        package.build_package("../generated-invalid", work, downloaded)
