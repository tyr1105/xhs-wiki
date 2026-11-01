# -*- coding: utf-8 -*-
"""tests/test_x94d_asset_progress.py —— 资源下载进度和台账连接的测试。

覆盖两块：
1. 集成测试：把真实的 ``AssetManager._default_install`` 和真实的
   ``assets.install_model`` / ``install_xhs_downloader`` 接在一起跑，
   只把下载器（``assets._curl_download``，下面用 FakeHTTP94d
   适配出假 curl）和 uv（``assets._default_uv``）换成假的。这样"回调
   签名不匹配"这类问题（曾经出过的 bug：assets 层一个参数、daemon 层
   两个参数，第一块数据就 TypeError 且被吞掉）会
   直接让这组测试失败，而不是只在真实安装时卡在"下载中 0%"。
   - whisper：进度从 0 附近单调增长到 1，最后 ready；
   - sensevoice：压缩包大小未知 → 进度是 None（"下载中"），最后 ready；
   - 假网络中途抛非 ValueError 异常 → failed + assets_error，retry 后成功。
2. 数据库连接：HTTP 服务每个请求一个新线程，台账连接必须"用完就关"，
   连续 50 次请求后打开的连接数不增长（曾经出过句柄累积问题）。

红线自查：全部用临时目录（XHS_WIKI_HOME / XHS_WIKI_LOG_DIR 指到
tempfile），不访问网络、不运行安装脚本、不启动真实子进程。
"""

import contextlib
import hashlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

from xhs_wiki import assets as assets_module
from xhs_wiki.daemon import AssetManager, ServiceServer, Supervisor
from xhs_wiki.store import ServiceStore, now_beijing

# 复用 test_assets.py / test_x94b_background_assets.py 里的构造器和假环境（同目录导入）。
from test_assets import fake_manifest, make_downloader_archive, make_sensevoice_archive
from test_x94b_background_assets import (
    AFTERNOON,
    FakeEngine,
    FakeKeychain,
    FakeProvider,
    FakeSpawner,
)


# ---------------------------------------------------------------------------
# 假网络：既能正常出数据，也能"中途断流"（读出若干字节后抛 RuntimeError）
# ---------------------------------------------------------------------------

class BoomStream(io.BytesIO):
    """先交出 ``limit`` 字节，之后 read 抛 RuntimeError。

    为什么要非 ValueError 的异常：fetch 会把 OSError 统一转成
    ``assets_network_error``（ValueError），而这里要测"假网络
    中途抛出非 ValueError 的异常"的路径——那是后来加的兜底
    （``_prepare`` 捕获任何异常 → failed + assets_error）。
    """

    def __init__(self, data: bytes, limit: int):
        super().__init__(data[:limit])

    def read(self, size=-1):
        chunk = super().read(size)
        if not chunk:
            raise RuntimeError("connection reset by peer")
        return chunk


class FakeHTTP94d:
    """假网络：url → 内存字节流；boom 里的 url 中途断流；没配的 url 断网。"""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.boom: dict[str, int] = {}
        self.requested: list[str] = []

    def serve(self, url: str, data: bytes) -> None:
        self.files[url] = data

    def fail_after(self, url: str, limit: int) -> None:
        """让这个 url 的响应在给出 limit 字节后断流。"""
        self.boom[url] = limit

    def __call__(self, url: str):
        self.requested.append(url)
        if url in self.boom:
            return contextlib.closing(
                BoomStream(self.files.get(url, b""), self.boom[url]))
        if url not in self.files:
            raise OSError("network unreachable")
        return contextlib.closing(io.BytesIO(self.files[url]))


class RecordingManager(AssetManager):
    """AssetManager 的子类：链路全部用真实现，只多记一份进度回调实参。

    记录点在 ``_make_progress`` 的外层——如果有人把 assets 层的回调
    改回两个参数，这里会先收到 TypeError（和真实安装时一模一样的
    失败方式），测试立刻失败。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.progress_log: list = []

    def _make_progress(self, kind: str):
        write = super()._make_progress(kind)

        def callback(fraction):
            self.progress_log.append((kind, fraction))
            write(fraction)

        return callback


# ---------------------------------------------------------------------------
# 集成测试基类：真实 _default_check / _default_install，假网络 + 假 uv
# ---------------------------------------------------------------------------

class IntegrationBase(unittest.TestCase):
    """把 AssetManager 和 assets 的真实安装函数接起来（只换下载器和 uv）。

    注入方式：``_default_install`` 里 ``from . import assets`` 后调
    ``install_model`` / ``install_xhs_downloader``，它们内部在
    ``downloader or _curl_download``、``uv or _default_uv`` 处查模块
    全局——所以 patch 模块属性即可让整条真链路离线跑（不访问网络、
    不真装、不真的启动 curl）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["XHS_WIKI_HOME"] = str(Path(self.tmp.name) / "app")
        os.environ["XHS_WIKI_LOG_DIR"] = str(Path(self.tmp.name) / "logs")
        self.home = Path(self.tmp.name) / "app"
        self.store = ServiceStore(self.home)
        self.http = FakeHTTP94d()
        self.manifest = fake_manifest(Path(self.tmp.name))
        self.manager = RecordingManager(self.store, interval=999)

    def fake_downloader(self):
        """把 FakeHTTP94d 适配成 ``_curl_download`` 形状的假 curl。

        语义对照（与旧 FakeHTTP94d 流式替身一一对应）：
        - 正常 url：分块写进临时文件并回调 ``on_bytes``（制造中间进度，
          进度回调的真实链路才会被走到）；
        - boom 里的 url：写出 ``limit`` 字节后抛 RuntimeError（非
          ValueError，喂给 daemon 的"意外异常"兜底路径）；
        - 没配的 url：抛 ValueError("assets_network_error")（断网）。
        """

        def downloader(url, temporary_path, *, on_bytes=None, manager=None):
            # 后台下载器会传下载管理器；本替身不创建进程，只模拟字节流。
            self.http.requested.append(url)
            if url in self.http.boom:
                data = self.http.files.get(url, b"")
                limit = self.http.boom[url]
                temporary_path.write_bytes(data[:limit])
                if on_bytes is not None:
                    on_bytes(limit)
                raise RuntimeError("connection reset by peer")
            if url not in self.http.files:
                raise ValueError("assets_network_error")
            data = self.http.files[url]
            done = 0
            with temporary_path.open("wb") as output:
                for start in range(0, len(data), 4096):
                    chunk = data[start:start + 4096]
                    output.write(chunk)
                    done += len(chunk)
                    if on_bytes is not None:
                        on_bytes(done)

        return downloader

    def serve_downloader(self):
        """把下载组件压缩包挂上假网络（压缩包必须是真的 tar.gz，
        否则解压那步过不去；sha256 同步写回假清单）。"""
        archive = make_downloader_archive()
        entry = dict(self.manifest["xhs_downloader"])
        entry["sha256"] = hashlib.sha256(archive).hexdigest()
        self.manifest["xhs_downloader"] = entry
        self.http.serve(entry["url"], archive)

    def prepare(self):
        """在假清单 + 假 curl + 假 uv 环境下跑一轮真实的 _prepare。"""
        with unittest.mock.patch.object(assets_module, "load_manifest",
                                        return_value=self.manifest), \
             unittest.mock.patch.object(assets_module, "_curl_download",
                                        self.fake_downloader()), \
             unittest.mock.patch.object(assets_module, "_default_uv",
                                        lambda args: None):
            self.manager._prepare()

    def model_progress(self):
        """只取语音模型那一路的进度实参列表。"""
        return [f for kind, f in self.manager.progress_log
                if kind == "asr_model"]


# ---------------------------------------------------------------------------
# 1. whisper：进度 0 → 1 单调增长
# ---------------------------------------------------------------------------

class TestWhisperProgressMonotonic(IntegrationBase):

    def test_progress_grows_monotonically_to_ready(self):
        # 把假 whisper 文件做大（跨多个下载块），进度才有"从 0 涨到 1"
        # 的中间值可看；文件大小和 sha256 都按真实内容算。第一个文件
        # 必须是大文件：这样第一次回调的比例就接近 0（清单按 dict 顺序
        # 逐个下载，小文件排在前面会一上来就 1/N）。
        whisper_files = {
            "model.bin": os.urandom(assets_module.CHUNK_BYTES * 2 + 100),
            "vocab.txt": os.urandom(assets_module.CHUNK_BYTES + 5),
            "config.json": b'{"model":"turbo"}',
        }
        entry = self.manifest["whisper"]
        entry["files"] = {
            name: [hashlib.sha256(data).hexdigest(), len(data)]
            for name, data in whisper_files.items()
        }
        entry["_bytes"] = dict(whisper_files)
        for name, data in whisper_files.items():
            self.http.serve(entry["base"] + name, data)
        self.serve_downloader()

        self.prepare()

        state = self.manager.state()
        self.assertEqual(state["asr_model"]["state"], "ready")
        self.assertEqual(state["asr_model"]["progress"], 1.0)
        self.assertEqual(state["xhs_downloader"]["state"], "ready")
        self.assertTrue(self.manager.all_ready())

        fractions = self.model_progress()
        self.assertTrue(fractions)
        # 每个比例都是 0–1 的数（whisper 各文件大小清单里有写，
        # 不会出现 None）。
        for fraction in fractions:
            self.assertIsInstance(fraction, float)
            self.assertGreaterEqual(fraction, 0.0)
            self.assertLessEqual(fraction, 1.0)
        # 从 0 附近单调增长到 1：不能往回跳，也不能一上来就是 1。
        self.assertEqual(fractions, sorted(fractions))
        self.assertLess(fractions[0], 0.2)
        self.assertEqual(fractions[-1], 1.0)


# ---------------------------------------------------------------------------
# 2. sensevoice：压缩包大小未知 → 进度 None，最后 ready
# ---------------------------------------------------------------------------

class TestSensevoiceUnknownProgress(IntegrationBase):

    def test_progress_none_then_ready(self):
        self.store.update_settings({"asr_model": "sensevoice"})
        entry = self.manifest["sensevoice"]
        # 压缩包大小未知（清单只写了 sha256 之外的字段）——假的也一样。
        self.http.serve(entry["archive"],
                        make_sensevoice_archive(entry["_bytes"]))
        self.http.serve(entry["vad"]["url"], entry["_vad_bytes"])
        self.serve_downloader()

        self.prepare()

        state = self.manager.state()
        self.assertEqual(state["asr_model"]["state"], "ready")
        self.assertEqual(state["asr_model"]["progress"], 1.0)
        # 下载压缩包那一段拿不到比例：回调收到过 None（页面显示
        # "下载中"，而不是误导性的 0%）。
        fractions = self.model_progress()
        self.assertIn(None, fractions)
        # 有比例的阶段（VAD、收尾）仍然给出 0–1 的数。
        self.assertEqual(fractions[-1], 1.0)


# ---------------------------------------------------------------------------
# 3. 非 ValueError 异常 → failed(assets_error)，retry 后成功
# ---------------------------------------------------------------------------

class TestUnexpectedExceptionFailsAndRetries(IntegrationBase):

    def test_boom_then_retry_succeeds(self):
        entry = self.manifest["whisper"]
        for name, data in entry["_bytes"].items():
            self.http.serve(entry["base"] + name, data)
        # 第一个大文件中途断流（RuntimeError，不是 ValueError）。
        big_name = max(entry["files"], key=lambda n: entry["files"][n][1])
        self.http.fail_after(entry["base"] + big_name,
                             assets_module.CHUNK_BYTES // 2)
        self.serve_downloader()

        self.prepare()

        state = self.manager.state()
        self.assertEqual(state["xhs_downloader"]["state"], "ready")
        # 意外异常：failed + assets_error（不带原始异常文本）。
        self.assertEqual(state["asr_model"]["state"], "failed")
        self.assertEqual(state["asr_model"]["error"], "assets_error")
        self.assertFalse(self.manager.all_ready())

        # 网络修好后点"重试"：重新下载并成功。
        self.http.boom.clear()
        self.manager.retry()
        self.assertEqual(self.manager.state()["asr_model"]["state"],
                         "downloading")
        self.prepare()
        state = self.manager.state()
        self.assertEqual(state["asr_model"]["state"], "ready")
        self.assertEqual(state["asr_model"]["progress"], 1.0)
        self.assertTrue(self.manager.all_ready())


# ---------------------------------------------------------------------------
# 4. 数据库连接：连续 50 次请求后打开的连接数不增长
# ---------------------------------------------------------------------------

class ConnectionCounter:
    """数台账连接：created 一共开过几个，open 现在还开着几个。"""

    def __init__(self):
        self.open = 0
        self.created = 0


class CountingLedger:
    """假台账：只实现 /api/status 和单条重试用到的方法，close 时计数。"""

    def __init__(self, counter: ConnectionCounter):
        counter.open += 1
        counter.created += 1
        self._counter = counter

    def counts(self):
        return {"discovered": 0}

    def all_notes(self):
        return []

    def retry(self, note_id):
        pass

    def close(self):
        self._counter.open -= 1


class TestLedgerConnectionsClosedPerRequest(unittest.TestCase):
    """台账连接每个请求用完就关闭，不随请求数累积。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name) / "app"
        os.environ["XHS_WIKI_HOME"] = str(home)
        os.environ["XHS_WIKI_LOG_DIR"] = str(Path(self.tmp.name) / "logs")
        self.store = ServiceStore(home)
        self.counter = ConnectionCounter()
        self.supervisor = Supervisor(
            self.store, spawn=FakeSpawner(), now=lambda: AFTERNOON,
            notify=lambda code: None, assets=None)
        self.server = ServiceServer(
            ("127.0.0.1", 0), supervisor=self.supervisor,
            ledger_factory=lambda: CountingLedger(self.counter),
            keychain=FakeKeychain(),
            make_provider=lambda settings: FakeProvider(),
            make_engine=lambda settings: FakeEngine(),
            opener=lambda args: None,
            skills_root=Path(self.tmp.name) / "skills")
        self.port = self.server.server_port
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def _get_status(self):
        # /api/status 要带访问 Cookie（经真实 /auth 换来，只换一次）。
        if getattr(self, "_cookie", None) is None:
            from test_daemon import server_cookie
            self._cookie = server_cookie(self.server)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", "/api/status",
                         headers={"Host": f"127.0.0.1:{self.port}",
                                  "Cookie": self._cookie})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            return json.loads(response.read())
        finally:
            conn.close()

    def test_50_requests_do_not_accumulate_connections(self):
        for _ in range(50):
            data = self._get_status()
            self.assertIn("counts", data)
        # 每个请求各开一个连接（50 个），但全部用完即关：现在开着 0 个。
        self.assertEqual(self.counter.created, 50)
        self.assertEqual(self.counter.open, 0)


if __name__ == "__main__":
    unittest.main()
