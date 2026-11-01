# -*- coding: utf-8 -*-
"""tests/test_x94b_background_assets.py —— 后台下载组件和语音模型的测试。

覆盖三块：
1. assets.py 新增的就绪检查（xhs_downloader_ready / model_ready）、
   预计大小（asset_total_bytes）和下载进度回调（install_model /
   install_xhs_downloader 的 progress 参数）；
2. daemon.AssetManager：后台资源准备的状态机（downloading → ready /
   failed → retry），进度写入，就绪回调；
3. 带资源线程的守护进程：/api/status 的 assets 字段、资源未就绪时
   run-now 被拦（needs_user assets_not_ready）并在就绪后自动开跑一次、
   POST /api/assets/retry、finish 的 assets_ready 与 setup_incomplete 清理；
4. 退出路径：服务信号处理和下载停止都使用替身，确认先停 curl、
   再关闭服务，且停止之后不会下载下一资源或触发自动开跑。

红线自查：全部用临时目录（XHS_WIKI_HOME / XHS_WIKI_LOG_DIR 指到
tempfile），网络和下载全是假的替身，不启动真实子进程。
"""

import http.client
import json
import os
import re
import signal
import shutil
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

from xhs_wiki import daemon
from xhs_wiki.daemon import AssetManager, ServiceServer, Supervisor
from xhs_wiki.ledger import Ledger
from xhs_wiki.store import ServiceStore, now_beijing

# 复用 test_assets.py 里的假清单/假网络构造器（同目录导入）。
from test_assets import FakeHTTP, fake_manifest, make_downloader_archive

# 与 test_daemon.py 一致的虚构数据和固定时间。
TODAY = now_beijing()
AFTERNOON = TODAY.replace(hour=12, minute=0, second=0, microsecond=0)
FAKE_PROFILE_URL = "https://www.xiaohongshu.com/user/profile/" + "ab" * 12

# 确认 runner 的扩展文案已注册（setup_incomplete 等）。
from xhs_wiki import runner  # noqa: F401,E402


# ---------------------------------------------------------------------------
# assets.py：就绪检查 / 预计大小 / 进度回调
# ---------------------------------------------------------------------------

class TestAssetsReadyChecks(unittest.TestCase):
    """xhs_downloader_ready / model_ready / asset_total_bytes。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "app"
        self.manifest = fake_manifest(Path(self.tmp.name))

    def test_downloader_ready_follows_extract_dir(self):
        import xhs_wiki.assets as assets
        with unittest.mock.patch.object(assets, "load_manifest",
                                        return_value=self.manifest):
            self.assertFalse(assets.xhs_downloader_ready(self.home))
            vendor = self.home / "vendor" / "Fake-Downloader-1.0"
            vendor.mkdir(parents=True)
            # 目录只能证明解压完成；依赖成功后还须有版本相符的就绪标记。
            self.assertFalse(assets.xhs_downloader_ready(self.home))
            # 标记里还要有装依赖时 venv 的身份（第 2 行）。
            (vendor.parent / ".xhs-downloader-ready").write_text(
                assets.downloader_marker_text(self.home, "1.0"), encoding="utf-8")
            self.assertTrue(assets.xhs_downloader_ready(self.home))

    def test_model_ready_false_for_missing_or_unknown(self):
        import xhs_wiki.assets as assets
        with unittest.mock.patch.object(assets, "load_manifest",
                                        return_value=self.manifest):
            self.assertFalse(assets.model_ready("whisper", self.home))
            # 未知模型名不抛异常：后台线程不该被坏设置弄崩。
            self.assertFalse(assets.model_ready("nope", self.home))

    def test_model_ready_true_when_files_match(self):
        import xhs_wiki.assets as assets
        entry = self.manifest["whisper"]
        directory = self.home / "models" / entry["dir"]
        directory.mkdir(parents=True)
        for name, data in entry["_bytes"].items():
            (directory / name).write_bytes(data)
        with unittest.mock.patch.object(assets, "load_manifest",
                                        return_value=self.manifest):
            self.assertTrue(assets.model_ready("whisper", self.home))

    def test_asset_total_bytes_from_real_manifest(self):
        import xhs_wiki.assets as assets
        total = assets.asset_total_bytes("whisper")
        self.assertEqual(
            total,
            sum(size for _sha, size in
                assets.load_manifest()["whisper"]["files"].values()))
        # 真实清单里 whisper 约 1.5 GiB：量级对得上（防清单单位改错）。
        self.assertGreater(total, 1_000_000_000)
        # 下载组件清单没写大小：未知（页面显示"下载中"而不是百分比）。
        self.assertIsNone(assets.asset_total_bytes("xhs_downloader"))
        self.assertIsNone(assets.asset_total_bytes("nope"))


class TestInstallProgress(unittest.TestCase):
    """install_model / install_xhs_downloader 的 progress 回调。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "app"
        self.manifest = fake_manifest(Path(self.tmp.name))
        import xhs_wiki.assets as assets
        self.assets = assets

    def test_whisper_progress_monotonic_to_one(self):
        entry = self.manifest["whisper"]
        http = FakeHTTP()
        for name, data in entry["_bytes"].items():
            http.serve(entry["base"] + name, data)
        seen = []
        with unittest.mock.patch.object(self.assets, "load_manifest",
                                        return_value=self.manifest):
            self.assets.install_model("whisper", self.home, http=http,
                                      progress=lambda f: seen.append(f))
        self.assertTrue(seen)
        self.assertEqual(seen[-1], 1.0)
        # 单调不减：进度条不能往回跳。
        self.assertEqual(seen, sorted(seen))

    def test_downloader_progress_without_total(self):
        archive = make_downloader_archive()
        entry = dict(self.manifest["xhs_downloader"])
        entry["sha256"] = __import__("hashlib").sha256(archive).hexdigest()
        self.manifest["xhs_downloader"] = entry
        http = FakeHTTP()
        http.serve(entry["url"], archive)
        seen = []
        with unittest.mock.patch.object(self.assets, "load_manifest",
                                        return_value=self.manifest):
            self.assets.install_xhs_downloader(
                self.home, http=http, uv=lambda args: None,
                progress=lambda fraction: seen.append(fraction))
        # 统一约定：清单没写压缩包大小 → 回调收到 None（下载中、
        # 比例未知），控制台显示"下载中"而不是 0%。
        self.assertTrue(seen)
        self.assertTrue(all(f is None for f in seen))


# ---------------------------------------------------------------------------
# AssetManager：状态机（不启动线程，直接调 _prepare）
# ---------------------------------------------------------------------------

class FakeAssetsEnv:
    """可编排的假环境：check 的结果和 install 的行为都能逐次指定。"""

    def __init__(self, store, ready=("xhs_downloader", "asr_model")):
        self.store = store
        self.ready = set(ready)
        self.install_calls = []      # (kind, name) 列表
        self.fail_times = {}         # kind -> 前几次抛错
        self.progress_at = {}        # kind -> 安装中汇报的进度
        self.check_count = 0

    def check(self):
        self.check_count += 1
        return ({"xhs_downloader": "xhs_downloader" in self.ready,
                 "asr_model": "asr_model" in self.ready},
                self.store.load_settings().get("asr_model") or "whisper")

    def install(self, kind, name, progress):
        self.install_calls.append((kind, name))
        left = self.fail_times.get(kind, 0)
        if left > 0:
            self.fail_times[kind] = left - 1
            raise ValueError("assets_network_error")
        if progress is not None and kind in self.progress_at:
            # 统一约定：progress(fraction_or_None)，一个参数。
            progress(self.progress_at[kind])


class AssetManagerTestBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["XHS_WIKI_HOME"] = str(Path(self.tmp.name) / "app")
        os.environ["XHS_WIKI_LOG_DIR"] = str(Path(self.tmp.name) / "logs")
        os.environ["XHS_WIKI_SKILLS_ROOT"] = str(Path(self.tmp.name) / "skills")
        self.store = ServiceStore(Path(self.tmp.name) / "app")
        self.fired = []
        self.manager = AssetManager(self.store, interval=999)
        self.manager.on_ready = lambda: self.fired.append(True)


class TestAssetManager(AssetManagerTestBase):

    def test_initial_state_is_downloading(self):
        state = self.manager.state()
        for kind in ("xhs_downloader", "asr_model"):
            self.assertEqual(state[kind]["state"], "downloading")
            self.assertEqual(state[kind]["progress"], 0.0)
            self.assertIsNone(state[kind]["error"])

    def test_state_has_no_private_keys(self):
        # _tried 等内部键绝不能漏进 /api/status。
        for entry in self.manager.state().values():
            for key in entry:
                self.assertFalse(key.startswith("_"))

    def test_prepare_downloads_missing_and_fires_ready(self):
        env = FakeAssetsEnv(self.store, ready=())
        self.manager._check = env.check
        self.manager._install = env.install
        self.manager._prepare()
        self.assertEqual(sorted(k for k, _n in env.install_calls),
                         ["asr_model", "xhs_downloader"])
        state = self.manager.state()
        for kind in ("xhs_downloader", "asr_model"):
            self.assertEqual(state[kind]["state"], "ready")
            self.assertEqual(state[kind]["progress"], 1.0)
        self.assertTrue(self.manager.all_ready())
        self.assertEqual(self.fired, [True])
        # 再跑一轮：都就绪，不重复下载、不重复回调。
        self.manager._prepare()
        self.assertEqual(len(env.install_calls), 2)
        self.assertEqual(self.fired, [True])

    def test_prepare_reports_progress_while_downloading(self):
        env = FakeAssetsEnv(self.store, ready=())
        env.progress_at["asr_model"] = 0.5
        self.manager._check = env.check
        self.manager._install = env.install
        # 只让它汇报进度、别真正结束：直接测 _make_progress 的写入。
        cb = self.manager._make_progress("asr_model")
        with self.manager._lock:
            self.manager._state["asr_model"]["state"] = "downloading"
        cb(0.5)
        self.assertEqual(
            self.manager.state()["asr_model"]["progress"], 0.5)
        cb(1.0)
        self.assertEqual(
            self.manager.state()["asr_model"]["progress"], 1.0)
        # 比例未知（None）：状态里也是 None，页面显示"下载中"。
        cb(None)
        self.assertIsNone(self.manager.state()["asr_model"]["progress"])

    def test_failure_sets_error_code_and_no_ready(self):
        env = FakeAssetsEnv(self.store, ready=())
        env.fail_times["xhs_downloader"] = 99  # 一直失败
        self.manager._check = env.check
        self.manager._install = env.install
        self.manager._prepare()
        state = self.manager.state()
        self.assertEqual(state["xhs_downloader"]["state"], "failed")
        self.assertEqual(state["xhs_downloader"]["error"],
                         "assets_network_error")
        self.assertFalse(self.manager.all_ready())
        self.assertEqual(self.fired, [])
        # 再跑一轮：failed 的资源不会自动重试（等用户点「重试」）。
        self.manager._prepare()
        self.assertEqual(len(env.install_calls), 2)

    def test_retry_resets_failed_and_succeeds(self):
        env = FakeAssetsEnv(self.store, ready=())
        env.fail_times["xhs_downloader"] = 1  # 第一次失败，重试成功
        self.manager._check = env.check
        self.manager._install = env.install
        self.manager._prepare()
        self.assertEqual(
            self.manager.state()["xhs_downloader"]["state"], "failed")
        self.manager.retry()
        self.assertEqual(
            self.manager.state()["xhs_downloader"]["state"], "downloading")
        self.manager._prepare()
        self.assertEqual(
            self.manager.state()["xhs_downloader"]["state"], "ready")
        self.assertTrue(self.manager.all_ready())
        self.assertEqual(self.fired, [True])

    def test_ready_assets_are_not_downloaded(self):
        env = FakeAssetsEnv(self.store, ready=("xhs_downloader",))
        self.manager._check = env.check
        self.manager._install = env.install
        self.manager._prepare()
        self.assertEqual([k for k, _ in env.install_calls], ["asr_model"])


class TestAssetManagerShutdown(AssetManagerTestBase):
    """停止下载的回归：只操作假 curl，不访问网络也不结束外部进程。"""

    def test_default_installs_share_controller_and_stop_it(self):
        """两种资源都使用同一个控制器，stop 才能找到正在下载的进程。"""
        from xhs_wiki import assets

        controller = unittest.mock.Mock()
        manager = AssetManager(self.store, curl=controller)
        progress = unittest.mock.Mock()
        with unittest.mock.patch.object(assets, "install_xhs_downloader") as install_xhs, \
             unittest.mock.patch.object(assets, "install_model") as install_model:
            manager._default_install("xhs_downloader", None, progress)
            manager._default_install("asr_model", "whisper", progress)
        install_xhs.assert_called_once_with(
            self.store.root, progress=progress, downloader=controller.download)
        install_model.assert_called_once_with(
            "whisper", self.store.root, progress=progress,
            downloader=controller.download)
        manager.stop()
        controller.stop.assert_called_once_with()
        self.assertTrue(manager._stop.is_set())
        self.assertTrue(manager._wake.is_set())

    def test_stop_during_first_install_prevents_second_and_ready(self):
        """退出恰好发生在第一项收尾时，不下载第二项，也不自动开跑。"""
        calls = []
        self.manager._check = lambda: ({}, "whisper")

        def finish_during_shutdown(kind, _name, _progress):
            """用同步替身固定停止时机，避免真实线程调度造成偶发结果。"""
            calls.append(kind)
            self.manager.stop()

        self.manager._install = finish_during_shutdown
        self.manager._prepare()
        self.assertEqual(calls, ["xhs_downloader"])
        self.assertFalse(self.manager.all_ready())
        self.assertEqual(self.fired, [])

    def test_stopped_manager_does_not_resume_or_fire_ready(self):
        """停止是最终状态；迟到的重试请求或就绪回调不能重新启动工作。"""
        self.manager._check = unittest.mock.Mock()
        self.manager._install = unittest.mock.Mock()
        self.manager.stop()
        self.manager.retry()
        self.manager._prepare()
        self.manager._fire_ready()
        self.manager._check.assert_not_called()
        self.manager._install.assert_not_called()
        self.assertEqual(self.fired, [])

    def test_stop_terminates_inflight_curl_and_removes_pid(self):
        """通过真实下载控制器管理假进程，核对线程退出、断点保留和 PID 清理。"""
        from xhs_wiki import assets

        running = threading.Event()
        processes = []
        part = self.store.root / ".generated-model.part"
        pid_file = Path(str(part) + ".pid")

        class FakeCurl:
            """只提供 Popen 的生命周期接口，内容和 PID 均由测试生成。"""

            def __init__(self, args, **_kwargs):
                self.pid = 800000 + len(processes)
                self.returncode = None
                self.terminated = False
                processes.append(self)
                Path(args[args.index("--output") + 1]).write_bytes(bytes(range(16)))

            def poll(self):
                """下载进入轮询时通知测试，PID 此时应该已落盘。"""
                running.set()
                return self.returncode

            def terminate(self):
                """假 SIGTERM 只改本替身，不触碰操作系统里的任何进程。"""
                self.terminated = True
                self.returncode = -signal.SIGTERM

            def kill(self):
                """假 SIGKILL 兜底同样只更新替身。"""
                self.returncode = -signal.SIGKILL

            def wait(self, timeout=None):
                """停止后立刻完成等待；没有真实子进程需要回收。"""
                return self.returncode

        controller = assets.CurlDownloadManager(popen=FakeCurl)
        manager = AssetManager(
            self.store, curl=controller, interval=999,
            check=lambda: ({"xhs_downloader": True}, "whisper"))
        manager.on_ready = unittest.mock.Mock()

        def install_model(_name, _home, *, progress, downloader):
            """只使用传入的受控下载器，不解压、不安装、不调用真实模型。"""
            downloader("https://example.invalid/generated-model", part)

        with unittest.mock.patch.object(assets, "install_model", install_model):
            try:
                manager.start()
                self.assertTrue(running.wait(2), "假 curl 未进入下载轮询")
                self.assertTrue(pid_file.is_file())
                manager.stop()
                manager._thread.join(timeout=2)
                self.assertFalse(manager._thread.is_alive())
            finally:
                manager.stop()
                if manager._thread is not None:
                    manager._thread.join(timeout=2)
        self.assertEqual(len(processes), 1)
        self.assertTrue(processes[0].terminated)
        self.assertFalse(pid_file.exists())
        self.assertEqual(part.read_bytes(), bytes(range(16)))
        manager.on_ready.assert_not_called()


class TestServeShutdown(AssetManagerTestBase):
    """用假 HTTP 服务验证退出钩子，不绑定端口、不发送真实进程信号。"""

    def test_sigterm_and_sigint_stop_assets_before_server_close(self):
        """两种信号与普通返回共用 finally，退出后恢复原信号处理器。"""
        for stop_signal in (signal.SIGTERM, signal.SIGINT, None):
            with self.subTest(stop_signal=stop_signal):
                self._exercise_shutdown(stop_signal)

    def test_server_failure_still_stops_assets_and_closes(self):
        """HTTP 主循环异常也执行同一收尾，原错误继续向上传播。"""
        self._exercise_shutdown("failure")

    def _exercise_shutdown(self, stop_signal):
        """记录退出顺序；只替换进程边界，实际执行 serve 的 try/finally。"""
        events = []
        handlers = {signal.SIGTERM: object(), signal.SIGINT: object()}
        original_handlers = dict(handlers)

        def set_handler(signum, handler):
            """假 signal.signal 返回旧处理器，与标准库约定一致。"""
            previous = handlers[signum]
            handlers[signum] = handler
            return previous

        manager = unittest.mock.Mock()
        manager.stop.side_effect = lambda: events.append("assets_stopped")
        supervisor = unittest.mock.Mock()

        class FakeServer:
            """不打开 socket，通过调用注册的处理器模拟系统退出信号。"""

            server_port = daemon.DEFAULT_PORT

            def __init__(self, _address, *, supervisor):
                self.supervisor = supervisor

            def serve_forever(self):
                """进入服务之后才触发退出，覆盖下载线程已经启动的时刻。"""
                manager.start.assert_called_once_with()
                events.append("serving")
                if stop_signal == "failure":
                    raise RuntimeError("generated_service_failure")
                if stop_signal is not None:
                    handlers[stop_signal](stop_signal, None)

            def server_close(self):
                """关闭前资源已经停掉；收尾期间重复信号被暂时忽略。"""
                self_outer.assertEqual(events[-1], "assets_stopped")
                self_outer.assertTrue(all(value == signal.SIG_IGN
                                          for value in handlers.values()))
                events.append("server_closed")

        self_outer = self
        with unittest.mock.patch.object(daemon.os, "chdir"), \
             unittest.mock.patch.object(daemon.signal, "signal", set_handler), \
             unittest.mock.patch.object(daemon, "Supervisor", return_value=supervisor), \
             unittest.mock.patch.object(daemon, "AssetManager", return_value=manager), \
             unittest.mock.patch.object(daemon, "ServiceServer", FakeServer), \
             unittest.mock.patch.object(daemon, "remove_stale_login_qr"):
            if stop_signal == "failure":
                with self.assertRaisesRegex(RuntimeError, "^generated_service_failure$"):
                    daemon.serve()
            else:
                daemon.serve()
        self.assertEqual(events, ["serving", "assets_stopped", "server_closed"])
        self.assertEqual(handlers, original_handlers)


# ---------------------------------------------------------------------------
# 带资源线程的守护进程（真实 HTTP 服务，端口 0）
# ---------------------------------------------------------------------------

class FakeProcess:
    def __init__(self):
        self.exit_code = None

    def poll(self):
        return self.exit_code


class FakeSpawner:
    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        return FakeProcess()


class FakeKeychain:
    def __init__(self):
        self.saved = {}

    def save(self, account, key):
        self.saved[account] = key

    def load(self, account):
        from xhs_wiki.llm.base import LLMError
        if account not in self.saved:
            raise LLMError("auth_failed", "missing")
        return self.saved[account]


class FakeProvider:
    def check(self):
        return True, "claude_code_logged_in"


class FakeEngine:
    def health(self):
        return True, "ok"


class AssetsHttpTestBase(unittest.TestCase):
    """真实 ServiceServer（端口 0）+ 可手动驱动的 AssetManager。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "app"
        os.environ["XHS_WIKI_HOME"] = str(self.home)
        os.environ["XHS_WIKI_LOG_DIR"] = str(Path(self.tmp.name) / "logs")
        self.store = ServiceStore(self.home)
        self.store.write_scheduler(
            last_scheduled_date=AFTERNOON.date().isoformat())
        self.spawner = FakeSpawner()
        self.env = FakeAssetsEnv(self.store, ready=())
        self.manager = AssetManager(self.store, interval=999,
                                    check=self.env.check,
                                    install=self.env.install)
        self.supervisor = Supervisor(
            self.store, spawn=self.spawner, now=lambda: AFTERNOON,
            notify=lambda code: None, assets=self.manager)
        self.manager.on_ready = self.supervisor.on_assets_ready
        self.server = ServiceServer(
            ("127.0.0.1", 0), supervisor=self.supervisor,
            ledger_factory=lambda: Ledger(self.home / "ledger.sqlite3"),
            keychain=FakeKeychain(),
            make_provider=lambda settings: FakeProvider(),
            make_engine=lambda settings: FakeEngine(),
            opener=lambda args: None,
            disk_usage=lambda path: shutil.disk_usage(self.tmp.name),
            skills_root=Path(self.tmp.name) / "skills")
        self.port = self.server.server_port
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.manager.stop)

    # ---------------- HTTP 小工具（与 test_daemon 相同的规矩） ----------------

    def request(self, method, path, body=None):
        # 除 /healthz、/auth 外都要带访问 Cookie（经真实 /auth 换来）。
        if getattr(self, "_cookie", None) is None:
            from test_daemon import server_cookie
            self._cookie = server_cookie(self.server)
        headers = {"Host": f"127.0.0.1:{self.port}", "Cookie": self._cookie}
        if method == "POST":
            status, page = self.request("GET", "/")
            token = re.search(rb'content="([0-9a-f]{64})"', page).group(1)
            headers.update({
                "Origin": f"http://127.0.0.1:{self.port}",
                "X-CSRF-Token": token.decode(),
                "Content-Type": "application/json",
            })
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            payload = json.dumps(body or {}) if method == "POST" else None
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def get_status(self):
        status, raw = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        return json.loads(raw)

    def ready_settings(self):
        kb = Path(self.tmp.name) / "kb"
        kb.mkdir(exist_ok=True)
        self.store.update_settings({"kb_root": str(kb)})
        self.store.update_settings({"profile_url": FAKE_PROFILE_URL})


class TestAssetsStatusField(AssetsHttpTestBase):

    def test_status_contains_assets_progress(self):
        data = self.get_status()
        self.assertIn("assets", data)
        for kind in ("xhs_downloader", "asr_model"):
            entry = data["assets"][kind]
            self.assertEqual(entry["state"], "downloading")
            self.assertIn("progress", entry)
            self.assertIn("error", entry)

    def test_status_shows_total_bytes_for_model(self):
        data = self.get_status()
        # 还没开始下载时 total_bytes 未知：开始下载后由 _prepare 填上。
        self.assertIsNone(data["assets"]["xhs_downloader"]["total_bytes"])

    def test_default_assets_state_without_manager(self):
        self.supervisor.assets = None
        data = self.get_status()
        for kind in ("xhs_downloader", "asr_model"):
            self.assertEqual(data["assets"][kind]["state"], "ready")


class TestRunDeferredUntilAssetsReady(AssetsHttpTestBase):

    def test_run_now_deferred_with_needs_user(self):
        status, body = self.request("POST", "/api/run-now")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        # 没有拉起子进程：资源还没就绪。
        self.assertEqual(self.spawner.calls, [])
        data = self.get_status()
        codes = [i["code"] for i in data["status"]["needs_user"]]
        self.assertIn("assets_not_ready", codes)
        self.assertEqual(data["status"]["state"], "needs_user")
        self.assertIn("自动开始", data["status"]["message"])

    def test_auto_run_once_after_ready(self):
        status, _ = self.request("POST", "/api/run-now")
        self.assertEqual(self.spawner.calls, [])
        # 模拟后台线程下载完成：install 立即成功，_prepare 的末尾判定
        # 全部就绪并回调 on_ready → Supervisor 自动补跑被拦下的那次运行。
        self.manager._prepare()
        self.assertTrue(self.manager.all_ready())
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertEqual(self.spawner.calls[0][
            self.spawner.calls[0].index("--trigger") + 1], "manual")
        # needs_user 里的 assets_not_ready 已被清掉。
        data = self.get_status()
        codes = [i["code"] for i in data["status"]["needs_user"]]
        self.assertNotIn("assets_not_ready", codes)
        # 就绪后再点「立即运行」：正常直接开跑（不再被拦）。
        status, _ = self.request("POST", "/api/run-now")
        self.assertEqual(status, 409)  # 上面那次还在"运行"（假进程活着）

    def test_run_not_deferred_when_assets_absent(self):
        self.supervisor.assets = None
        status, _ = self.request("POST", "/api/run-now")
        self.assertEqual(status, 200)
        self.assertEqual(len(self.spawner.calls), 1)

    def test_run_not_deferred_after_ready(self):
        self.manager._prepare()
        self.assertTrue(self.manager.all_ready())
        status, _ = self.request("POST", "/api/run-now")
        self.assertEqual(status, 200)
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertIn("manual", self.spawner.calls[0])


class TestAssetsRetryEndpoint(AssetsHttpTestBase):

    def test_retry_409_without_manager(self):
        self.supervisor.assets = None
        status, body = self.request("POST", "/api/assets/retry")
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "assets_not_managed")

    def test_retry_resets_failed_state(self):
        self.env.fail_times["xhs_downloader"] = 99
        self.manager._prepare()
        self.assertEqual(
            self.manager.state()["xhs_downloader"]["state"], "failed")
        status, body = self.request("POST", "/api/assets/retry")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["assets"]["xhs_downloader"]["state"],
                         "downloading")


class TestFinishWithAssets(AssetsHttpTestBase):

    def test_finish_deferred_and_clears_setup_incomplete(self):
        self.ready_settings()
        # 先放一条旧的 setup_incomplete 事项（上次运行预检留下的）。
        self.store.add_needs_user("setup_incomplete", "首次设置还没有完成",
                                  now=AFTERNOON)
        status, body = self.request("POST", "/api/setup/finish")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["setup_completed"])
        # 资源没就绪：页面要提示"组件下载完成后会自动开始导入"。
        self.assertFalse(data["assets_ready"])
        # 运行被拦下（没拉子进程），但 setup_incomplete 已立即清掉。
        self.assertEqual(self.spawner.calls, [])
        codes = [i["code"] for i in self.get_status()["status"]["needs_user"]]
        self.assertNotIn("setup_incomplete", codes)
        self.assertIn("assets_not_ready", codes)

    def test_finish_returns_assets_ready_when_ready(self):
        self.ready_settings()
        self.manager._prepare()
        status, body = self.request("POST", "/api/setup/finish")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["assets_ready"])
        self.assertEqual(len(self.spawner.calls), 1)


class TestConsoleHtmlContent(unittest.TestCase):
    """console.html 的资源准备条等改动（字符串断言，页面行为靠浏览器保证）。"""

    def setUp(self):
        import xhs_wiki.runner as runner_module
        self.html = (Path(runner_module.__file__).resolve().parent
                     / "web" / "console.html").read_text(encoding="utf-8")

    def test_assets_banner_and_retry_button_exist(self):
        self.assertIn('id="assets-bar"', self.html)
        self.assertIn('id="assets-retry"', self.html)
        self.assertIn("/api/assets/retry", self.html)

    def test_plain_code_table_covers_examples(self):
        # 大白话对照表存在，且包含几个典型例子。
        self.assertIn("CODE_LABELS", self.html)
        self.assertIn("claude_code_logged_in:'Claude Code 已登录，可以使用'"
                      .replace(" ", ""), self.html.replace(" ", ""))
        self.assertIn("function plainCode", self.html)

    def test_kb_root_input_disabled_until_loaded(self):
        self.assertIn('id="w-kb-root" value="~/XHS-Wiki" disabled', self.html)

    def test_qr_refresh_label_and_one_qr_place(self):
        self.assertIn("重新获取二维码", self.html)
        # 旧的"二维码生成中…"占位空框不应再出现。
        self.assertNotIn("二维码生成中", self.html)

    def test_finish_hint_when_assets_not_ready(self):
        self.assertIn("组件下载完成后会自动开始导入", self.html)


if __name__ == "__main__":
    unittest.main()
