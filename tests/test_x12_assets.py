# -*- coding: utf-8 -*-
"""X12 后台资源回归：用现场生成的压缩包覆盖校验、续传和原子安装。

本文件只写 pytest 的临时目录；所有下载和硬件探测均注入替身，不联网、
不执行下载出来的程序，也不读取真实用户的应用目录。
"""

import contextlib
import hashlib
import io
import os
import stat
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from xhs_wiki import assets


def archive_bytes(files):
    """生成小型真实 tar.gz，让被测代码经过真实解压与内容指纹验证。"""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, content in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(content)
            item.mode = 0o777
            archive.addfile(item, io.BytesIO(content))
    return stream.getvalue()


@pytest.fixture
def release(tmp_path, monkeypatch):
    """替换随包清单与芯片检测，避免测试依赖待发布的真实指纹或本机芯片。"""
    body = b"generated-not-an-executable-" + os.urandom(12)
    folder = "generated-headless-1"
    payload = archive_bytes({f"{folder}/llm-wiki-desktop": body,
                             f"{folder}/LICENSE": b"generated-license"})
    entry = {"version": "generated-1", "url": "https://example.invalid/generated.tar.gz",
             "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload),
             "extract_dir": folder, "binary": "llm-wiki-desktop",
             "binary_sha256": hashlib.sha256(body).hexdigest()}
    monkeypatch.setattr(assets, "load_manifest", lambda: {"llmwiki": entry})
    monkeypatch.setattr(assets, "llmwiki_supported", lambda: True)
    monkeypatch.setattr(assets, "_LLMWIKI_VERIFY_CACHE", {})
    return SimpleNamespace(home=tmp_path / "generated home", entry=entry,
                           payload=payload, body=body)


def downloader_for(payload, calls=None):
    """假下载器只写生成的字节，保留 fetch 自己的校验和进度逻辑。"""
    def download(url, path, *, on_bytes=None):
        """记录临时路径并报告写入大小，不调用任何外部下载程序。"""
        if calls is not None:
            calls.append(path)
        path.write_bytes(payload)
        if on_bytes is not None:
            on_bytes(len(payload))
    return download


def test_native_arm64_needs_no_sysctl(monkeypatch):
    """原生 Apple 芯片直接通过，不必启动子进程。"""
    monkeypatch.setattr(assets.os, "uname", lambda: SimpleNamespace(sysname="Darwin", machine="arm64"))
    monkeypatch.setattr(assets.subprocess, "run", lambda *a, **k: pytest.fail("unexpected_process"))
    assert assets.llmwiki_supported() is True


@pytest.mark.parametrize("value,code,expected", [("1\n", 0, True), ("0\n", 0, False),
                                                 ("1\n", 1, False), ("unknown", 0, False)])
def test_intel_or_rosetta_probe(monkeypatch, value, code, expected):
    """Rosetta 的 uname 和 Intel 一样，必须以 sysctl 成功返回 1 为依据。"""
    monkeypatch.setattr(assets.os, "uname", lambda: SimpleNamespace(sysname="Darwin", machine="x86_64"))
    def probe(args, **kwargs):
        """核对参数列表和五秒超时，并返回生成的能力值。"""
        assert args == ["/usr/sbin/sysctl", "-n", "hw.optional.arm64"]
        assert kwargs["timeout"] == 5
        assert not kwargs.get("shell", False)
        return SimpleNamespace(stdout=value, returncode=code)
    monkeypatch.setattr(assets.subprocess, "run", probe)
    assert assets.llmwiki_supported() is expected


@pytest.mark.parametrize("error", [OSError("generated"), subprocess.TimeoutExpired("generated", 5)])
def test_platform_probe_failure_is_unsupported(monkeypatch, error):
    """命令缺失或超时都保守判断为不支持，不向页面暴露原始错误。"""
    monkeypatch.setattr(assets.os, "uname", lambda: SimpleNamespace(sysname="Darwin", machine="x86_64"))
    def fail(*args, **kwargs):
        """抛出替身异常，不启动 sysctl。"""
        raise error
    monkeypatch.setattr(assets.subprocess, "run", fail)
    assert assets.llmwiki_supported() is False


def test_non_mac_arm_is_unsupported(monkeypatch):
    """ARM Linux 不是 Apple 芯片的 Mac，不能误装 Darwin 程序。"""
    monkeypatch.setattr(assets.os, "uname", lambda: SimpleNamespace(sysname="Linux", machine="arm64"))
    assert assets.llmwiki_supported() is False


def test_install_verifies_binary_and_private_permissions(release):
    """正常安装返回绝对路径，程序可执行，资料私有，临时包与目录全部清除。"""
    seen = []
    binary = assets.install_llmwiki(release.home, downloader=downloader_for(release.payload), progress=seen.append)
    assert binary == assets.llmwiki_binary_path(release.home)
    assert binary.is_absolute() and binary.read_bytes() == release.body
    assert stat.S_IMODE(binary.stat().st_mode) == 0o755
    assert stat.S_IMODE(binary.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((binary.parent / "LICENSE").stat().st_mode) == 0o600
    assert list((release.home / "vendor").iterdir()) == [binary.parent]
    assert seen[-1] == 1.0
    assert assets.llmwiki_ready(release.home)


def test_install_stream_http_is_injectable(release):
    """旧的内存流下载接口同样通过真实 fetch 和解压路径。"""
    requests = []
    def http(url):
        """只返回内存文件流。"""
        requests.append(url)
        return contextlib.closing(io.BytesIO(release.payload))
    assert assets.install_llmwiki(release.home, http=http).read_bytes() == release.body
    assert requests == [release.entry["url"]]


def test_archive_hash_mismatch_retries_only_once(release):
    """压缩包大小正确但指纹错误，沿用 fetch 的一次完整重下上限。"""
    release.entry["sha256"] = "0" * 64
    calls = []
    with pytest.raises(ValueError, match="^assets_sha256_mismatch$"):
        assets.install_llmwiki(release.home, downloader=downloader_for(release.payload, calls))
    assert len(calls) == 2
    assert list((release.home / "vendor").iterdir()) == []


def test_archive_size_mismatch_retries_only_once(release):
    """错误大小在安装之前失败，不把包的内容当作可运行后台。"""
    release.entry["size"] += 1
    calls = []
    with pytest.raises(ValueError, match="^assets_size_mismatch$"):
        assets.install_llmwiki(release.home, downloader=downloader_for(release.payload, calls))
    assert len(calls) == 2
    assert not assets.llmwiki_ready(release.home)


def test_binary_hash_mismatch_preserves_previous_directory(release):
    """包完整不代表本体可信；新本体不符时旧版本目录和内容原样保留。"""
    old = assets.llmwiki_binary_path(release.home)
    old.parent.mkdir(parents=True)
    old.write_bytes(b"generated-previous")
    release.entry["binary_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="^assets_sha256_mismatch$"):
        assets.install_llmwiki(release.home, downloader=downloader_for(release.payload))
    assert old.read_bytes() == b"generated-previous"
    assert list((release.home / "vendor").iterdir()) == [old.parent]


def test_resume_reuses_fetch_partial_file(release):
    """网络中断保留固定 .part，下次从同一文件续写而不是重下。"""
    calls = []
    split = len(release.payload) // 2
    def partial(url, path, *, on_bytes=None):
        """第一次写半包后模拟网络失败，第二次核对旧内容再续写。"""
        calls.append(path)
        if len(calls) == 1:
            path.write_bytes(release.payload[:split])
            raise ValueError("assets_network_error")
        assert path.read_bytes() == release.payload[:split]
        with path.open("ab") as handle:
            handle.write(release.payload[split:])
    with pytest.raises(ValueError, match="^assets_network_error$"):
        assets.install_llmwiki(release.home, downloader=partial)
    assert calls[0].stat().st_size == split
    binary = assets.install_llmwiki(release.home, downloader=partial)
    assert calls[0] == calls[1]
    assert binary.read_bytes() == release.body
    assert not calls[0].exists()


def test_unsupported_platform_does_not_download(release, monkeypatch):
    """平台不支持时不创建目录，更不尝试下载。"""
    monkeypatch.setattr(assets, "llmwiki_supported", lambda: False)
    with pytest.raises(ValueError, match="^llmwiki_unsupported_platform$"):
        assets.install_llmwiki(release.home, downloader=lambda *a, **k: pytest.fail("unexpected_download"))
    assert not release.home.exists()


@pytest.mark.parametrize("kind,code", [("corrupt", "assets_extract_failed"),
                                        ("wrong_dir", "assets_extract_dir_missing"),
                                        ("missing_binary", "llmwiki_binary_missing")])
def test_extract_failures_leave_no_half_install(release, kind, code):
    """压缩格式错误、顶层目录错误和缺本体都不会留下可被误认的安装目录。"""
    if kind == "corrupt":
        payload = b"generated-not-a-tar"
    elif kind == "wrong_dir":
        payload = archive_bytes({"other/generated": b"generated"})
    else:
        payload = archive_bytes({release.entry["extract_dir"] + "/LICENSE": b"generated"})
    release.entry.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    with pytest.raises(ValueError, match=f"^{code}$"):
        assets.install_llmwiki(release.home, downloader=downloader_for(payload))
    assert list((release.home / "vendor").iterdir()) == []


def test_ready_rechecks_replaced_file(release):
    """就绪检查必须发现安装后发生的损坏。"""
    assert not assets.llmwiki_ready(release.home)
    binary = assets.install_llmwiki(release.home, downloader=downloader_for(release.payload))
    assert assets.llmwiki_ready(release.home)
    binary.write_bytes(b"x" * len(release.body))
    assert not assets.llmwiki_ready(release.home)


def test_ready_rejects_symlink(release):
    """同内容符号链接仍不算已安装的普通程序本体。"""
    binary = assets.llmwiki_binary_path(release.home)
    binary.parent.mkdir(parents=True)
    target = release.home / "generated-other"
    target.write_bytes(release.body)
    binary.symlink_to(target)
    assert not assets.llmwiki_ready(release.home)


def test_repeat_install_skips_download_and_restores_mode(release):
    """完整安装重复执行不消耗网络，并确保本体没有丢失执行权限。"""
    binary = assets.install_llmwiki(release.home, downloader=downloader_for(release.payload))
    binary.chmod(0o600)
    assert assets.install_llmwiki(release.home, downloader=lambda *a, **k: pytest.fail("unexpected_download")) == binary
    assert stat.S_IMODE(binary.stat().st_mode) == 0o755


def test_atomic_replace_failure_restores_previous(release, monkeypatch):
    """最后改名失败时还原旧目录，清理展开内容，并给出固定错误码。"""
    binary = assets.llmwiki_binary_path(release.home)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"generated-previous")
    original = assets.os.replace
    def fail_new_install(source, destination):
        """只阻断新版本目录落地，允许旧目录备份及恢复操作。"""
        if Path(destination) == binary.parent and Path(source).name == release.entry["extract_dir"]:
            raise OSError("generated-failure")
        return original(source, destination)
    monkeypatch.setattr(assets.os, "replace", fail_new_install)
    with pytest.raises(ValueError, match="^assets_error$"):
        assets.install_llmwiki(release.home, downloader=downloader_for(release.payload))
    assert binary.read_bytes() == b"generated-previous"
    assert list((release.home / "vendor").iterdir()) == [binary.parent]


def test_asset_total_bytes_uses_archive_size(release):
    """控制台所显示下载大小取压缩包字节数，不把未知大小硬编码到页面。"""
    assert assets.asset_total_bytes("llmwiki") == len(release.payload)


def test_double_replace_failure_preserves_previous_backup(release, monkeypatch):
    """新目录落地和旧目录还原都失败时，唯一的完整旧目录必须留存。"""
    binary = assets.llmwiki_binary_path(release.home)
    binary.parent.mkdir(parents=True)
    previous = b"generated-previous-complete"
    binary.write_bytes(previous)
    original = assets.os.replace
    def fail_destination(source, destination):
        """允许把旧目录移到备份，但拒绝任何正式目录落地操作。"""
        if Path(destination) == binary.parent:
            raise OSError("generated-double-failure")
        return original(source, destination)
    monkeypatch.setattr(assets.os, "replace", fail_destination)
    with pytest.raises(ValueError, match="^assets_error$"):
        assets.install_llmwiki(release.home, downloader=downloader_for(release.payload))
    vendor = release.home / "vendor"
    backups = list(vendor.glob(".llmwiki-previous-*"))
    assert len(backups) == 1
    assert (backups[0] / "previous" / release.entry["binary"]).read_bytes() == previous
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o700
    assert not list(vendor.glob(".llmwiki-install-*"))
    assert not list(vendor.glob("*.tar.gz"))
    assert not assets.llmwiki_ready(release.home)


@pytest.fixture
def cached_binary(release, monkeypatch):
    """直接生成已安装的普通本体，统计实际计算内容指纹的次数。"""
    binary = assets.llmwiki_binary_path(release.home)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(release.body)
    hashes = []
    original = assets._sha256_of
    def calculate(path):
        """仍计算真实指纹，只额外计数，避免用恒真替身掩盖损坏。"""
        hashes.append(path)
        return original(path)
    monkeypatch.setattr(assets, "_sha256_of", calculate)
    return SimpleNamespace(path=binary, hashes=hashes, original=original)


def test_ready_unchanged_file_uses_cached_hash(release, cached_binary):
    """轮询同一本体只做文件属性检查，首次之外不再读取大文件全文。"""
    for _ in range(5):
        assert assets.llmwiki_ready(release.home)
    assert cached_binary.hashes == [cached_binary.path]


def test_ready_manifest_change_invalidates_cache(release, cached_binary):
    """即使文件完全没改，清单预期指纹变化也不能沿用旧的通过结论。"""
    assert assets.llmwiki_ready(release.home)
    expected = release.entry["binary_sha256"]
    release.entry["binary_sha256"] = "0" * 64
    assert not assets.llmwiki_ready(release.home)
    assert not assets.llmwiki_ready(release.home)
    release.entry["binary_sha256"] = expected
    assert assets.llmwiki_ready(release.home)
    assert len(cached_binary.hashes) == 3


def test_ready_mtime_change_recalculates_hash(release, cached_binary):
    """修改时间变化后重新核对，不凭同样的大小判定文件未变。"""
    assert assets.llmwiki_ready(release.home)
    before = cached_binary.path.stat()
    os.utime(cached_binary.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
    assert assets.llmwiki_ready(release.home)
    assert len(cached_binary.hashes) == 2


def test_ready_same_size_replacement_invalidates_cache(release, cached_binary):
    """整体替换为同大小、同修改时间的坏文件，也因文件身份变化而重算。"""
    assert assets.llmwiki_ready(release.home)
    before = cached_binary.path.stat()
    replacement = cached_binary.path.with_name("generated-replacement")
    replacement.write_bytes(b"x" * before.st_size)
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(replacement, cached_binary.path)
    assert not assets.llmwiki_ready(release.home)
    assert len(cached_binary.hashes) == 2


def test_ready_deleted_file_drops_cached_success(release, cached_binary):
    """文件删除立刻失去就绪状态；重新落地后必须重新核对。"""
    assert assets.llmwiki_ready(release.home)
    cached_binary.path.unlink()
    assert not assets.llmwiki_ready(release.home)
    assert cached_binary.path not in assets._LLMWIKI_VERIFY_CACHE
    cached_binary.path.write_bytes(release.body)
    assert assets.llmwiki_ready(release.home)
    assert len(cached_binary.hashes) == 2


def test_ready_file_changed_during_hash_is_not_cached(release, cached_binary, monkeypatch):
    """读取指纹期间文件被替换时，不能把读到的旧指纹缓存为新文件就绪。"""
    def change_after_read(path):
        """先读取旧内容，再模拟另一个写入者修改本体。"""
        digest = cached_binary.original(path)
        path.write_bytes(b"generated-changed-during-read")
        return digest
    monkeypatch.setattr(assets, "_sha256_of", change_after_read)
    assert not assets.llmwiki_ready(release.home)
    assert cached_binary.path not in assets._LLMWIKI_VERIFY_CACHE
