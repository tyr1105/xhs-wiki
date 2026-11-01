"""下载模块（download.py）的测试：不访问网络、不启动真实下载器。

做法：注入假的 xhs_factory（一个异步上下文管理器，extract 会按场景在工作目录里
写文件或抛异常）、假的 http_get 和假的 ffprobe。笔记 ID 和签名链接都用代码里
生成的虚构数据。默认视频校验另用临时生成的画面和正弦波音轨验证真实解码。
"""

from contextlib import contextmanager
from fractions import Fraction
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import av
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from xhs_wiki.download import _default_ffprobe, download_note


def make_note_id(seed: int = 1) -> str:
    """生成虚构的 24 位十六进制笔记 ID。"""
    return "%024x" % seed


def make_signed_url(note_id: str) -> str:
    """生成虚构的签名链接（token 是测试专用的假值，仅用于验证"不外泄"）。"""
    return f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=FAKE{note_id}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeXHS:
    """XHS-Downloader 的假替身：behavior 是一个函数，负责写文件/抛异常并返回作品信息列表。"""

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def extract(self, url, download=False, check_record=True,
                      progress_callback=None, **kwargs):
        self.calls.append(url)
        return self.behavior(progress_callback)


def write_media(dest_dir: Path, note_id: str, name: str, data: bytes) -> Path:
    """模拟下载器落盘：写到 <dest_dir>/media/<note_id>/<name>。"""
    folder = dest_dir / "media" / note_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


def note_data(note_id: str, *, kind="视频", urls=None, title="测试标题", author="测试作者"):
    """构造 XHS 返回形状的虚构作品信息（中文键名与上游保持一致）。"""
    return {
        "作品ID": note_id, "作品类型": kind, "作品标题": title, "作者昵称": author,
        "作品描述": "测试描述", "作品标签": "测试", "发布时间": "2026-01-01 00:00:00",
        "下载地址": urls if urls is not None else [f"https://cdn.example.test/{note_id}.mp4"],
        "点赞数量": 1,  # 白名单之外的字段，不应出现在 meta 里
    }


def run(note_id, behavior, dest_dir, **kwargs):
    """统一入口：用假下载器跑一次 download_note。"""
    factory = kwargs.pop("xhs_factory", None) or (lambda: FakeXHS(behavior))
    return download_note(note_id, make_signed_url(note_id), dest_dir,
                         vendor_dir=None, xhs_factory=factory, **kwargs)


def test_video_download_ok(tmp_path):
    """视频下载成功：文件、sha256、meta 都正确。"""
    nid = make_note_id(1)
    payload = b"fake-video-bytes-001"

    def behavior(cb):
        write_media(tmp_path, nid, f"{nid}.mp4", payload)
        return [note_data(nid)]

    result = run(nid, behavior, tmp_path, ffprobe=lambda p: True)
    assert result["status"] == "ok"
    assert result["code"] is None
    assert result["media_kind"] == "video"
    assert len(result["media"]) == 1
    entry = result["media"][0]
    assert entry["path"] == f"media/{nid}/{nid}.mp4"
    assert entry["bytes"] == len(payload)
    assert entry["sha256"] == sha256_bytes(payload)
    assert result["meta"]["title"] == "测试标题"
    assert result["meta"]["author"] == "测试作者"
    assert result["meta"]["published_at"] == "2026-01-01 00:00:00"
    assert result["meta"]["type"] == "视频"


def test_images_download_ok(tmp_path):
    """图文下载成功：多张图片都在清单里。"""
    nid = make_note_id(2)
    payloads = [f"img-{i}".encode() for i in range(3)]
    urls = [f"https://cdn.example.test/{nid}-{i}.jpg" for i in range(3)]

    def behavior(cb):
        for i, data in enumerate(payloads):
            write_media(tmp_path, nid, f"{nid}-{i}.jpg", data)
        return [note_data(nid, kind="图文", urls=urls)]

    result = run(nid, behavior, tmp_path, ffprobe=lambda p: True)
    assert result["status"] == "ok"
    assert result["media_kind"] == "images"
    assert [m["bytes"] for m in result["media"]] == [len(p) for p in payloads]
    assert [m["sha256"] for m in result["media"]] == [sha256_bytes(p) for p in payloads]


def test_text_note_ok(tmp_path):
    """纯文字笔记：没有下载地址，status ok、media 为空、类型 text。"""
    nid = make_note_id(3)

    def behavior(cb):
        return [note_data(nid, kind="文字", urls=[])]

    result = run(nid, behavior, tmp_path)
    assert result["status"] == "ok"
    assert result["media_kind"] == "text"
    assert result["media"] == []


def test_metadata_unavailable(tmp_path):
    """拿不到作品信息：返回 metadata_unavailable。"""
    nid = make_note_id(4)

    def behavior(cb):
        return []  # 下载器提取失败时返回空列表

    result = run(nid, behavior, tmp_path)
    assert result == {"status": "failed", "code": "metadata_unavailable",
                      "media_kind": "text", "media": [], "meta": {}}


def test_metadata_id_mismatch(tmp_path):
    """返回了别的作品（ID 不匹配）：同样算 metadata_unavailable。"""
    nid = make_note_id(5)

    def behavior(cb):
        return [note_data(make_note_id(99))]

    result = run(nid, behavior, tmp_path)
    assert result["code"] == "metadata_unavailable"


def test_file_too_large_via_progress(tmp_path):
    """下载进度里单个文件超过上限：中止并返回 file_too_large。"""
    nid = make_note_id(6)

    def behavior(cb):
        cb({"total_bytes": 2048, "completed_bytes": 0})
        return []

    result = run(nid, behavior, tmp_path, max_file_bytes=1024)
    assert result["code"] == "file_too_large"
    assert result["status"] == "failed"


def test_file_too_large_on_disk(tmp_path):
    """落盘的文件本身超过上限：返回 file_too_large，不留半截文件。"""
    nid = make_note_id(7)

    def behavior(cb):
        write_media(tmp_path, nid, f"{nid}.mp4", b"x" * 2048)
        return [note_data(nid)]

    result = run(nid, behavior, tmp_path, max_file_bytes=1024, ffprobe=lambda p: True)
    assert result["code"] == "file_too_large"
    assert not (tmp_path / "media" / nid).exists()


def test_disk_low_before_download(tmp_path):
    """磁盘余量检查（下载前）：min_free_bytes 设成超大值触发 disk_low。"""
    nid = make_note_id(8)

    def behavior(cb):
        raise AssertionError("不应该开始下载")

    result = run(nid, behavior, tmp_path, min_free_bytes=10 ** 15)
    assert result["code"] == "disk_low"


def test_disk_low_during_download(tmp_path):
    """下载中途磁盘余量跌破下限：进度回调抛出 disk_low。"""
    nid = make_note_id(9)

    def behavior(cb):
        write_media(tmp_path, nid, f"{nid}.mp4", b"partial")
        cb({"total_bytes": 10, "completed_bytes": 5})  # 先报一次正常进度
        return []

    result = run(nid, behavior, tmp_path, min_free_bytes=10 ** 15, max_file_bytes=10 ** 9)
    assert result["code"] == "disk_low"
    assert not (tmp_path / "media" / nid).exists()


def test_direct_get_fallback_success(tmp_path):
    """媒体检查失败但普通 GET 能下载：兜底成功，ffprobe 校验通过。"""
    nid = make_note_id(10)
    payload = b"fallback-video-bytes"
    probed = []

    def behavior(cb):
        # 下载器"认为"自己失败了：有地址但没写任何文件
        return [note_data(nid)]

    def fake_http_get(url):
        assert url == f"https://cdn.example.test/{nid}.mp4"
        return payload

    def fake_ffprobe(path):
        probed.append(Path(path).name)
        return True

    result = run(nid, behavior, tmp_path, http_get=fake_http_get, ffprobe=fake_ffprobe)
    assert result["status"] == "ok"
    assert result["media_kind"] == "video"
    assert result["media"][0]["sha256"] == sha256_bytes(payload)
    assert (tmp_path / "media" / nid / f"{nid}.mp4").read_bytes() == payload
    assert not (tmp_path / "media" / nid / f"{nid}.mp4.part").exists()
    assert probed  # 兜底文件确实过了 ffprobe


def test_direct_get_fallback_probe_failed(tmp_path):
    """GET 兜底下载成功但 ffprobe 校验不过：返回 media_check_failed，不留文件。"""
    nid = make_note_id(11)

    def behavior(cb):
        return [note_data(nid)]

    result = run(nid, behavior, tmp_path,
                 http_get=lambda url: b"broken-video",
                 ffprobe=lambda p: False)
    assert result["status"] == "failed"
    assert result["code"] == "media_check_failed"
    assert not (tmp_path / "media" / nid).exists()


def test_media_check_failed_no_fallback(tmp_path):
    """文件数量对不上（图文多地址，兜底不支持）：media_check_failed。"""
    nid = make_note_id(12)
    urls = [f"https://cdn.example.test/{nid}-{i}.jpg" for i in range(3)]

    def behavior(cb):
        write_media(tmp_path, nid, f"{nid}-0.jpg", b"only-one")  # 只落了一张
        return [note_data(nid, kind="图文", urls=urls)]

    result = run(nid, behavior, tmp_path, http_get=lambda u: b"")
    assert result["code"] == "media_check_failed"


def test_no_signed_url_leak_on_network_error(tmp_path):
    """下载器抛出的异常带签名链接：返回值和异常都不能包含它。"""
    nid = make_note_id(13)
    signed = make_signed_url(nid)

    def behavior(cb):
        raise RuntimeError(f"boom while fetching {signed}")

    result = run(nid, behavior, tmp_path)
    assert result["status"] == "failed"
    assert result["code"] == "network_error"
    assert signed not in json.dumps(result, ensure_ascii=False)
    assert signed not in str(result)


def test_no_signed_url_leak_in_meta(tmp_path):
    """meta 是白名单字段：XHS 原始数据（含下载地址）不能整体流出去。"""
    nid = make_note_id(14)
    data = note_data(nid)

    def behavior(cb):
        write_media(tmp_path, nid, f"{nid}.mp4", b"v")
        return [data]

    result = run(nid, behavior, tmp_path, ffprobe=lambda p: True)
    dumped = json.dumps(result, ensure_ascii=False)
    assert "下载地址" not in dumped
    assert "点赞数量" not in dumped
    assert "xsec_token" not in dumped
    assert set(result["meta"]) == {"title", "author", "desc", "tags",
                                   "published_at", "type"}


def test_no_partial_files_left_on_failure(tmp_path):
    """下载中途失败：media/<note_id>/ 里的半截文件全部清掉。"""
    nid = make_note_id(15)

    def behavior(cb):
        write_media(tmp_path, nid, f"{nid}.mp4", b"half-downloaded")
        raise RuntimeError("connection reset")  # 下载到一半断开

    result = run(nid, behavior, tmp_path)
    assert result["code"] == "network_error"
    assert not (tmp_path / "media" / nid).exists()
    assert list((tmp_path / "media").glob("*")) == [] if (tmp_path / "media").exists() else True


# 两秒钟的彩色画面和正弦波由代码生成，既不下载素材，也不调用外部编码程序。
def make_probe_video(path: Path) -> Path:
    with av.open(str(path), mode="w") as container:
        video = container.add_stream("mpeg4", rate=10)
        video.width, video.height, video.pix_fmt = 64, 48, "yuv420p"
        audio = container.add_stream("aac", rate=48000)
        audio.layout = "mono"
        for index in range(20):
            pixels = np.full((48, 64, 3), (index * 11) % 256, dtype=np.uint8)
            picture = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            picture.pts, picture.time_base = index, Fraction(1, 10)
            for packet in video.encode(picture):
                container.mux(packet)
            position = np.arange(index * 4800, (index + 1) * 4800)
            samples = (0.2 * np.sin(2 * np.pi * 440 * position / 48000)).astype(np.float32)
            sound = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="fltp", layout="mono")
            sound.sample_rate, sound.pts = 48000, index * 4800
            sound.time_base = Fraction(1, 48000)
            for packet in audio.encode(sound):
                container.mux(packet)
        for stream in (video, audio):
            for packet in stream.encode(None):
                container.mux(packet)
    return path


# 完整的 MP4 能读出首帧；截断文件丢失尾部索引，必须失败且不输出底层诊断文本。
def test_default_probe_reads_generated_video_and_rejects_half_file(tmp_path, caplog, capfd):
    video = make_probe_video(tmp_path / "generated with spaces.mp4")
    assert _default_ffprobe(video) is True
    data = video.read_bytes()
    truncated = tmp_path / "truncated.mp4"
    truncated.write_bytes(data[:len(data) // 2])
    assert _default_ffprobe(truncated) is False
    assert _default_ffprobe(tmp_path / "missing.mp4") is False
    assert caplog.text == ""
    assert capfd.readouterr() == ("", "")


# 元数据不能代替解码成功：没有视频流、非法宽高或空帧都应被拒绝。
@pytest.mark.parametrize("dimensions, frames, expected", [
    ([], [object()], False),
    ([(0, 48)], [object()], False),
    ([(64, 0)], [object()], False),
    ([(64, 48)], [], False),
    ([(0, 0), (64, 48)], [object()], True),
])
def test_default_probe_requires_video_dimensions_and_first_frame(
    tmp_path, monkeypatch, dimensions, frames, expected,
):
    calls, closed = [], []
    streams = [SimpleNamespace(width=width, height=height) for width, height in dimensions]

    # 假解码器保留流对象的身份，确认只解码宽高有效的视频流。
    def decode(stream):
        calls.append(stream)
        return iter(frames)

    @contextmanager
    def open_video(path):
        try:
            yield SimpleNamespace(streams=SimpleNamespace(video=streams), decode=decode)
        finally:
            closed.append(True)

    monkeypatch.setattr(av, "open", open_video)
    assert _default_ffprobe(tmp_path / "synthetic.mp4") is expected
    assert all(stream.width > 0 and stream.height > 0 for stream in calls)
    assert closed == [True]


# 打开、解码、关闭的任何异常都只能变成 False，原始诊断信息也不能漏进日志。
@pytest.mark.parametrize("stage", ["open", "decode", "close"])
def test_default_probe_hides_av_failures(tmp_path, monkeypatch, caplog, capfd, stage):
    detail = "synthetic_private_diagnostic"

    def fail():
        av.logging.log(av.logging.ERROR, "synthetic", detail)
        raise RuntimeError(detail)

    def decode(stream):
        if stage == "decode":
            fail()
        return iter([object()])

    @contextmanager
    def open_video(path):
        if stage == "open":
            fail()
        try:
            yield SimpleNamespace(
                streams=SimpleNamespace(video=[SimpleNamespace(width=64, height=48)]),
                decode=decode,
            )
        finally:
            if stage == "close":
                fail()

    monkeypatch.setattr(av, "open", open_video)
    assert _default_ffprobe(tmp_path / "synthetic.mp4") is False
    assert detail not in caplog.text
    assert capfd.readouterr() == ("", "")


# 即使依赖缺失，媒体校验也遵守布尔接口，由上层统一记录 media_check_failed。
def test_default_probe_missing_pyav_returns_false(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "av", None)
    assert _default_ffprobe(tmp_path / "synthetic.mp4") is False
