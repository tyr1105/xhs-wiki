"""把下载原件解析成 JSON/Markdown 包，供 runner 入库和中断后恢复。

本模块位于下载与知识库引擎之间：视频调用本机语音转写，图片调用本机
文字识别，纯文字直接整理正文。外部解析器可以注入，原件不会交给引擎。
SHA256 是文件内容的指纹；内部摘要检测文件损坏，返回的 JSON 文件摘要
由 runner 与台账比对，才是跨次运行的独立核对依据。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


_NOTE_ID = re.compile(r"[0-9a-f]{24}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_URL = re.compile(r"https?://[^\s<>\[\]()\"`]+", re.IGNORECASE)
_SIGNED_PARAMETER = re.compile(r"\b(?:xsec_token|xsec_source)=[^\s&<>\[\]()]+", re.IGNORECASE)
_IMAGE_SUFFIXES = {".heic", ".webp", ".jpg", ".jpeg", ".png"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm"}
_RETURN_FIELDS = ("note_id", "title", "url", "author", "published_at", "coverage", "media")
_DOCUMENT_FIELDS = {
    "note_id", "title", "url", "author", "published_at", "media_kind", "type",
    "desc", "tags", "audio_transcript", "images_ocr", "coverage", "media",
    "package_md_sha256", "payload_sha256",
}


# 只用经过格式校验的编号构造公开链接，不沿用下载器的签名地址。
def _canonical_url(note_id: str) -> str:
    if not isinstance(note_id, str) or _NOTE_ID.fullmatch(note_id) is None:
        raise ValueError("note_id_invalid")
    return f"https://www.xiaohongshu.com/explore/{note_id}"


# 正文、标题和识别文字都可能含分享链接；统一删除查询串和片段，避免落盘签名。
def _clean_text(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("download_invalid")
    value = _URL.sub(lambda match: re.split(r"[?#]", match.group(), maxsplit=1)[0], value)
    return _SIGNED_PARAMETER.sub("[已移除]", value).strip()


# 标签在下载器里可能是一段字符串或字符串数组；只保留文字，不序列化任意对象。
def _tags(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("download_invalid")
    return [text for item in value if (text := _clean_text(item))]


# 规范编码忽略字典顺序和缩进，用于内容摘要；整个文件的摘要另按原始字节计算。
def _canonical_bytes(document: dict) -> bytes:
    return json.dumps(document, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


# 固定磁盘编码，恢复时连缩进和空白变化也拒绝；独立可信摘要仍由台账保存。
def _document_bytes(document: dict) -> bytes:
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


# 分块读取原件，避免把最大可达 GiB 的视频一次加载进内存。
def _file_fingerprint(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


# 清单路径始终相对工作目录，拒绝路径逃逸以及容易携带签名的查询串式文件名。
def _relative_media(value: object) -> Path:
    if not isinstance(value, str) or not value or any(char in value for char in "?#\x00\\\n\r"):
        raise ValueError("media_missing")
    path = Path(value)
    if (path.is_absolute() or len(path.parts) < 2 or path.parts[0] != "media"
            or ".." in path.parts or path.as_posix() != value):
        raise ValueError("media_missing")
    return path


# 恢复时也检查清单格式，但不读取原件：它们可能已经按清理规则合法删除。
def _manifest_shape(media: object, kind: str) -> list[dict]:
    if not isinstance(media, list) or kind not in ("video", "images", "text"):
        raise ValueError("media_missing")
    if (kind == "text" and media) or (kind != "text" and not media):
        raise ValueError("media_missing")
    seen: set[str] = set()
    result: list[dict] = []
    for item in media:
        if not isinstance(item, dict):
            raise ValueError("media_missing")
        relative = _relative_media(item.get("path"))
        size, digest = item.get("bytes"), item.get("sha256")
        suffixes = _VIDEO_SUFFIXES if kind == "video" else _IMAGE_SUFFIXES
        if (relative.suffix.lower() not in suffixes or str(relative) in seen
                or type(size) is not int or size <= 0 or not isinstance(digest, str)
                or _SHA256.fullmatch(digest) is None):
            raise ValueError("media_missing")
        seen.add(str(relative))
        result.append({"path": relative.as_posix(), "bytes": size, "sha256": digest})
    return result


# 解析前先核对下载回执，不把被替换的原件重新计算成一份貌似有效的新清单。
def _checked_media(root: Path, media: object, kind: str) -> list[dict]:
    try:
        manifest = _manifest_shape(media, kind)
        for item in manifest:
            relative = Path(item["path"])
            path = root
            for part in relative.parts:
                path = path / part
                if path.is_symlink():
                    raise ValueError("media_missing")
            if not path.is_file() or not path.resolve().is_relative_to(root):
                raise ValueError("media_missing")
            if _file_fingerprint(path) != (item["bytes"], item["sha256"]):
                raise ValueError("media_missing")
        return manifest
    except (OSError, RuntimeError, ValueError):
        raise ValueError("media_missing") from None


# 只调整这条笔记的工作目录及新建父目录，不改变已存在的共享祖先目录权限。
def _private_directory(root: Path) -> None:
    missing: list[Path] = []
    cursor = root
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    root.chmod(0o700)


# ASR 替身与现有 asr.py 保持同一接口；临时音轨用完即删，不列入可清理的原件。
# 转写错误的错误码透传规则：解析失败时控制台的失败列表直接
# 显示这些代码，必须让用户看懂"具体原因"而不是一律 asr_failed——
# - asr.py 报 asr_model_missing（所选语音模型没有安装）→ 原样透传；
# - asr.py 报 audio_extract_failed（抽取音频失败）→ 原样透传；
# - 旧版 asr_audio_extract_failed[:ffmpeg_not_found] 映射保留，兼容旧台账/调用方；
# - 其他错误 → 仍然统一 asr_failed。所有代码都不带原始异常文本。
_ASR_ERROR_CODES = {
    "asr_model_missing": "asr_model_missing",
    "audio_extract_failed": "audio_extract_failed",
    "asr_audio_extract_failed": "audio_extract_failed",
    "asr_audio_extract_failed:ffmpeg_not_found": "audio_extract_failed:ffmpeg_not_found",
}


def _audio_text(root: Path, media: list[dict], backend, models_dir, model: str) -> str:
    try:
        if backend is None:
            from . import asr as backend
        if models_dir is None:
            from .. import config
            models_dir = config.models_dir()
        texts: list[str] = []
        with tempfile.TemporaryDirectory(prefix=".package-audio-", dir=root) as folder:
            for index, item in enumerate(media):
                wav = Path(folder) / f"audio-{index}.wav"
                backend.extract_audio(root / item["path"], wav)
                result = backend.transcribe(wav, model=model, models_dir=Path(models_dir))
                if not isinstance(result, dict) or not isinstance(result.get("text"), str):
                    raise ValueError("asr_failed")
                texts.append(_clean_text(result["text"]))
        return "\n\n".join(text for text in texts if text)
    except Exception as error:
        # 第三方异常可能含签名地址或本机路径，绝不把原异常或异常链传给 runner；
        # 但 asr.py 已经整理好的稳定错误代码（模型未装 / 抽音频失败）按上表
        # 透传，让控制台的失败列表能说明具体原因。
        mapped = _ASR_ERROR_CODES.get(str(error))
        raise ValueError(mapped or "asr_failed") from None


# 每张图片保留一个结果（包括空结果），使引擎能分清哪些图片没有识别到文字。
def _image_text(root: Path, media: list[dict], backend) -> list[dict]:
    try:
        if backend is None:
            from . import ocr as backend
        recognize = backend if callable(backend) else backend.recognize
        results: list[dict] = []
        for item in media:
            result = recognize(root / item["path"])
            if not isinstance(result, dict) or not isinstance(result.get("text"), str):
                raise ValueError("ocr_failed")
            results.append({"path": item["path"], "text": _clean_text(result["text"])})
        return results
    except Exception:
        raise ValueError("ocr_failed") from None


# Whisper 在纯音乐视频上会"幻听"出反复出现的字幕署名。
# 识别门槛：
_HALLUCINATION_PHRASE_LEN = 20   # 短句长度上限（≤ 20 字）
_HALLUCINATION_REPEAT = 5        # 连续重复次数下限（≥ 5 次）
_HALLUCINATION_RATIO = 0.6       # 占全文行数的比例下限（≥ 60%）


def strip_hallucinated_repeats(text: str) -> tuple[str, bool]:
    """识别并删掉 Whisper 幻听出的重复短句，返回 (清理后文本, 是否清空)。

    现象：一条只有背景音乐的视频，被转写出"示例字幕组
    某某"之类的字幕署名反复出现几十遍——这不是说话内容，是模型对纯音乐
    的幻觉输出。判定规则（对清理后的转写逐行看，两个条件都要求这个短句
    总共出现了至少 5 次，避免把只出现一两次的正常短句误删）：
    - 同一个短句（≤ 20 字）连续重复 5 次以上；或者
    - 它占了全部非空行的 60% 以上（且总出现次数 ≥ 5 次），
    满足任一条就把这个短句的所有行删掉。删完为空时返回的第二值为 True，
    调用方（build_package）据此在 coverage 里标记 audio_transcript_
    hallucination，交给笔记卡片去判断内容够不够。

    行的切分依据：asr 的转写文本按分段用空行拼接（见 _audio_text），每个
    分段一行；正常语音不会让同一个 ≤20 字的短句连续占满 5 行以上。
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return "", False
    total = len(lines)
    counted: dict[str, list[int]] = {}
    for index, line in enumerate(lines):
        counted.setdefault(line, []).append(index)
    # 计算每个短句的"最长连续重复段"长度：出现位置有序，逐个比较相邻位置。
    def max_run(positions: list[int]) -> int:
        best = run = 1
        for previous, current in zip(positions, positions[1:]):
            run = run + 1 if current == previous + 1 else 1
            best = max(best, run)
        return best
    hallucinated = {
        phrase for phrase, positions in counted.items()
        if len(phrase) <= _HALLUCINATION_PHRASE_LEN
        and len(positions) >= _HALLUCINATION_REPEAT
        and (max_run(positions) >= _HALLUCINATION_REPEAT
             or len(positions) / total >= _HALLUCINATION_RATIO)
    }
    if not hallucinated:
        return text, False
    kept = [line for line in lines if line not in hallucinated]
    return "\n\n".join(kept), not kept


# 空转写/空识别是有效结果，只如实标注覆盖情况；不在解析层判断内容是否值得入库。
# hallucination=True 表示转写本来非空、但整段都是幻听重复短句、已被清空，
# 单独标记让引擎能区分"没有语音"和"有输出但全是幻觉"。
def _coverage(document: dict, *, hallucination: bool = False) -> str:
    if document["media_kind"] == "video":
        if document["audio_transcript"].strip():
            return "audio_transcript"
        return "audio_transcript_hallucination" if hallucination else "audio_transcript_empty"
    if document["media_kind"] == "images":
        return "images_ocr" + ("" if any(item["text"].strip() for item in document["images_ocr"]) else "_empty")
    return "text_only"


# 章节顺序固定；即使没有该类内容也保留标题，让读取方获得稳定的文本结构。
def _markdown(document: dict) -> str:
    sections = [
        ("标题", document["title"]), ("作者", document["author"]),
        ("发布时间", document["published_at"]), ("原链接", document["url"]),
        ("标签", "\n".join(document["tags"])), ("正文", document["desc"]),
        ("语音转写", document["audio_transcript"]),
        ("图片文字", "\n\n".join(f"### 图片 {index + 1}\n\n{item['text']}"
                                for index, item in enumerate(document["images_ocr"]))),
    ]
    return "\n\n".join(f"## {title}\n\n{text}" for title, text in sections) + "\n"


# 每个文件先写同目录的私有临时文件，再原子替换；JSON 最后发布，作为本次包的清单。
# 两次 replace 不是跨文件事务；中间断电时 load_package 会发现摘要不符并拒绝入库。
def _write_package(root: Path, markdown: bytes, document: bytes) -> None:
    temporary: list[tuple[Path, Path]] = []
    try:
        for name, content in (("package.md", markdown), ("package.json", document)):
            descriptor, filename = tempfile.mkstemp(prefix=".package-", dir=root)
            path = Path(filename)
            temporary.append((path, root / name))
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        for path, target in temporary:
            os.replace(path, target)
    except OSError:
        raise ValueError("package_write_failed") from None
    finally:
        cleanup_failed = False
        for path, _ in temporary:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                cleanup_failed = True
        if cleanup_failed:
            raise ValueError("package_write_failed") from None


# 返回平铺字典，避免 runner 和引擎在旧的 {package: ...} 包装上产生歧义。
def _result(document: dict, markdown: str, json_bytes: bytes) -> dict:
    return {**{key: document[key] for key in _RETURN_FIELDS}, "package_md": markdown,
            "package_sha256": hashlib.sha256(json_bytes).hexdigest()}


def build_package(note_id, dest_dir, download_result, *, asr=None, ocr=None,
                  models_dir=None, asr_model="whisper") -> dict:
    """生成一条笔记的解析包；解析器可注入，默认语音模型 Whisper（取舍见 docs/architecture.md §5）。

    asr 提供 extract_audio 和 transcribe；ocr 是 recognize 函数或带该方法的对象。
    models_dir 省略时，仅视频解析会读取 config 给出的模型目录。
    """
    url = _canonical_url(note_id)
    if (not isinstance(download_result, dict) or download_result.get("status") != "ok"
            or not isinstance(download_result.get("meta"), dict)
            or download_result.get("media_kind") not in ("video", "images", "text")):
        raise ValueError("download_invalid")
    meta, kind = download_result["meta"], download_result["media_kind"]
    document = {"note_id": note_id, "url": url, "media_kind": kind,
                **{key: _clean_text(meta.get(key)) for key in
                   ("title", "author", "published_at", "type", "desc")},
                "tags": _tags(meta.get("tags")), "audio_transcript": "", "images_ocr": []}
    try:
        root = Path(dest_dir)
        if root.is_symlink():
            raise ValueError("media_missing")
        root = root.resolve()
        _private_directory(root)
    except (OSError, RuntimeError):
        raise ValueError("package_write_failed") from None
    document["media"] = _checked_media(root, download_result.get("media"), kind)
    hallucinated = False
    if kind == "video":
        transcript = _audio_text(root, document["media"], asr, models_dir, asr_model)
        # 清理 Whisper 幻听的重复短句：清空时在 coverage 里
        # 标记 audio_transcript_hallucination，交给笔记卡片判断内容够不够。
        transcript, hallucinated = strip_hallucinated_repeats(transcript)
        document["audio_transcript"] = transcript
    elif kind == "images":
        document["images_ocr"] = _image_text(root, document["media"], ocr)
    # 再核对一次，避免解析期间原件变化却仍写出旧下载回执对应的包。
    _checked_media(root, document["media"], kind)
    document["coverage"] = _coverage(document, hallucination=hallucinated)
    markdown = _markdown(document)
    markdown_bytes = markdown.encode("utf-8")
    document["package_md_sha256"] = hashlib.sha256(markdown_bytes).hexdigest()
    document["payload_sha256"] = hashlib.sha256(_canonical_bytes(document)).hexdigest()
    json_bytes = _document_bytes(document)
    _write_package(root, markdown_bytes, json_bytes)
    return _result(document, markdown, json_bytes)


# 恢复时除摘要还验证字段形状，防止不完整旧产物被误当作可直接入库的包。
def _validate_document(document: dict) -> None:
    if not isinstance(document, dict) or set(document) != _DOCUMENT_FIELDS:
        raise ValueError("package_changed")
    if document["url"] != _canonical_url(document["note_id"]):
        raise ValueError("package_changed")
    for key in ("title", "author", "published_at", "type", "desc", "audio_transcript"):
        if not isinstance(document[key], str) or _clean_text(document[key]) != document[key]:
            raise ValueError("package_changed")
    if not isinstance(document["tags"], list) or _tags(document["tags"]) != document["tags"]:
        raise ValueError("package_changed")
    kind = document["media_kind"]
    if _manifest_shape(document["media"], kind) != document["media"]:
        raise ValueError("package_changed")
    images = document["images_ocr"]
    if not isinstance(images, list):
        raise ValueError("package_changed")
    for item in images:
        if (not isinstance(item, dict) or set(item) != {"path", "text"}
                or not isinstance(item["text"], str) or _clean_text(item["text"]) != item["text"]):
            raise ValueError("package_changed")
    if kind == "images":
        if [item["path"] for item in images] != [item["path"] for item in document["media"]]:
            raise ValueError("package_changed")
    elif images:
        raise ValueError("package_changed")
    if kind != "video" and document["audio_transcript"]:
        raise ValueError("package_changed")
    computed = _coverage(document)
    if document["coverage"] != computed:
        # 幻听清空在落盘的文档里只剩空转写，恢复时无法和
        # "本来就没有语音"区分，两种 coverage 都接受（其余必须严格一致）。
        if not (document["coverage"] == "audio_transcript_hallucination"
                and computed == "audio_transcript_empty"):
            raise ValueError("package_changed")


def load_package(dest_dir) -> dict:
    """读取已有包并核对摘要；损坏、缺失或不一致统一报 package_changed。

    内部摘要不能替代台账里的独立摘要：若有人同时重写内容和内部摘要，调用方
    仍须把返回的 package_sha256 与原台账比对。本函数不接触可能已清理的原件。
    """
    try:
        root = Path(dest_dir)
        json_path, md_path = root / "package.json", root / "package.md"
        if root.is_symlink() or any(path.is_symlink() or not path.is_file() for path in (json_path, md_path)):
            raise ValueError("package_changed")
        json_bytes, markdown_bytes = json_path.read_bytes(), md_path.read_bytes()
        document = json.loads(json_bytes)
        _validate_document(document)
        payload = {key: value for key, value in document.items() if key != "payload_sha256"}
        if (hashlib.sha256(_canonical_bytes(payload)).hexdigest() != document["payload_sha256"]
                or hashlib.sha256(markdown_bytes).hexdigest() != document["package_md_sha256"]
                or json_bytes != _document_bytes(document)):
            raise ValueError("package_changed")
        markdown = markdown_bytes.decode("utf-8")
        if markdown != _markdown(document):
            raise ValueError("package_changed")
        return _result(document, markdown, json_bytes)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        raise ValueError("package_changed") from None
