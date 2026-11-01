"""下载模块：给定一条收藏的签名链接，调用 XHS-Downloader 把视频或图片
下载到工作目录，并返回原件清单。

在整体里的位置（见 docs/architecture.md §4）：runner 在"下载"阶段对每条收藏调用本模块的
``download_note``，结果写进台账（成功推进到 downloaded，失败记 failed:download + 错误代码）。

主要做法：
- 通过 ``from source import XHS`` 调用下载组件，并做文件校验、超大文件拦截和失败分类；
- 媒体检查失败时走兜底通道：用普通 GET 直接下载 CDN 地址，再用
  PyAV（在 Python 内调用媒体解码库）校验视频可读，无需外部 ffprobe 程序。

安全约束（红线）：
- 签名链接（带 xsec_token 的 URL）和 XHS 返回的原始数据绝不写进日志、返回值或异常信息；
  ``meta`` 里只保留白名单字段（见 ``_meta_from_data``）。
- 不做任何反检测；站点拒绝就如实返回失败，不绕过。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import shutil
import sys
import urllib.request
from pathlib import Path

# 允许向外暴露的错误代码。任何其他异常都归入 network_error，
# 并且只传代码字符串，不传原始异常文本（原始异常可能包含 URL）。
_ERROR_CODES = {
    "metadata_unavailable",  # 拿不到作品信息
    "media_check_failed",    # 媒体文件数量或可读性检查没通过（GET 兜底也救不回来）
    "file_too_large",        # 单个文件超过上限
    "disk_low",              # 磁盘剩余空间不足
    "network_error",         # 网络/下载器层面的其他失败
}

# 认作"媒体原件"的扩展名。XHS-Downloader 保存图文/视频时用的就是这些后缀。
_MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".mp4", ".mov", ".webm"}


def _digest(path: Path) -> str:
    """计算文件的 sha256（分块读取，避免大文件占内存）。"""
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _default_ffprobe(path: Path) -> bool:
    """默认的视频可读性校验：用 PyAV 读取视频流并实际解码第一帧。

    下载器偶尔会留下字节数正常但内容损坏的视频；只读取宽高无法证明画面可读。
    保留旧函数名和注入点，调用方无需改动，也不依赖系统安装的媒体程序。
    返回 True/False，不抛异常——校验失败本身就是一种可预料的结果。
    """
    try:
        import av

        # 捕获并丢弃底层解码日志，避免坏文件的原始路径或内容进入应用日志。
        # 上下文退出时即关闭容器；只有宽高有效且确实产出画面的流才算可用。
        with av.logging.Capture(), av.open(str(path)) as container:
            for stream in container.streams.video:
                if stream.width > 0 and stream.height > 0:
                    if next(container.decode(stream), None) is not None:
                        return True
        return False
    except Exception:
        # 导入、打开、读头、解码和关闭失败统一视为不可读，不透传第三方异常。
        return False


def _default_http_get(url: str) -> bytes:
    """默认的普通 GET 下载（兜底通道用）。测试会注入假实现，不会走到这里。

    只发一次普通 GET（带 Range 上限，防止一次读进超过上限的字节），不带头部探测——
    实测过：这类 CDN 对 HEAD 返回 404，但普通 GET 正常。
    URL 里带签名参数，只在请求时使用，绝不进日志。
    """
    request = urllib.request.Request(url, headers={"Range": "bytes=0-"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _load_xhs_class(vendor_dir: Path):
    """把 XHS-Downloader 2.8 的目录加进 sys.path，返回它的 XHS 类。

    术语：XHS-Downloader 是第三方开源下载组件（安装时会
    放到 APP_HOME/vendor/XHS-Downloader-2.8）。它提供 ``from source import XHS`` 的
    Python 接口。
    """
    # vendor_dir 可能是 vendor 根目录（APP_HOME/vendor，runner 传的是它），也可能直接是
    # XHS-Downloader-2.8 目录本身；源码包解压后 source 包在子目录里，两种都要能找到
    # （只加 vendor 根目录会报 No module named 'source'）。
    root = Path(vendor_dir)
    candidates = [root] + sorted(root.glob("XHS-Downloader-*"))
    target = next((c for c in candidates if (c / "source" / "__init__.py").is_file()), root)
    path = str(target)
    if path not in sys.path:
        sys.path.insert(0, path)
    from source import XHS
    return XHS


def _meta_from_data(data: dict) -> dict:
    """从 XHS 返回的作品信息里只挑白名单字段。

    为什么做白名单而不是黑名单：XHS 返回的字典里还有点赞数、下载地址等字段，
    下载地址本身带签名参数，绝不能流进日志或返回值，白名单从结构上保证这一点。
    """
    return {
        "title": data.get("作品标题", ""),
        "author": data.get("作者昵称", ""),
        "desc": data.get("作品描述", ""),
        "tags": data.get("作品标签", ""),
        "published_at": data.get("发布时间", ""),
        "type": data.get("作品类型", ""),
    }


def _media_kind(data: dict, file_count: int) -> str:
    """判断媒体类型：视频 / 图文 / 纯文字。

    规则：作品类型写明"视频"就是 video；有下载地址或已下到文件的是 images；
    都没有就是 text（纯文字笔记没有可下载的原件）。
    """
    if "视频" in (data.get("作品类型") or ""):
        return "video"
    if file_count > 0 or data.get("下载地址"):
        return "images"
    return "text"


def _collect_media_files(dest_dir: Path, note_id: str) -> list[Path]:
    """收集本次下载产生的媒体文件（按路径排序，保证结果稳定）。"""
    media_root = dest_dir / "media"
    if not media_root.is_dir():
        return []
    return sorted(
        p for p in media_root.rglob("*")
        if p.is_file() and not p.is_symlink() and p.suffix.lower() in _MEDIA_SUFFIXES
    )


def _direct_fallback(urls: list, dest_dir: Path, note_id: str, *, http_get, ffprobe,
                     max_file_bytes: int, free_bytes) -> Path | None:
    """普通 GET 兜底下载。

    背景：部分视频的 CDN 地址（对 HEAD 请求返回 404）会让下载器在"查大小"一步就放弃，
    目录里没有视频文件。这里用普通 GET 把同一个地址直接拉下来，再用视频检查注入点校验。

    保守限制：只处理"恰好一个视频地址"的情形；图文多地址的布局
    没有验证过，直接按 media_check_failed 上报，不猜测文件名。

    返回下载好的文件路径；任何一步不行就返回 None（调用方记 media_check_failed）。
    下载中途的半截文件写在 .part 临时文件里，失败即删，不会污染 media 目录。
    """
    if len(urls) != 1 or not str(urls[0]).startswith(("http://", "https://")):
        return None
    if free_bytes() < max_file_bytes:
        # 兜底下载前也检查磁盘：至少要放得下上限大小的文件。
        raise ValueError("disk_low")
    try:
        data = http_get(str(urls[0]))
    except Exception:
        # GET 本身失败（网络错误等）：按原失败类别 media_check_failed 上报。
        return None
    if not data:
        return None
    if len(data) > max_file_bytes:
        raise ValueError("file_too_large")
    folder = dest_dir / "media" / note_id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{note_id}.mp4"
    part = folder / f"{note_id}.mp4.part"
    try:
        # 先写临时文件再改名（项目规范）；私有文件权限 0600。
        with part.open("wb") as stream:
            stream.write(data)
        part.chmod(0o600)
        if not ffprobe(part):
            return None
        part.replace(target)
        return target
    except Exception:
        return None
    finally:
        # 无论成败，都不留下 .part 半截文件。
        part.unlink(missing_ok=True)


async def _download_note_async(note_id, signed_url, dest_dir, *, vendor_dir, xhs_factory,
                               http_get, ffprobe, min_free_bytes, max_file_bytes) -> dict:
    """download_note 的异步主体：XHS-Downloader 的接口是 async 的。"""

    def free_bytes() -> int:
        return shutil.disk_usage(dest_dir).free

    def fail(code: str, media_kind: str = "text", meta: dict | None = None) -> dict:
        # 失败时清掉本次下载的半截文件：整个 media/<note_id>/ 都是这一条笔记的，
        # 删掉不影响别的笔记；删除是尽力而为，失败也不掩盖真正的错误代码。
        shutil.rmtree(dest_dir / "media" / note_id, ignore_errors=True)
        return {"status": "failed", "code": code, "media_kind": media_kind,
                "media": [], "meta": meta or {}}

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_dir.chmod(0o700)
    except OSError:
        return fail("disk_low")

    # 第一步：磁盘余量检查。剩余低于 min_free_bytes（默认 10 GiB）直接放弃下载。
    if free_bytes() < min_free_bytes:
        return fail("disk_low")

    def guard_progress(event: dict) -> None:
        """下载进度回调（XHS-Downloader 会在写盘过程中反复调用）。

        做两件事：单个文件超过 max_file_bytes 就中止；磁盘余量跌破下限就中止。
        主动抛 ValueError 是让上游下载器立刻停手的唯一途径。
        """
        total = max(0, event.get("total_bytes") or 0)
        completed = max(0, event.get("completed_bytes") or 0)
        if max(total, completed) > max_file_bytes:
            raise ValueError("file_too_large")
        if free_bytes() < min_free_bytes:
            raise ValueError("disk_low")

    # 第二步：构造 XHS 下载器。参数要点：
    # 作品文件存到 <work_path>/media/<作品ID>/，不按作者分档、不存 md、不下载封面。
    if xhs_factory is None:
        XHS = _load_xhs_class(Path(vendor_dir))

        def xhs_factory():
            return XHS(
                work_path=str(dest_dir), folder_name="media", name_format="作品ID",
                folder_mode=True, author_archive=False, record_data=False,
                note_format="", image_format="AUTO", video_cover_download=False,
                live_download=False, max_retry=1, timeout=30, script_server=False,
            )

    data = None
    try:
        # 下载器会往 stdout 打印进度；签名链接可能出现在它的输出里，整体重定向丢弃。
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            async with xhs_factory() as xhs:
                # 上游默认关闭证书校验；这里为两个会话显式开启。
                for client_name in ("request_client", "download_client"):
                    client = getattr(getattr(xhs, "manager", None), client_name, None)
                    if client is not None:
                        client.verify = True
                result = await xhs.extract(
                    signed_url, download=True, check_record=False,
                    progress_callback=guard_progress,
                )
        # 只认作品 ID 匹配的那一条，避免把同页其他作品的信息带出去。
        data = next((d for d in (result or []) if d.get("作品ID") == note_id), None)
        if not data:
            return fail("metadata_unavailable")
    except ValueError as error:
        # 自己人抛的错误代码原样保留；其他 ValueError 归入 network_error（不透传文本）。
        code = str(error) if str(error) in _ERROR_CODES else "network_error"
        return fail(code)
    except Exception:
        # 网络失败、下载器内部错误等：只报代码，异常文本里可能有签名链接。
        return fail("network_error")

    meta = _meta_from_data(data)
    kind_hint = _media_kind(data, 0)

    # 第三步：核对下载结果。期望的文件数 = XHS 给出的下载地址数。
    expected = len(data.get("下载地址") or [])
    files = _collect_media_files(dest_dir, note_id)
    # 落盘文件本身超过上限：直接判 file_too_large，不再尝试兜底（兜底只会下到同样的超大文件）。
    if any(p.stat().st_size > max_file_bytes for p in files):
        return fail("file_too_large", kind_hint, meta)
    # 纯文字笔记：没有下载地址也没有文件，本身就是完整的结果；
    # 有媒体的笔记：文件数必须和地址数一致、非空、不超限，视频还要通过解码检查。
    valid = expected == 0 and len(files) == 0 or expected > 0 and len(files) == expected and all(
        p.stat().st_size > 0 and p.stat().st_size <= max_file_bytes for p in files
    ) and all(ffprobe(p) for p in files if p.suffix.lower() in {".mp4", ".mov", ".webm"})

    if not valid:
        # 第四步：媒体检查失败 → 尝试普通 GET 兜底（仅单个视频地址的情形）。
        try:
            fallback = _direct_fallback(
                data.get("下载地址") or [], dest_dir, note_id,
                http_get=http_get, ffprobe=ffprobe,
                max_file_bytes=max_file_bytes, free_bytes=free_bytes,
            )
        except ValueError as error:
            return fail(str(error), kind_hint, meta)
        if fallback is None:
            return fail("media_check_failed", kind_hint, meta)
        files = [fallback]

    # 收尾前再看一次磁盘和文件大小（兜底通道已查过，这里统一兜住）。
    for p in files:
        if p.stat().st_size > max_file_bytes:
            return fail("file_too_large", kind_hint, meta)
    if free_bytes() < min_free_bytes:
        return fail("disk_low", kind_hint, meta)

    return {
        "status": "ok",
        "code": None,
        "media_kind": _media_kind(data, len(files)),
        "media": [
            {"path": str(p.relative_to(dest_dir)), "bytes": p.stat().st_size,
             "sha256": _digest(p)}
            for p in files
        ],
        "meta": meta,
    }


def download_note(note_id: str, signed_url: str, dest_dir, *, vendor_dir=None,
                  xhs_factory=None, http_get=None, ffprobe=None,
                  min_free_bytes: int = 10 * 1024 ** 3,
                  max_file_bytes: int = 1024 ** 3) -> dict:
    """下载一条收藏的视频或图片。

    参数：
    - note_id：24 位十六进制的笔记 ID（台账里的主键）；
    - signed_url：带 xsec_token 的签名链接，只在调用下载器时使用，不进任何输出；
    - dest_dir：这条笔记的工作目录（APP_HOME/work/<note_id>/），媒体落在 media/ 下；
    - vendor_dir：XHS-Downloader 2.8 的目录（默认 None 时必须提供 xhs_factory）；
    - xhs_factory / http_get / ffprobe：注入点，测试时换成假替身，不访问网络。

    返回：
    ``{"status": "ok"|"failed", "code": None|错误代码, "media_kind": "video"|"images"|"text",
       "media": [{"path", "bytes", "sha256"}], "meta": {title, author, desc, tags,
       published_at, type}}``。失败时 media 为空列表、meta 尽力保留白名单字段。
    """
    # XHS-Downloader 的接口是协程，这里用 asyncio.run 驱动；本模块的调用方（runner）
    # 是同步代码，不需要感知 asyncio。
    return asyncio.run(_download_note_async(
        note_id, signed_url, Path(dest_dir),
        vendor_dir=vendor_dir, xhs_factory=xhs_factory,
        http_get=http_get or _default_http_get,
        ffprobe=ffprobe or _default_ffprobe,
        min_free_bytes=min_free_bytes, max_file_bytes=max_file_bytes,
    ))
