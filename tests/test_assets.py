# -*- coding: utf-8 -*-
"""tests/test_assets.py —— 安装资源模块（assets.py）的测试。

全部用假网络（内存文件流）和假 uv，不访问网络、不装任何东西。
"""

import contextlib
import hashlib
import io
import os
import tarfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from xhs_wiki import assets


def fake_manifest(tmp_path: Path) -> dict:
    """构造一份小尺寸的假清单：sha256/大小用真实生成的内容算出来，
    这样 fetch 的校验逻辑跑的是真路径（不是恒真断言）。"""
    downloader_bytes = b"downloader-archive-" + os.urandom(8)
    whisper_files = {
        "config.json": b'{"model":"turbo"}',
        "model.bin": os.urandom(64),
    }
    sensevoice_files = {
        "model.int8.onnx": os.urandom(48),
        "tokens.txt": b"token-a\ntoken-b\n",
    }
    vad_bytes = os.urandom(32)
    manifest = {
        "xhs_downloader": {
            "url": "https://example.invalid/xhs.tar.gz",
            "sha256": hashlib.sha256(downloader_bytes).hexdigest(),
            "extract_dir": "Fake-Downloader-1.0",
            "_bytes": downloader_bytes,
        },
        "whisper": {
            "base": "https://huggingface.co/generated/model/resolve/fake-revision/",
            "dir": "whisper-fake",
            "files": {
                name: [hashlib.sha256(data).hexdigest(), len(data)]
                for name, data in whisper_files.items()
            },
            "_bytes": dict(whisper_files),
        },
        "sensevoice": {
            "archive": "https://example.invalid/sensevoice.tar.bz2",
            "dir": "sensevoice-fake",
            "files": {
                name: [hashlib.sha256(data).hexdigest(), len(data)]
                for name, data in sensevoice_files.items()
            },
            "vad": {
                "url": "https://example.invalid/silero_vad.onnx",
                "sha256": hashlib.sha256(vad_bytes).hexdigest(),
                "size": len(vad_bytes),
            },
            "_bytes": dict(sensevoice_files),
            "_vad_bytes": vad_bytes,
        },
    }
    return manifest


class FakeHTTP:
    """假网络：url → 内存字节流；missing 里的 url 模拟断网。"""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.missing: set[str] = set()
        self.requested: list[str] = []

    def serve(self, url: str, data: bytes) -> None:
        self.files[url] = data

    def __call__(self, url: str):
        self.requested.append(url)
        if url in self.missing or url not in self.files:
            raise OSError("network unreachable")
        return contextlib.closing(io.BytesIO(self.files[url]))


def make_sensevoice_archive(files: dict) -> bytes:
    """把假模型文件打成真的 tar.bz2（走 tarfile 真解压路径）。"""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:bz2") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo("sensevoice-fake/" + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def make_downloader_archive() -> bytes:
    """假的 XHS-Downloader 压缩包：顶层目录 + requirements.txt。"""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in (
            ("Fake-Downloader-1.0/requirements.txt", b"httpx\n"),
            ("Fake-Downloader-1.0/source.py", b"class XHS: pass\n"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class TestFetch(unittest.TestCase):
    """fetch 的校验与原子替换。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_fetch_success_writes_content(self):
        payload = b"hello assets"
        http = FakeHTTP()
        http.serve("https://example.invalid/f.bin", payload)
        dest = self.dir / "sub" / "f.bin"
        assets.fetch("https://example.invalid/f.bin", dest,
                     sha256=hashlib.sha256(payload).hexdigest(),
                     size=len(payload), http=http)
        self.assertEqual(dest.read_bytes(), payload)

    def test_fetch_size_mismatch(self):
        http = FakeHTTP()
        http.serve("https://example.invalid/f.bin", b"12345")
        dest = self.dir / "f.bin"
        with self.assertRaises(ValueError) as ctx:
            assets.fetch("https://example.invalid/f.bin", dest, size=6,
                         http=http)
        self.assertEqual(str(ctx.exception), "assets_size_mismatch")
        # 目标位置不能出现半成品，也不能留临时文件。
        self.assertFalse(dest.exists())
        self.assertEqual(list(dest.parent.iterdir()), [])

    def test_fetch_sha256_mismatch(self):
        http = FakeHTTP()
        http.serve("https://example.invalid/f.bin", b"12345")
        dest = self.dir / "f.bin"
        with self.assertRaises(ValueError) as ctx:
            assets.fetch("https://example.invalid/f.bin", dest, size=5,
                         sha256="0" * 64, http=http)
        self.assertEqual(str(ctx.exception), "assets_sha256_mismatch")
        self.assertFalse(dest.exists())

    def test_fetch_network_error_keeps_old_file(self):
        # 原子替换：目标已有旧内容时，断网后旧内容必须原样保留。
        dest = self.dir / "f.bin"
        dest.write_bytes(b"old content")
        http = FakeHTTP()  # 什么都没配：全部 url 断网。
        with self.assertRaises(ValueError) as ctx:
            assets.fetch("https://example.invalid/f.bin", dest, http=http)
        self.assertEqual(str(ctx.exception), "assets_network_error")
        self.assertEqual(dest.read_bytes(), b"old content")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["f.bin"])

    def test_fetch_progress_callback(self):
        payload = b"x" * (assets.CHUNK_BYTES + 10)  # 跨两个块，回调多次。
        http = FakeHTTP()
        http.serve("https://example.invalid/big", payload)
        seen = []
        # 统一约定：progress(fraction_or_None)，一个参数。
        assets.fetch("https://example.invalid/big", self.dir / "big",
                     size=len(payload), http=http,
                     progress=lambda fraction: seen.append(fraction))
        self.assertEqual(seen[-1], 1.0)
        self.assertGreater(len(seen), 1)

    def test_fetch_progress_none_without_size(self):
        # 清单没写大小时：回调收到 None（"下载中、比例未知"），不是 0。
        payload = b"size unknown"
        http = FakeHTTP()
        http.serve("https://example.invalid/nosize", payload)
        seen = []
        assets.fetch("https://example.invalid/nosize", self.dir / "nosize",
                     http=http, progress=lambda f: seen.append(f))
        self.assertEqual(seen, [None])


class TestInstallDownloader(unittest.TestCase):
    """install_xhs_downloader：下载 → 校验 → 解压 → 假 uv。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "app"
        self.manifest = fake_manifest(Path(self.tmp.name))

    def _http(self):
        http = FakeHTTP()
        entry = self.manifest["xhs_downloader"]
        http.serve(entry["url"],
                   make_downloader_archive() if entry["_bytes"] is None
                   else entry["_bytes"])
        return http

    def test_install_end_to_end_with_fake_uv(self):
        # 用真实 tar 包跑全流程（覆盖 _bytes 换成真的压缩包内容）。
        http = FakeHTTP()
        archive_bytes = make_downloader_archive()
        entry = dict(self.manifest["xhs_downloader"])
        entry["sha256"] = hashlib.sha256(archive_bytes).hexdigest()
        self.manifest["xhs_downloader"] = entry
        http.serve(entry["url"], archive_bytes)

        uv_calls = []
        import unittest.mock as mock
        with mock.patch.object(assets, "load_manifest",
                               return_value=self.manifest):
            directory = assets.install_xhs_downloader(
                self.home, http=http, uv=lambda args: uv_calls.append(list(args)))
        self.assertEqual(directory, self.home / "vendor" / "Fake-Downloader-1.0")
        self.assertTrue((directory / "requirements.txt").is_file())
        # uv 命令形状：pip install --python <venv> -r <requirements>。
        self.assertEqual(uv_calls, [["pip", "install",
                                     "--python", str(self.home / "venv" / "bin" / "python"),
                                     "--only-binary",
                                     "av,ctranslate2,onnxruntime,numpy,tokenizers,sherpa-onnx,cryptography,lxml,curl-cffi,pydantic-core,pyobjc-core",
                                     "-r", str(directory / "requirements.txt")]])
        # 压缩包解压完应被清掉。
        self.assertFalse((self.home / "vendor" / "Fake-Downloader-1.0.tar.gz").exists())

    def test_install_sha_mismatch_aborts(self):
        import unittest.mock as mock
        entry = dict(self.manifest["xhs_downloader"])
        entry["sha256"] = "1" * 64  # 指纹不对。
        self.manifest["xhs_downloader"] = entry
        http = self._http()
        with mock.patch.object(assets, "load_manifest",
                               return_value=self.manifest):
            with self.assertRaises(ValueError) as ctx:
                assets.install_xhs_downloader(self.home, http=http, uv=lambda a: None)
        self.assertEqual(str(ctx.exception), "assets_sha256_mismatch")
        # uv 不能被调用（校验失败就停）。
        self.assertFalse((self.home / "vendor" / "Fake-Downloader-1.0").exists())

    def test_install_network_error(self):
        import unittest.mock as mock
        with mock.patch.object(assets, "load_manifest",
                               return_value=self.manifest):
            with self.assertRaises(ValueError) as ctx:
                assets.install_xhs_downloader(self.home, http=FakeHTTP(),
                                              uv=lambda a: None)
        self.assertEqual(str(ctx.exception), "assets_network_error")


class TestModels(unittest.TestCase):
    """install_model / verify_model：两个模型的安装和核对。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "app"
        self.manifest = fake_manifest(Path(self.tmp.name))
        self.models = self.home / "models"

    def test_install_whisper_downloads_each_file(self):
        import unittest.mock as mock
        entry = self.manifest["whisper"]
        http = FakeHTTP()
        for name, data in entry["_bytes"].items():
            http.serve(entry["base"] + name, data)
        with mock.patch.object(assets, "load_manifest",
                               return_value=self.manifest):
            directory = assets.install_model("whisper", self.home, http=http)
        self.assertEqual(directory, self.models / "whisper-fake")
        for name, data in entry["_bytes"].items():
            self.assertEqual((directory / name).read_bytes(), data)

    def test_install_sensevoice_extracts_and_verifies(self):
        import unittest.mock as mock
        entry = self.manifest["sensevoice"]
        archive = make_sensevoice_archive(entry["_bytes"])
        http = FakeHTTP()
        http.serve(entry["archive"], archive)
        http.serve(entry["vad"]["url"], entry["_vad_bytes"])
        with mock.patch.object(assets, "load_manifest",
                               return_value=self.manifest):
            directory = assets.install_model("sensevoice", self.home, http=http)
        self.assertEqual(directory, self.models / "sensevoice-fake")
        self.assertTrue((self.models / "silero_vad.onnx").is_file())
        # 压缩包解压校验完删除。
        self.assertFalse((self.models / (entry["dir"] + ".tar.bz2")).exists())

    def test_install_sensevoice_bad_content_fails(self):
        # 压缩包能解开，但里面的模型文件被换过内容：指纹核对必须拦住。
        import unittest.mock as mock
        entry = dict(self.manifest["sensevoice"])
        tampered = dict(entry["_bytes"])
        # 换成同长度不同内容：只触发指纹核对，不先撞上大小核对。
        tampered["tokens.txt"] = b"T" * len(entry["_bytes"]["tokens.txt"])
        archive = make_sensevoice_archive(tampered)
        http = FakeHTTP()
        http.serve(entry["archive"], archive)
        with mock.patch.object(assets, "load_manifest",
                               return_value=self.manifest):
            with self.assertRaises(ValueError) as ctx:
                assets.install_model("sensevoice", self.home, http=http)
        self.assertEqual(str(ctx.exception), "assets_sha256_mismatch")

    def test_install_model_invalid_name(self):
        with self.assertRaises(ValueError) as ctx:
            assets.install_model("paddle", self.home, http=FakeHTTP())
        self.assertEqual(str(ctx.exception), "assets_model_invalid")

    def test_verify_model_whisper(self):
        import unittest.mock as mock
        entry = self.manifest["whisper"]
        directory = self.models / "whisper-fake"
        directory.mkdir(parents=True)
        for name, data in entry["_bytes"].items():
            (directory / name).write_bytes(data)
        with mock.patch.object(assets, "load_manifest",
                               return_value=self.manifest):
            ok, missing = assets.verify_model("whisper", self.home)
            self.assertTrue(ok)
            self.assertEqual(missing, [])
            # 弄坏一个文件再核对。
            (directory / "model.bin").write_bytes(b"corrupted")
            ok, missing = assets.verify_model("whisper", self.home)
        self.assertFalse(ok)
        self.assertEqual(missing, ["model.bin"])

    def test_verify_model_sensevoice_includes_vad(self):
        import unittest.mock as mock
        entry = self.manifest["sensevoice"]
        directory = self.models / "sensevoice-fake"
        directory.mkdir(parents=True)
        for name, data in entry["_bytes"].items():
            (directory / name).write_bytes(data)
        # 不写 VAD：核对应报缺失。
        with mock.patch.object(assets, "load_manifest",
                               return_value=self.manifest):
            ok, missing = assets.verify_model("sensevoice", self.home)
            self.assertFalse(ok)
            self.assertEqual(missing, ["silero_vad.onnx"])
            (self.models / "silero_vad.onnx").write_bytes(entry["_vad_bytes"])
            ok, missing = assets.verify_model("sensevoice", self.home)
            self.assertTrue(ok)

    def test_verify_model_invalid_name(self):
        with self.assertRaises(ValueError):
            assets.verify_model("whisper-x", self.home)


class TestCliAssets(unittest.TestCase):
    """cli 的 assets 子命令分发（不碰网络：monkeypatch assets 模块函数）。"""

    def setUp(self):
        import tempfile
        import os
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "app"
        self._old_home = os.environ.get("XHS_WIKI_HOME")
        os.environ["XHS_WIKI_HOME"] = str(self.home)

    def tearDown(self):
        import os
        if self._old_home is None:
            os.environ.pop("XHS_WIKI_HOME", None)
        else:
            os.environ["XHS_WIKI_HOME"] = self._old_home

    def _main(self, *argv):
        from xhs_wiki import cli
        lines = []
        with unittest.mock.patch.object(cli, "_print", lambda line="": lines.append(line)):
            code = cli.main(list(argv))
        return code, lines

    def test_verify_reports_missing(self):
        code, lines = self._main("assets", "verify", "whisper")
        self.assertEqual(code, 1)
        self.assertTrue(any("✗" in line for line in lines))

    def test_install_model_dispatch(self):
        import unittest.mock as mock
        from xhs_wiki import assets as assets_module
        with mock.patch.object(assets_module, "install_model") as install, \
             mock.patch.object(assets_module, "verify_model",
                               return_value=(False, ["model.bin"])):
            code, lines = self._main("assets", "install-model", "whisper")
        self.assertEqual(code, 0)
        install.assert_called_once_with("whisper", self.home)
        self.assertTrue(any("已安装" in line for line in lines))

    def test_install_model_skips_when_complete(self):
        import unittest.mock as mock
        from xhs_wiki import assets as assets_module
        with mock.patch.object(assets_module, "install_model") as install, \
             mock.patch.object(assets_module, "verify_model",
                               return_value=(True, [])):
            code, lines = self._main("assets", "install-model", "whisper")
        self.assertEqual(code, 0)
        install.assert_not_called()
        self.assertTrue(any("跳过" in line for line in lines))


class TestCurlFetch(unittest.TestCase):
    """默认下载走系统 curl 的路径（全部用假 curl，不启动子进程）。

    分两层测：
    - ``downloader=`` 注入点：直接换掉整个下载函数，测 fetch 的进度、
      校验和 .part 清理逻辑；
    - ``assets._curl_download`` 本体：patch ``subprocess.Popen`` 造一个
      假 curl 进程，测命令行参数形状、退出码和断点续传。
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    # -- downloader= 注入层 ------------------------------------------------

    def test_downloader_success_progress_monotonic(self):
        # 假 curl 正常完成：进度单调上升，校验通过，内容落到目标位置。
        payload = bytes(range(256)) * 40  # 10240 字节，分块才有多段进度

        def fake_curl(url, temporary_path, *, on_bytes=None):
            # 模拟真 curl：分块写文件，每块回调一次字节数。
            done = 0
            with temporary_path.open("wb") as output:
                for start in range(0, len(payload), 1024):
                    chunk = payload[start:start + 1024]
                    output.write(chunk)
                    done += len(chunk)
                    if on_bytes is not None:
                        on_bytes(done)

        seen = []
        dest = self.dir / "model.bin"
        assets.fetch("https://example.invalid/model.bin", dest,
                     sha256=hashlib.sha256(payload).hexdigest(),
                     size=len(payload), downloader=fake_curl,
                     progress=lambda fraction: seen.append(fraction))
        self.assertEqual(dest.read_bytes(), payload)
        # 进度单调上升且最终到 1.0；中间有真实的多段值。
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 1.0)
        self.assertGreater(len(seen), 1)
        self.assertLess(seen[0], 0.5)
        # 成功后 .part 不能留在目录里。
        self.assertEqual([p.name for p in self.dir.iterdir()], ["model.bin"])

    def test_downloader_progress_none_without_size(self):
        # size 未知时回调收到 None（"下载中、比例未知"），不是 0。
        def fake_curl(url, temporary_path, *, on_bytes=None):
            temporary_path.write_bytes(b"unknown-size")
            if on_bytes is not None:
                on_bytes(len(b"unknown-size"))

        seen = []
        assets.fetch("https://example.invalid/nosize", self.dir / "nosize",
                     downloader=fake_curl,
                     progress=lambda fraction: seen.append(fraction))
        self.assertEqual(seen, [None])

    def test_downloader_failure_keeps_part_for_resume(self):
        # 假 curl 中途退出（抛 assets_network_error）→ .part 保留，
        # 下次接着下（断点续传的前提是临时文件还在）。
        payload = b"0123456789" * 100

        def flaky_curl(url, temporary_path, *, on_bytes=None):
            if not temporary_path.exists():
                # 第一次：写一半就"退出码非 0"。
                temporary_path.write_bytes(payload[:300])
                if on_bytes is not None:
                    on_bytes(300)
                raise ValueError("assets_network_error")
            # 第二次：从 .part 已有内容接着补全（模拟 --continue-at -）。
            with temporary_path.open("ab") as output:
                output.write(payload[temporary_path.stat().st_size:])
            if on_bytes is not None:
                on_bytes(temporary_path.stat().st_size)

        dest = self.dir / "resume.bin"
        with self.assertRaises(ValueError) as ctx:
            assets.fetch("https://example.invalid/resume.bin", dest,
                         sha256=hashlib.sha256(payload).hexdigest(),
                         size=len(payload), downloader=flaky_curl)
        self.assertEqual(str(ctx.exception), "assets_network_error")
        # .part 保留（网络失败不删，留给断点续传）。
        part = self.dir / ".resume.bin.part"
        self.assertTrue(part.is_file())
        self.assertEqual(part.read_bytes(), payload[:300])
        # 第二次下载成功，续传后内容完整、位置正确。
        assets.fetch("https://example.invalid/resume.bin", dest,
                     sha256=hashlib.sha256(payload).hexdigest(),
                     size=len(payload), downloader=flaky_curl)
        self.assertEqual(dest.read_bytes(), payload)
        self.assertFalse(part.exists())

    def test_downloader_sha_mismatch_deletes_part(self):
        # sha256 不对：报 assets_sha256_mismatch，并删除 .part
        # （坏数据续传下去永远对不上，下次必须从头下）。
        def bad_curl(url, temporary_path, *, on_bytes=None):
            temporary_path.write_bytes(b"corrupted-content")
            if on_bytes is not None:
                on_bytes(len(b"corrupted-content"))

        dest = self.dir / "bad.bin"
        with self.assertRaises(ValueError) as ctx:
            assets.fetch("https://example.invalid/bad.bin", dest,
                         size=len(b"corrupted-content"),
                         sha256="0" * 64, downloader=bad_curl)
        self.assertEqual(str(ctx.exception), "assets_sha256_mismatch")
        self.assertFalse(dest.exists())
        self.assertFalse((self.dir / ".bad.bin.part").exists())
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_downloader_size_mismatch_deletes_part(self):
        # 大小不对：同样删 .part 从头再来。
        def short_curl(url, temporary_path, *, on_bytes=None):
            temporary_path.write_bytes(b"12345")
            if on_bytes is not None:
                on_bytes(5)

        dest = self.dir / "short.bin"
        with self.assertRaises(ValueError) as ctx:
            assets.fetch("https://example.invalid/short.bin", dest,
                         size=6, downloader=short_curl)
        self.assertEqual(str(ctx.exception), "assets_size_mismatch")
        self.assertFalse((self.dir / ".short.bin.part").exists())

    # -- _curl_download 本体（假 Popen） -----------------------------------

    def test_curl_command_shape(self):
        # 启动参数必须是列表形式，并包含要求的关键参数。
        calls = []

        class FakePopen:
            def __init__(self, args, **kwargs):
                calls.append(args)
                self.pid = 10001  # 仅写 PID 记录，不对这个虚构编号发送信号。
                self.returncode = 0  # 立即成功，不进轮询循环

            def poll(self):
                return 0

        with unittest.mock.patch.object(assets.subprocess, "Popen",
                                        FakePopen):
            assets._curl_download("https://example.invalid/f",
                                  self.dir / ".f.part", on_bytes=lambda n: n)
        self.assertEqual(len(calls), 1)
        args = calls[0]
        self.assertIsInstance(args, list)  # 列表形式，绝不 shell=True
        self.assertEqual(args[0], "/usr/bin/curl")
        for flag in ("--fail", "--location", "--silent", "--show-error",
                     "--connect-timeout", "--retry", "--retry-delay",
                     "--continue-at", "--output"):
            self.assertIn(flag, args)
        # --continue-at 的值是 "-"（从现有 .part 大小续传）。
        self.assertEqual(args[args.index("--continue-at") + 1], "-")
        self.assertIn("15", args)   # 连接超时 15 秒
        self.assertIn("3", args)    # 重试 3 次
        # URL 是最后一个参数；--output 指向临时文件。
        self.assertEqual(args[-1], "https://example.invalid/f")
        self.assertEqual(Path(args[args.index("--output") + 1]),
                         self.dir / ".f.part")

    def test_curl_exit_code_maps_to_network_error(self):
        # curl 退出码非 0 → assets_network_error，错误信息不带 URL。
        class FailPopen:
            def __init__(self, args, **kwargs):
                self.pid = 10002
                self.returncode = 22

            def poll(self):
                return 22

        with unittest.mock.patch.object(assets.subprocess, "Popen",
                                        FailPopen):
            with self.assertRaises(ValueError) as ctx:
                assets._curl_download("https://example.invalid/f",
                                      self.dir / ".f.part")
        self.assertEqual(str(ctx.exception), "assets_network_error")
        self.assertNotIn("example.invalid", str(ctx.exception))

    def test_curl_resume_second_call_uses_continue_at(self):
        # 第一次中途退出（写了一半退出码非 0）→ fetch 报网络错误且
        # .part 保留；再次下载时命令里必须带 --continue-at -。
        payload = b"abcdefgh" * 64  # 512 字节

        class HalfThenFullPopen:
            """假 curl：第 1 次调用写一半就退出 18（传输被断），
            从第 2 次起把剩下的字节补完（模拟 --continue-at - 续传）。
            每写一小块睡 2 毫秒，让轮询有机会观察到中间进度。"""

            invocation = 0

            def __init__(self, args, **kwargs):
                self.args = list(args)
                self.pid = 10003
                calls.append(self.args)
                self.returncode = None
                type(self).invocation += 1
                self._serial = type(self).invocation
                threading.Thread(target=self._run, daemon=True).start()

            def _run(self):
                out = Path(self.args[self.args.index("--output") + 1])
                already = out.stat().st_size if out.exists() else 0
                limit = len(payload) // 2 if self._serial == 1 else len(payload)
                for start in range(already, limit, 64):
                    time.sleep(0.002)
                    # --continue-at - 语义：追加写。
                    with out.open("ab") as handle:
                        handle.write(payload[start:start + 64])
                self.returncode = 18 if self._serial == 1 else 0

            def poll(self):
                return self.returncode

            def kill(self):
                self.returncode = -1

            def wait(self):
                pass

        calls = []
        HalfThenFullPopen.invocation = 0
        dest = self.dir / "resume.bin"
        # 轮询间隔 patch 小，让进度回调有机会观察到中间值。
        with unittest.mock.patch.object(assets, "CURL_POLL_SECONDS", 0.001), \
             unittest.mock.patch.object(assets.subprocess, "Popen",
                                        HalfThenFullPopen):
            # 第一次：写一半就"退出码非 0"。
            with self.assertRaises(ValueError) as ctx:
                assets.fetch("https://example.invalid/resume.bin", dest,
                             sha256=hashlib.sha256(payload).hexdigest(),
                             size=len(payload),
                             progress=lambda f: None)
            self.assertEqual(str(ctx.exception), "assets_network_error")
            part = self.dir / ".resume.bin.part"
            self.assertTrue(part.is_file())
            self.assertEqual(part.stat().st_size, len(payload) // 2)
            # 第二次：从 .part 续传补完，校验通过。
            seen = []
            assets.fetch("https://example.invalid/resume.bin", dest,
                         sha256=hashlib.sha256(payload).hexdigest(),
                         size=len(payload),
                         progress=lambda f: seen.append(f))
        self.assertEqual(dest.read_bytes(), payload)
        # 两次启动的命令都带断点续传参数（第二次尤其必须）。
        self.assertEqual(len(calls), 2)
        for args in calls:
            self.assertIsInstance(args, list)
            self.assertIn("--continue-at", args)
            self.assertEqual(args[args.index("--continue-at") + 1], "-")
        self.assertEqual(seen[-1], 1.0)


if __name__ == "__main__":
    unittest.main()
