# -*- coding: utf-8 -*-
"""下载生命周期回归测试：验证遗留进程归属、退出清理及校验重下上限。

下载一律使用内存数据和假 Popen；只有命令行归属测试启动本测试拥有的
Python 睡眠进程，不运行真实 curl、不访问网络，文件全部放在 tmp_path。
"""

import hashlib
import os
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from xhs_wiki import assets


class FakeCurl:
    """最小进程替身：写入生成的内容并记录终止操作，不发送真实信号。"""

    def __init__(self, args, *, payload=b"generated", running=False, stubborn=False, **kwargs):
        """使用固定虚构 PID；调用方只有替身方法能结束这个虚构进程。"""
        self.pid = 12345
        self.returncode = None if running else 0
        self.stubborn = stubborn
        self.signals = []
        self.waits = []
        self.path = Path(args[args.index("--output") + 1])
        self.path.write_bytes(payload)

    def poll(self):
        """返回替身状态，不访问真实进程表。"""
        return self.returncode

    def terminate(self):
        """模拟是否响应温和终止，覆盖五秒后的强制结束分支。"""
        self.signals.append(signal.SIGTERM)
        if not self.stubborn:
            self.returncode = -signal.SIGTERM

    def kill(self):
        """模拟强制结束。"""
        self.signals.append(signal.SIGKILL)
        self.returncode = -signal.SIGKILL

    def wait(self, timeout=None):
        """不真实等待五秒，直接模拟超时或已退出。"""
        self.waits.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("generated", timeout)
        return self.returncode


@pytest.mark.parametrize("matching", [True, False])
def test_orphan_is_checked_before_new_writer(tmp_path, monkeypatch, matching):
    """只终止带 curl 和本临时路径的自建进程，无关睡眠进程必须继续存活。"""
    part = tmp_path / "generated space" / ".model.bin.part"
    part.parent.mkdir()
    args = [sys.executable, "-c", "import time; time.sleep(60)"]
    if matching:
        args += ["curl", str(part)]
    sleeper = subprocess.Popen(args)
    pid_path = assets._curl_pid_path(part)
    assets._write_curl_pid(pid_path, sleeper.pid)
    seen = []

    def ps(command, **kwargs):
        """只替换沙箱禁用的 ps；命令行来自本用例刚启动的参数，信号仍真实发送。

        进程已结束时由 Popen.poll 回收，模拟 ps 找不到该进程；禁止查询
        任何本用例没有创建的 PID，不以真实用户的进程充当测试数据。
        """
        assert command == ["/bin/ps", "-o", "command=", "-p", str(sleeper.pid)]
        running = sleeper.poll() is None
        return SimpleNamespace(returncode=0 if running else 1,
                               stdout=" ".join(args) if running else "")

    monkeypatch.setattr(assets.subprocess, "run", ps)

    def spawn(args, **kwargs):
        """新写入者开始时旧 PID 已清除，且旧进程的状态符合归属判断。"""
        assert not pid_path.exists()
        if matching:
            assert sleeper.wait(timeout=1) == -signal.SIGTERM
        else:
            assert sleeper.poll() is None
        seen.append(args)
        return FakeCurl(args, **kwargs)

    try:
        manager = assets.CurlDownloadManager(popen=spawn)
        manager.download("https://example.invalid/generated", part)
        assert len(seen) == 1
        assert not pid_path.exists()
    finally:
        # 不管被测逻辑通过或失败，都只回收本用例自己创建的进程。
        if sleeper.poll() is None:
            sleeper.kill()
        sleeper.wait(timeout=5)


@pytest.mark.parametrize("value", ["0", "-1", "1", "garbled", "", "self"])
def test_invalid_pid_never_signals(tmp_path, monkeypatch, value):
    """非法编号不能成为进程组信号，也不能结束当前服务。"""
    part = tmp_path / ".generated.part"
    path = assets._curl_pid_path(part)
    path.write_text(str(os.getpid()) if value == "self" else value)

    def forbidden(*args):
        """任何信号都算失败，包含探测存在性的信号 0。"""
        pytest.fail("unexpected_signal")

    monkeypatch.setattr(assets.os, "kill", forbidden)
    assets._cleanup_orphan_curl(part)
    assert not path.exists()


def test_gone_pid_is_removed(tmp_path, monkeypatch):
    """已不存在的 PID 只删记录，不读取命令行或启动清理进程。"""
    part = tmp_path / ".generated.part"
    path = assets._curl_pid_path(part)
    assets._write_curl_pid(path, 12345)

    def gone(pid, sig):
        """只允许探测，不允许真正的结束信号。"""
        assert sig == 0
        raise ProcessLookupError

    monkeypatch.setattr(assets.os, "kill", gone)
    assets._cleanup_orphan_curl(part)
    assert not path.exists()


@pytest.mark.parametrize("failure", ["denied", "timeout", "nonzero"])
def test_ps_failure_blocks_download_and_keeps_pid(tmp_path, monkeypatch, failure):
    """无法验证归属时不误杀、不删记录，也不能启动新写入者。"""
    part = tmp_path / ".generated.part"
    path = assets._curl_pid_path(part)
    assets._write_curl_pid(path, 12345)
    signals = []
    monkeypatch.setattr(assets.os, "kill", lambda pid, sig: signals.append(sig))

    def ps(args, **kwargs):
        """验证查询参数，模拟权限拒绝、超时或查询失败。"""
        assert args == ["/bin/ps", "-o", "command=", "-p", "12345"]
        if failure == "denied":
            raise PermissionError("generated")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 2)
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(assets.subprocess, "run", ps)
    manager = assets.CurlDownloadManager(popen=lambda *a, **kw: pytest.fail("unexpected_spawn"))
    with pytest.raises(ValueError, match="^assets_error$"):
        manager.download("https://example.invalid/generated", part)
    assert signals and all(sig == 0 for sig in signals)
    assert path.exists()


@pytest.mark.parametrize("reused", [True, False])
def test_orphan_kill_rechecks_identity_after_five_seconds(tmp_path, monkeypatch, reused):
    """假时钟覆盖五秒升级；编号换了主人就不能发送 SIGKILL。"""
    part = tmp_path / ".generated.part"
    path = assets._curl_pid_path(part)
    assets._write_curl_pid(path, 12345)
    now = [0.0]
    signals = []
    checks = []

    def matches(pid, candidate, **kwargs):
        """超时后的再次核对模拟 PID 复用，强杀后的核对模拟旧进程消失。"""
        checks.append(now[0])
        if signal.SIGKILL in signals:
            return False
        if reused and now[0] >= 5:
            return False
        return True

    monkeypatch.setattr(assets, "_curl_process_matches", matches)
    monkeypatch.setattr(assets.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(assets.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(assets.os, "kill", lambda pid, sig: signals.append(sig))
    assets._cleanup_orphan_curl(part)
    assert now[0] == pytest.approx(5)
    assert signals == ([signal.SIGTERM] if reused else [signal.SIGTERM, signal.SIGKILL])
    assert not path.exists()


@pytest.mark.parametrize("stubborn", [False, True])
def test_callback_error_stops_curl_and_removes_private_pid(tmp_path, stubborn):
    """PID 在运行时已经落盘且私有；回调失败也必须回收子进程和记录。"""
    part = tmp_path / ".generated.part"
    processes = []

    def spawn(args, **kwargs):
        """返回一直运行的替身，让回调走异常清理路径。"""
        process = FakeCurl(args, running=True, stubborn=stubborn, **kwargs)
        processes.append(process)
        return process

    def callback(done):
        """核对运行期间 PID 内容、权限及临时文件权限，然后主动失败。"""
        path = assets._curl_pid_path(part)
        assert path.read_text().strip() == str(processes[0].pid)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(part.stat().st_mode) == 0o600
        raise RuntimeError("generated_callback_error")

    manager = assets.CurlDownloadManager(popen=spawn)
    with pytest.raises(RuntimeError, match="generated_callback_error"):
        manager.download("https://example.invalid/generated", part, on_bytes=callback)
    assert processes[0].signals == ([signal.SIGTERM, signal.SIGKILL] if stubborn else [signal.SIGTERM])
    assert processes[0].waits == ([5, 5] if stubborn else [5])
    assert not assets._curl_pid_path(part).exists()
    assert part.exists()


def test_success_removes_pid_and_preserves_new_owner(tmp_path):
    """正常完成会删 PID，但旧调用的收尾不能误删新服务写入的编号。"""
    part = tmp_path / ".generated.part"
    path = assets._curl_pid_path(part)
    manager = assets.CurlDownloadManager(popen=FakeCurl)
    manager.download("https://example.invalid/generated", part)
    assert not path.exists()

    def new_owner(done):
        """模拟另一服务已经登记；旧调用只拥有自己的 PID，不能清新记录。"""
        assets._write_curl_pid(path, 54321)

    manager.download("https://example.invalid/generated", part, on_bytes=new_owner)
    assert path.read_text().strip() == "54321"


def test_pid_write_failure_reaps_started_curl(tmp_path, monkeypatch):
    """进程已创建但 PID 落盘失败时也必须结束，不能制造新的遗留写入者。"""
    processes = []
    part = tmp_path / ".generated.part"

    def spawn(args, **kwargs):
        """保存被创建的替身供收尾断言。"""
        process = FakeCurl(args, running=True, **kwargs)
        processes.append(process)
        return process

    def fail_write(*args):
        """模拟磁盘失败，生产错误只允许暴露固定错误码。"""
        raise OSError("generated_write_failure")

    monkeypatch.setattr(assets, "_write_curl_pid", fail_write)
    with pytest.raises(ValueError, match="^assets_network_error$"):
        assets.CurlDownloadManager(popen=spawn).download("https://example.invalid/generated", part)
    assert processes[0].signals == [signal.SIGTERM]
    assert not assets._curl_pid_path(part).exists()


def test_stop_after_spawn_cannot_miss_unregistered_process(tmp_path):
    """启动与退出交错时，停止操作必须等到新句柄可见并结束它。"""
    spawned = threading.Event()
    release = threading.Event()
    stopping = threading.Event()
    failures = []
    processes = []
    part = tmp_path / ".generated.part"

    def spawn(args, **kwargs):
        """模拟进程已产生、但 Popen 尚未返回给 PID 登记逻辑。"""
        process = FakeCurl(args, running=True, **kwargs)
        processes.append(process)
        spawned.set()
        assert release.wait(3)
        return process

    manager = assets.CurlDownloadManager(popen=spawn)

    def download():
        """后台下载被 stop 终止后会返回固定错误码。"""
        try:
            manager.download("https://example.invalid/generated", part)
        except ValueError as error:
            failures.append(str(error))

    def stop():
        """记录停止请求已发起，让启动和停止的交错顺序可断言。"""
        stopping.set()
        manager.stop()

    worker = threading.Thread(target=download)
    stopper = threading.Thread(target=stop)
    try:
        worker.start()
        assert spawned.wait(3)
        stopper.start()
        assert stopping.wait(3)
    finally:
        release.set()
        worker.join(5)
        if stopper.ident is not None:
            stopper.join(5)
        manager.stop()
    assert not worker.is_alive() and not stopper.is_alive()
    assert processes[0].signals == [signal.SIGTERM]
    assert failures == ["assets_network_error"]
    assert not assets._curl_pid_path(part).exists()
    with pytest.raises(ValueError, match="^assets_error$"):
        manager.download("https://example.invalid/generated", part)
    assert len(processes) == 1


@pytest.mark.parametrize("mismatch", ["size", "sha256"])
@pytest.mark.parametrize("recover", [True, False])
def test_integrity_retry_is_once_and_starts_from_zero(tmp_path, mismatch, recover):
    """首轮坏内容允许重下一次，第二轮仍坏就保留旧目标并报告校验错误。"""
    payload = bytes(range(64))
    damaged = payload[:-1] if mismatch == "size" else bytes(value ^ 0xFF for value in payload)
    dest = tmp_path / "generated.bin"
    dest.write_bytes(b"old-generated")
    part = tmp_path / ".generated.bin.part"
    part.write_bytes(b"old-part")
    starts = []

    def download(url, path, *, on_bytes=None):
        """记住下载前已有的字节，证明第二轮没有续接损坏内容。"""
        starts.append(path.read_bytes() if path.exists() else None)
        path.write_bytes(payload if recover and len(starts) == 2 else damaged)

    kwargs = dict(size=len(payload), sha256=hashlib.sha256(payload).hexdigest(), downloader=download)
    if recover:
        assets.fetch("https://example.invalid/generated", dest, **kwargs)
        assert dest.read_bytes() == payload
    else:
        with pytest.raises(ValueError, match=f"^assets_{mismatch}_mismatch$"):
            assets.fetch("https://example.invalid/generated", dest, **kwargs)
        assert dest.read_bytes() == b"old-generated"
    assert starts == [b"old-part", None]
    assert not part.exists()


def test_model_is_ready_after_one_integrity_retry(tmp_path, monkeypatch):
    """小型生成清单跑真实安装和核对，恢复后模型能被识别为就绪。"""
    payload = bytes(range(64))
    manifest = {"whisper": {"dir": "generated-model",
        "base": "https://huggingface.co/generated/resolve/revision/",
        "files": {"model.bin": [hashlib.sha256(payload).hexdigest(), len(payload)]}}}
    monkeypatch.setattr(assets, "load_manifest", lambda: manifest)
    calls = []

    def download(url, path, *, on_bytes=None):
        """第一次写短文件，第二次完整恢复，保证没有第三次调用。"""
        calls.append(url)
        path.write_bytes(payload[:-1] if len(calls) == 1 else payload)

    assets.install_model("whisper", tmp_path, downloader=download)
    assert assets.model_ready("whisper", tmp_path)
    assert len(calls) == 2 and calls[0] == calls[1]


def test_network_failure_during_integrity_retry_keeps_resume_data(tmp_path):
    """完整重下时发生网络错误也不能再次重下，应保留新的断点数据。"""
    calls = []

    def download(url, path, *, on_bytes=None):
        """第一轮校验失败，第二轮只写一部分后断网。"""
        calls.append(url)
        path.write_bytes(b"generated-part")
        if len(calls) == 2:
            raise ValueError("assets_network_error")

    dest = tmp_path / "generated.bin"
    with pytest.raises(ValueError, match="^assets_network_error$"):
        assets.fetch("https://example.invalid/generated", dest, size=100, downloader=download)
    assert len(calls) == 2
    assert (tmp_path / ".generated.bin.part").read_bytes() == b"generated-part"
    assert not dest.exists()
