# -*- coding: utf-8 -*-
"""test_daemon.py —— 守护进程、控制台接口和首次向导的测试。

控制台要求访问 Cookie：HTTP 请求统一先经真实的 POST /auth（测试
辅助函数 auth_cookie / server_cookie）换 Cookie 再发；访问控制本身的
测试见 tests/test_y2_console_security.py。

覆盖四块：
1. HTTP 层（ServiceServer/ServiceHandler）：临时目录里起真实的服务（端口 0
   随机分配），用 http.client 发请求；spawn/钥匙串/模型/引擎/打开命令全部
   换成假实现，绝不启动真实子进程、不碰真实钥匙串、不调真实模型；
2. 调度层（Supervisor.tick / 收尾 / 启动清理）：直接调用方法传固定时间；
3. 首次向导的各个 POST 接口：校验、Key 不泄漏、费用确认、finish 条件；
4. cli 的 print-launch-agent（plist 可解析、路径不在 ~/Documents）和
   doctor（不创建任何目录）。

红线自查：全程只用临时目录（XHS_WIKI_HOME / XHS_WIKI_LOG_DIR 指到
tempfile），不访问网络、不启动浏览器、不调用真实模型。
"""

import contextlib
import fcntl
import http.client
import io
import json
import os
import plistlib
import re
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

from xhs_wiki import cli, config, console_auth, daemon
from xhs_wiki.daemon import ServiceServer, Supervisor
from xhs_wiki.ledger import Ledger
from xhs_wiki.llm.base import LLMError
from xhs_wiki.store import ServiceStore, now_beijing

# 北京时间（所有测试时间基于同一个"今天"，避免跨天边界抖动）。
TODAY = now_beijing()
MORNING = TODAY.replace(hour=3, minute=0, second=0, microsecond=0)
AFTERNOON = TODAY.replace(hour=12, minute=0, second=0, microsecond=0)

# 虚构的测试数据（红线：不用任何真实编号）。
FAKE_PROFILE_URL = "https://www.xiaohongshu.com/user/profile/" + "ab" * 12
FAKE_NOTE_IDS = ["%024x" % i for i in range(1, 4)]  # 24 位十六进制
FAKE_KEY = "test-key-never-real-0001"


def auth_cookie(port: int, token: str) -> str:
    """测试辅助：走一遍真实的 POST /auth，返回 Cookie 请求头要用的"名字=值"。

    除 /healthz 和 /auth 外所有路由都要带访问 Cookie。
    测试和真实用户一样，用访问令牌（APP_HOME/console-token 里的值）
    换 Cookie，而不是直接拼 Cookie——这样 /auth 本身也被反复验证到。
    """
    body = urlencode({"token": token})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("POST", "/auth", body=body, headers={
            "Host": f"127.0.0.1:{port}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body.encode()))})
        response = conn.getresponse()
        response.read()
        assert response.status == 303, f"/auth 返回 {response.status}"
        set_cookie = response.getheader("Set-Cookie") or ""
        return set_cookie.split(";", 1)[0]
    finally:
        conn.close()


def server_cookie(server) -> str:
    """测试辅助：从服务的数据目录读访问令牌，经 /auth 换成 Cookie。"""
    token = console_auth.read_token(server.supervisor.store.root)
    return auth_cookie(server.server_port, token)


class FakeProcess:
    """假的子进程句柄：poll 返回 None 表示"活着"，0 表示"已退出"。"""

    def __init__(self, exit_code=None):
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code


class FakeSpawner:
    """假的 spawn：记录每次被要求启动的命令行，返回 FakeProcess。"""

    def __init__(self):
        self.calls = []
        self.procs = []

    def __call__(self, args):
        self.calls.append(list(args))
        proc = FakeProcess()  # 默认"活着"
        self.procs.append(proc)
        return proc


class FakeKeychain:
    """假的钥匙串：内存 dict。load 不到抛 LLMError，绝不碰真实钥匙串。"""

    def __init__(self):
        self.saved = {}
        self.load_calls = []

    def save(self, account, key):
        self.saved[account] = key

    def load(self, account):
        self.load_calls.append(account)
        if account not in self.saved:
            raise LLMError("auth_failed", "keychain_item_missing_44")
        return self.saved[account]


class FakeProvider:
    """假的模型提供方：check() 的结果可配置。"""

    def __init__(self, ok=True, reason="ok"):
        self.ok = ok
        self.reason = reason
        self.checked = 0

    def check(self):
        self.checked += 1
        return self.ok, self.reason


class FakeEngine:
    """假的知识库引擎：health() 的结果可配置。"""

    def __init__(self, ok=True, reason="ok"):
        self.ok = ok
        self.reason = reason

    def health(self):
        return self.ok, self.reason


class DaemonTestBase(unittest.TestCase):
    """公共环境：临时目录 + 假依赖 + 真实 HTTP 服务（0 端口随机分配）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "app"
        self.log_dir = Path(self.tmp.name) / "logs"
        # 环境变量先于一切 config 调用生效（绝不在真实主目录建任何文件）。
        os.environ["XHS_WIKI_HOME"] = str(self.home)
        os.environ["XHS_WIKI_LOG_DIR"] = str(self.log_dir)
        self.store = ServiceStore(self.home)
        # 知识库位置一开始就指到临时目录。默认值是 ~/XHS-Wiki
        # （真实主目录）：向导"换位置"的测试会把旧位置当成要搬走的知识库，
        # 开发者本机要是真有一个带 .xhs-wiki/ 标记的 ~/XHS-Wiki，会被搬进
        # 临时目录、测试结束时随临时目录一起删掉。
        self.store.update_settings(
            {"kb_root": str(Path(self.tmp.name) / "default-kb")})
        self.spawner = FakeSpawner()
        self.notified = []
        self.supervisor = Supervisor(
            self.store, spawn=self.spawner, now=lambda: AFTERNOON,
            notify=lambda code: self.notified.append(code))
        self.keychain = FakeKeychain()
        self.provider = FakeProvider()
        self.opener_calls = []
        self.server = ServiceServer(
            ("127.0.0.1", 0), supervisor=self.supervisor,
            ledger_factory=lambda: Ledger(self.home / "ledger.sqlite3"),
            keychain=lambda: self.keychain,
            make_provider=lambda settings: self.provider,
            make_engine=lambda settings: FakeEngine(),
            opener=lambda args: self.opener_calls.append(list(args)),
            disk_usage=lambda path: shutil.disk_usage(self.tmp.name),
            # Skill 安装位置指到临时目录（sync_skill 会真的写文件）。
            skills_root=Path(self.tmp.name) / "skills")
        # 默认把"今天已经定时跑过"写上：只测各自关注的逻辑。
        self.store.write_scheduler(
            last_scheduled_date=AFTERNOON.date().isoformat())
        self.port = self.server.server_port
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    # ---------------- HTTP 小工具 ----------------

    def auth_cookie(self) -> str:
        """经真实的 /auth 换一次访问 Cookie，本测试内缓存复用。"""
        if getattr(self, "_auth_cookie", None) is None:
            self._auth_cookie = server_cookie(self.server)
        return self._auth_cookie

    def request(self, method, path, *, host=None, origin="same",
                token="page", content_type="application/json", body=None,
                cookie="auth"):
        """发一个请求，返回 (状态码, 响应体字节)。

        origin="same" 表示带正确的 Origin；None 表示不带。
        token="page" 表示从控制台页面里取真令牌；None 不带。
        cookie="auth" 表示带上经 /auth 换来的访问 Cookie；None 不带；
        其他字符串原样作为 Cookie 请求头。取 CSRF 令牌时总是带合法 Cookie
        （没有 Cookie 拿不到页面），这样能单独测"有 CSRF、无 Cookie"的情况。
        """
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if origin == "same":
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        elif origin is not None:
            headers["Origin"] = origin
        if cookie == "auth":
            headers["Cookie"] = self.auth_cookie()
        elif cookie is not None:
            headers["Cookie"] = cookie
        real_token = None
        if token == "page":
            status, body_bytes = self.request("GET", "/", token=None)
            match = re.search(rb'name="csrf-token" content="([0-9a-f]{64})"',
                              body_bytes)
            real_token = match.group(1).decode()
        else:
            real_token = token
        if real_token is not None:
            headers["X-CSRF-Token"] = real_token
        payload = None
        if method == "POST":
            headers["Content-Type"] = content_type
            payload = body if isinstance(body, str) else json.dumps(body or {})
            headers["Content-Length"] = str(len(payload.encode()))
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def post(self, path, **kwargs):
        status, body = self.request("POST", path, **kwargs)
        try:
            return status, json.loads(body.decode("utf-8"))
        except ValueError:
            return status, body

    def add_failed_note(self, note_id=FAKE_NOTE_IDS[0], title="测试笔记",
                        op="download", error="network_error"):
        """往台账里塞一条停在 failed:<环节> 的笔记（供失败列表和重试用）。"""
        # 服务端不缓存台账连接，测试也随开随关（ledger_scope）。
        with self.server.ledger_scope() as ledger:
            ledger.upsert_discovered([{
                "note_id": note_id, "title": title,
                "url": "https://www.xiaohongshu.com/explore/" + note_id,
                "list_position": 0,
            }])
            note = ledger.get(note_id)
            with ledger._conn:
                ledger._conn.execute(
                    "UPDATE notes SET stage=?, attempts=3, last_error=? WHERE note_id=?",
                    ("failed:" + op, error, note_id))
        return note_id


# ---------------------------------------------------------------------------
# GET 接口
# ---------------------------------------------------------------------------

class TestGetRoutes(DaemonTestBase):

    def test_console_page_has_token_no_placeholder(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertNotIn("{{CSRF_TOKEN}}", text)
        self.assertRegex(text, r'name="csrf-token" content="[0-9a-f]{64}"')
        # 页面不引入外部资源（红线：控制台完全离线可用）。
        self.assertNotIn("http://", text.replace("http://127.0.0.1", ""))
        self.assertNotIn("https://cdn", text)

    def test_status_structure(self):
        status, data = self.request("GET", "/api/status")
        data = json.loads(data)
        self.assertEqual(status, 200)
        for key in ("status", "settings", "next_run_at", "runs", "counts", "kb",
                    "failed_notes", "setup_completed", "llm", "engine",
                    "cleanup_total_bytes", "disk", "login", "step_labels"):
            self.assertIn(key, data)
        self.assertFalse(data["setup_completed"])
        self.assertEqual(data["engine"], "markdown")
        self.assertEqual(data["llm"]["provider"], "claude_code")
        self.assertIn("key_saved", data["llm"])

    def test_status_counts_from_ledger(self):
        self.add_failed_note()
        status, data = self.request("GET", "/api/status")
        data = json.loads(data)
        self.assertEqual(data["counts"].get("failed:download"), 1)
        self.assertEqual(len(data["failed_notes"]), 1)
        note = data["failed_notes"][0]
        self.assertEqual(note["note_id"], FAKE_NOTE_IDS[0])
        self.assertEqual(note["stage"], "failed:download")
        self.assertEqual(note["error"], "network_error")

    def test_status_no_key_and_no_signed_url(self):
        self.keychain.save("anthropic", FAKE_KEY)
        self.store.update_settings({"llm": {"provider": "anthropic",
                                            "model": "m", "base_url": None}})
        status, data = self.request("GET", "/api/status")
        text = data.decode("utf-8")
        self.assertEqual(status, 200)
        # Key 只出现 key_saved 布尔，不出现 Key 本身。
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn("xsec_token", text)

    def test_status_key_saved_flag(self):
        _, data = self.request("GET", "/api/status")
        data = json.loads(data)
        self.assertFalse(data["llm"]["key_saved"])  # 钥匙串（假的）还没有 Key
        self.keychain.save("anthropic", FAKE_KEY)
        self.store.update_settings({"llm": {"provider": "anthropic",
                                            "model": "m", "base_url": None}})
        _, data = self.request("GET", "/api/status")
        self.assertTrue(json.loads(data)["llm"]["key_saved"])

    def test_login_qr_404_when_no_flow(self):
        status, _ = self.request("GET", "/api/login-qr.png")
        self.assertEqual(status, 404)

    def test_status_with_missing_kb_root_and_real_disk_usage(self):
        # 首次设置之前知识库目录还不存在：status 不能崩（往上找祖先目录查磁盘）。
        self.server.disk_usage = shutil.disk_usage
        self.store.update_settings(
            {"kb_root": str(Path(self.tmp.name) / "no" / "such" / "kb")})
        status, data = self.request("GET", "/api/status")
        data = json.loads(data)
        self.assertEqual(status, 200)
        self.assertIsNotNone(data["disk"]["free_gb"])

    def test_unknown_route_404(self):
        status, _ = self.request("GET", "/api/unknown")
        self.assertEqual(status, 404)


# ---------------------------------------------------------------------------
# POST 校验：任何一项不满足都要被拒绝，且不拉起任何子进程
# ---------------------------------------------------------------------------

class TestPostValidation(DaemonTestBase):

    def assert_rejected_no_spawn(self, status):
        self.assertIn(status, (400, 403, 409))
        self.assertEqual(self.spawner.calls, [])

    def test_post_missing_token_rejected(self):
        status, _ = self.post("/api/run-now", token=None)
        self.assert_rejected_no_spawn(status)

    def test_post_wrong_token_rejected(self):
        status, _ = self.post("/api/run-now", token="0" * 64)
        self.assert_rejected_no_spawn(status)

    def test_post_missing_origin_rejected(self):
        status, _ = self.post("/api/run-now", origin=None)
        self.assert_rejected_no_spawn(status)

    def test_post_wrong_origin_rejected(self):
        status, _ = self.post("/api/run-now", origin="http://evil.example")
        self.assert_rejected_no_spawn(status)

    def test_post_wrong_content_type_rejected(self):
        status, _ = self.post("/api/run-now", content_type="text/plain")
        self.assert_rejected_no_spawn(status)

    def test_post_oversized_body_rejected(self):
        big = json.dumps({"daily_time": "03:00", "pad": "x" * 5000})
        status, _ = self.post("/api/settings", body=big)
        self.assert_rejected_no_spawn(status)

    def test_post_wrong_host_rejected(self):
        status, _ = self.post("/api/run-now", host="evil.example:1")
        self.assert_rejected_no_spawn(status)

    def test_post_unknown_route_404(self):
        status, _ = self.post("/api/whatever")
        self.assertEqual(status, 404)
        self.assertEqual(self.spawner.calls, [])


# ---------------------------------------------------------------------------
# POST 动作（日常页）
# ---------------------------------------------------------------------------

class TestPostActions(DaemonTestBase):

    def test_run_now_once_then_409(self):
        status, data = self.post("/api/run-now")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertIn("run", self.spawner.calls[0])
        self.assertIn("manual", self.spawner.calls[0])
        status, data = self.post("/api/run-now")
        self.assertEqual(status, 409)
        self.assertEqual(data.get("error"), "already_running")
        self.assertEqual(len(self.spawner.calls), 1)

    def test_pause_creates_marker(self):
        status, _ = self.post("/api/pause")
        self.assertEqual(status, 200)
        self.assertTrue((self.home / "PAUSE").exists())

    def test_resume_clears_marker_and_starts(self):
        self.store.request_pause()
        status, _ = self.post("/api/resume")
        self.assertEqual(status, 200)
        self.assertFalse((self.home / "PAUSE").exists())
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertIn("resume", self.spawner.calls[0])

    def test_settings_valid_returns_next_run(self):
        status, data = self.post("/api/settings", body={"daily_time": "04:30"})
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        self.assertEqual(self.store.load_settings()["daily_time"], "04:30")
        self.assertIsInstance(data.get("next_run_at"), str)

    def test_settings_invalid_value_400_with_code(self):
        status, data = self.post("/api/settings", body={"daily_time": "25:00"})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "daily_time_invalid")
        self.assertEqual(self.store.load_settings()["daily_time"], "03:00")

    def test_settings_non_editable_field_400(self):
        status, data = self.post("/api/settings", body={"headless": True})
        self.assertEqual(status, 400)
        self.assertIn("field_not_editable", data.get("error", ""))

    def test_login_starts_child(self):
        status, data = self.post("/api/login")
        self.assertEqual(status, 200)
        self.assertTrue(data["login"]["active"])
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertIn("login", self.spawner.calls[0])

    def test_login_rejected_while_scan_step(self):
        self.store.write_status(now=AFTERNOON, state="running", step="scan")
        status, data = self.post("/api/login")
        self.assertEqual(status, 409)
        self.assertEqual(data.get("error"), "scan_in_progress")


# ---------------------------------------------------------------------------
# Supervisor.tick
# ---------------------------------------------------------------------------

class TestSupervisorTick(DaemonTestBase):

    def test_tick_daily_runs_once_per_day(self):
        self.store.write_scheduler(last_scheduled_date=None)
        self.supervisor.tick(AFTERNOON)  # 12:00 > 03:00，还没跑过 → 开跑
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertIn("schedule", self.spawner.calls[0])
        self.assertEqual(
            self.store.read_scheduler()["last_scheduled_date"],
            AFTERNOON.date().isoformat())
        self.supervisor.tick(AFTERNOON + timedelta(minutes=1))
        self.assertEqual(len(self.spawner.calls), 1)

    def test_tick_not_due_before_daily_time(self):
        self.store.write_scheduler(last_scheduled_date=None)
        early = MORNING - timedelta(hours=1)  # 02:00 < 03:00
        self.supervisor.tick(early)
        self.assertEqual(self.spawner.calls, [])

    def test_tick_followup_after_60_minutes(self):
        ended = AFTERNOON - timedelta(hours=2)
        self.store.append_run({"run_id": "svc-x", "trigger": "manual",
                               "started_at": ended.isoformat(),
                               "ended_at": ended.isoformat(),
                               "result": "incomplete"})
        self.supervisor.tick(ended + timedelta(minutes=30))
        self.assertEqual(self.spawner.calls, [])
        self.supervisor.tick(ended + timedelta(minutes=61))
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertIn("followup", self.spawner.calls[0])
        self.assertEqual(self.store.read_scheduler()["followups"], 1)

    def test_tick_respects_pause(self):
        self.store.request_pause()
        self.store.write_scheduler(last_scheduled_date=None)
        self.supervisor.tick(AFTERNOON)
        self.assertEqual(self.spawner.calls, [])

    def test_crashed_child_gets_error_record(self):
        proc = FakeProcess(exit_code=1)

        def spawn_exited(args):
            self.spawner.calls.append(list(args))
            return proc

        self.supervisor.spawn = spawn_exited
        self.assertTrue(self.supervisor.start_run("manual"))
        self.supervisor.tick(AFTERNOON)
        runs = self.store.list_runs(5)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["result"], "error")
        self.assertEqual(runs[0]["stop_reason"], "runner_crashed")
        self.assertEqual(self.store.read_status()["state"], "error")
        self.assertIn("error", self.notified)

    def test_exited_child_with_record_not_duplicated(self):
        proc = FakeProcess(exit_code=0)
        self.supervisor.spawn = lambda args: proc
        self.store.append_run({"run_id": "svc-ok", "trigger": "manual",
                               "started_at": (AFTERNOON + timedelta(seconds=1)).isoformat(),
                               "ended_at": (AFTERNOON + timedelta(seconds=2)).isoformat(),
                               "result": "completed"})
        self.assertTrue(self.supervisor.start_run("manual"))
        self.supervisor.tick(AFTERNOON + timedelta(seconds=5))
        self.assertEqual(len(self.store.list_runs(5)), 1)
        self.assertNotIn("error", self.notified)

    def test_xhs_login_autologin_throttled(self):
        self.store.add_needs_user("xhs_login", "收藏页要求登录", now=AFTERNOON)
        self.supervisor.tick(AFTERNOON)
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertIn("login", self.spawner.calls[0])
        # 扫码进程还在进行（假进程活着）：6 小时内不重复拉起。
        self.supervisor.tick(AFTERNOON + timedelta(hours=1))
        self.assertEqual(len(self.spawner.calls), 1)

    def test_no_login_spawn_while_scan_step(self):
        self.store.add_needs_user("xhs_login", "test", now=AFTERNOON)
        self.store.write_status(now=AFTERNOON, state="running", step="scan")
        self.supervisor.tick(AFTERNOON)
        self.assertEqual(self.spawner.calls, [])


# ---------------------------------------------------------------------------
# 扫码成功后自动开跑的前提（有 xhs_login 事项 + profile_url 已写回）
# ---------------------------------------------------------------------------

class TestLoginAutoRunPrecondition(DaemonTestBase):

    def test_no_auto_run_when_login_spawned_without_xhs_login(self):
        self.store.update_settings({"profile_url": FAKE_PROFILE_URL})
        self.assertTrue(self.supervisor.start_login())
        self.spawner.procs[0].exit_code = 0
        self.supervisor.tick(AFTERNOON + timedelta(minutes=1))
        self.assertEqual(len(self.spawner.calls), 1)  # 没有自动开跑

    def test_no_auto_run_when_xhs_login_still_present(self):
        self.store.add_needs_user("xhs_login", "test", now=AFTERNOON)
        self.store.update_settings({"profile_url": FAKE_PROFILE_URL})
        self.supervisor.tick(AFTERNOON)
        self.spawner.procs[0].exit_code = 1
        self.supervisor.tick(AFTERNOON + timedelta(minutes=1))
        self.assertEqual(len(self.spawner.calls), 1)

    def test_no_auto_run_without_profile_url(self):
        # 事项被清了，但 login 子命令没写回 profile_url：说明没真的登录。
        self.store.add_needs_user("xhs_login", "test", now=AFTERNOON)
        self.supervisor.tick(AFTERNOON)
        self.spawner.procs[0].exit_code = 0
        self.store.clear_needs_user("xhs_login")
        self.supervisor.tick(AFTERNOON + timedelta(minutes=1))
        self.assertEqual(len(self.spawner.calls), 1)

    def test_auto_run_when_login_succeeds_with_profile_url(self):
        self.store.add_needs_user("xhs_login", "test", now=AFTERNOON)
        self.store.update_settings({"profile_url": FAKE_PROFILE_URL})
        self.supervisor.tick(AFTERNOON)
        self.spawner.procs[0].exit_code = 0
        self.store.clear_needs_user("xhs_login")
        self.supervisor.tick(AFTERNOON + timedelta(minutes=1))
        self.assertEqual(len(self.spawner.calls), 2)
        self.assertIn("after_login", self.spawner.calls[1])


# ---------------------------------------------------------------------------
# 残留二维码的三个删除时机
# ---------------------------------------------------------------------------

class TestStaleLoginQrCleanup(DaemonTestBase):

    def write_stale_qr(self):
        qr = self.store.root / "login-qr.png"
        qr.write_bytes(b"stale qr png")
        return qr

    def test_startup_cleanup_removes_stale_qr(self):
        qr = self.write_stale_qr()
        daemon.remove_stale_login_qr(self.store)
        self.assertFalse(qr.exists())
        daemon.remove_stale_login_qr(self.store)  # 不存在时也不报错

    def test_start_login_removes_stale_qr_before_spawn(self):
        qr = self.write_stale_qr()
        self.assertTrue(self.supervisor.start_login())
        self.assertFalse(qr.exists())
        self.assertEqual(len(self.spawner.calls), 1)

    def test_reap_login_child_removes_stale_qr(self):
        qr = self.write_stale_qr()
        self.supervisor._login_proc = FakeProcess(exit_code=0)
        self.supervisor._reap_login_child(AFTERNOON)
        self.assertFalse(qr.exists())


# ---------------------------------------------------------------------------
# 启动清理（假运行中状态）
# ---------------------------------------------------------------------------

class TestStartupCleanup(unittest.TestCase):

    def make_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return ServiceStore(Path(tmp.name) / "service")

    def test_stale_running_state_cleaned(self):
        store = self.make_store()
        store.write_status(now=AFTERNOON, state="running", step="scan")
        notified = []
        supervisor = Supervisor(store, now=lambda: AFTERNOON,
                                notify=lambda code: notified.append(code))
        supervisor.cleanup_stale_running()
        status = store.read_status()
        self.assertEqual(status["state"], "error")
        self.assertIn("重新开始", status["message"])
        self.assertIn("error", notified)

    def test_stale_cleanup_skipped_when_lock_held(self):
        store = self.make_store()
        store.write_status(now=AFTERNOON, state="running", step="scan")
        notified = []
        supervisor = Supervisor(store, now=lambda: AFTERNOON,
                                notify=lambda code: notified.append(code))
        lock_fd = os.open(store.root / "runner.lock",
                          os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            supervisor.cleanup_stale_running()
            self.assertEqual(store.read_status()["state"], "running")
            self.assertEqual(notified, [])
        finally:
            os.close(lock_fd)


# ---------------------------------------------------------------------------
# 子进程命令行：必须带上守护进程自己的数据目录（--home）
# ---------------------------------------------------------------------------

class TestChildArgsHome(DaemonTestBase):

    def test_run_child_args_include_home(self):
        args = self.supervisor.run_child_args("manual")
        self.assertIn("--home", args)
        self.assertEqual(args[args.index("--home") + 1], str(self.home))
        self.assertIn("run", args)
        self.assertIn("manual", args)
        self.assertIn("-m", args)
        self.assertIn("xhs_wiki.cli", args)

    def test_login_child_args_include_home(self):
        args = self.supervisor.login_child_args()
        self.assertIn("--home", args)
        self.assertEqual(args[args.index("--home") + 1], str(self.home))
        self.assertIn("login", args)

    def test_default_spawn_closes_parent_log_handle(self):
        """_default_spawn 拉起子进程后要关掉父进程手里的日志句柄。"""
        opened = []
        real_popen = daemon.subprocess.Popen

        def fake_popen(args, **kwargs):
            opened.append(kwargs["stdout"])
            return FakeProcess()

        daemon.subprocess.Popen = fake_popen
        try:
            self.supervisor._default_spawn(["echo", "hi"])
        finally:
            daemon.subprocess.Popen = real_popen
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].closed)
        # 日志写在 XHS_WIKI_LOG_DIR 指定的临时目录里，不碰 ~/Library。
        self.assertTrue((self.log_dir / "daemon.log").exists())


# ---------------------------------------------------------------------------
# 首次向导：/api/setup/kb-root
# ---------------------------------------------------------------------------

class TestSetupKbRoot(DaemonTestBase):

    def test_kb_root_created_and_saved(self):
        target = Path(self.tmp.name) / "kb" / "我的库"
        status, data = self.post("/api/setup/kb-root", body={"path": str(target)})
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        self.assertTrue(target.is_dir())
        self.assertEqual(self.store.load_settings()["kb_root"], str(target))

    def test_kb_root_relative_rejected(self):
        status, data = self.post("/api/setup/kb-root", body={"path": "relative/kb"})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "kb_root_not_absolute")

    def test_kb_root_inside_app_home_rejected(self):
        status, data = self.post("/api/setup/kb-root",
                                 body={"path": str(self.home / "kb")})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "kb_root_inside_app_home")
        self.assertFalse((self.home / "kb").exists())  # 而且没建目录

    def test_kb_root_not_writable_rejected(self):
        root = Path(self.tmp.name) / "ro"
        root.mkdir()
        os.chmod(root, 0o500)
        try:
            status, data = self.post("/api/setup/kb-root",
                                     body={"path": str(root / "kb")})
            self.assertEqual(status, 400)
            self.assertEqual(data.get("error"), "kb_root_not_writable")
        finally:
            os.chmod(root, 0o700)

    def test_kb_root_missing_path_rejected(self):
        status, data = self.post("/api/setup/kb-root", body={"path": "  "})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "kb_root_invalid")


# ---------------------------------------------------------------------------
# 首次向导：/api/setup/engine、llm、key、test-llm、first-import
# ---------------------------------------------------------------------------

class TestSetupEngineAndLlm(DaemonTestBase):

    def test_engine_invalid_rejected(self):
        status, data = self.post("/api/setup/engine", body={"engine": "notepad"})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "engine_invalid")

    def test_engine_llmwiki_unhealthy_preserves_reason(self):
        # X12 已接入后台：用假的就绪资源和不健康引擎核对原因透传，
        # 原先“失败时不保存”的约束保留，且不启动或探测真实后台。
        self.server.make_engine = lambda settings: FakeEngine(
            ok=False, reason="llmwiki_desktop_running")
        with patch("xhs_wiki.assets.llmwiki_supported", return_value=True), \
                patch("xhs_wiki.assets.llmwiki_ready", return_value=True):
            status, data = self.post("/api/setup/engine", body={"engine": "llmwiki"})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "engine_unavailable")
        self.assertEqual(data.get("reason"), "llmwiki_desktop_running")
        self.assertEqual(self.store.load_settings()["engine"], "markdown")

    def test_engine_markdown_saved_when_healthy(self):
        status, data = self.post("/api/setup/engine", body={"engine": "markdown"})
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        self.assertEqual(self.store.load_settings()["engine"], "markdown")

    def test_engine_unhealthy_reason_returned(self):
        self.server.make_engine = lambda settings: FakeEngine(
            ok=False, reason="kb_not_writable:PermissionError")
        status, data = self.post("/api/setup/engine", body={"engine": "markdown"})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("reason"), "kb_not_writable:PermissionError")

    def test_llm_provider_invalid_rejected(self):
        status, data = self.post("/api/setup/llm",
                                 body={"provider": "gpt", "model": "m"})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "llm_provider_invalid")

    def test_llm_store_validation_applied(self):
        # 沿用 store 的校验：openai_compatible 的 base_url 必须 https。
        status, data = self.post("/api/setup/llm", body={
            "provider": "openai_compatible", "model": "m",
            "base_url": "http://evil.example"})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "base_url_invalid")

    def test_llm_valid_saved(self):
        status, data = self.post("/api/setup/llm", body={
            "provider": "openai_compatible", "model": "m",
            "base_url": "https://api.example.test/v1"})
        self.assertEqual(status, 200)
        self.assertEqual(self.store.load_settings()["llm"]["model"], "m")

    def test_key_saved_and_never_echoed(self):
        status, body = self.post("/api/setup/key",
                                 body={"provider": "anthropic", "key": FAKE_KEY})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"saved": True})
        self.assertEqual(self.keychain.saved.get("anthropic"), FAKE_KEY)

    def test_key_not_in_response_log_or_disk(self):
        status, body = self.post("/api/setup/key",
                                 body={"provider": "anthropic", "key": FAKE_KEY})
        # 响应体（原始字节）里没有 Key。
        self.assertNotIn(FAKE_KEY.encode(), json.dumps(body).encode())
        # 日志目录（守护进程唯一的写日志位置）里也没有。
        for path in self.log_dir.rglob("*") if self.log_dir.exists() else []:
            if path.is_file():
                self.assertNotIn(FAKE_KEY, path.read_text(encoding="utf-8"))
        # 数据目录的任何文件里也没有。
        for path in self.home.rglob("*"):
            if path.is_file() and path.suffix in (".json", ".jsonl", ".log"):
                self.assertNotIn(FAKE_KEY, path.read_text(encoding="utf-8"))

    def test_key_invalid_rejected(self):
        status, data = self.post("/api/setup/key",
                                 body={"provider": "anthropic", "key": ""})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "key_invalid")

    def test_key_provider_not_api_rejected(self):
        # claude_code/codex 用登录态，不需要 Key。
        status, data = self.post("/api/setup/key",
                                 body={"provider": "claude_code", "key": FAKE_KEY})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "key_provider_invalid")

    def test_key_save_failure_reported_without_key(self):
        class BrokenKeychain(FakeKeychain):
            def save(self, account, key):
                raise LLMError("unavailable", "keychain_save_failed_1")

        self.server.keychain = lambda: BrokenKeychain()
        status, data = self.post("/api/setup/key",
                                 body={"provider": "anthropic", "key": FAKE_KEY})
        self.assertEqual(status, 400)
        self.assertIn("key_save_failed", data.get("error", ""))
        self.assertNotIn(FAKE_KEY, json.dumps(data))

    def test_test_llm_without_confirm_cost_rejected(self):
        self.store.update_settings({"llm": {"provider": "anthropic",
                                            "model": "m", "base_url": None}})
        status, data = self.post("/api/setup/test-llm", body={})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "cost_confirmation_required")
        self.assertEqual(self.provider.checked, 0)  # 一次都没发请求

    def test_test_llm_with_confirm_cost_calls_check(self):
        self.store.update_settings({"llm": {"provider": "anthropic",
                                            "model": "m", "base_url": None}})
        status, data = self.post("/api/setup/test-llm",
                                 body={"confirm_cost": True})
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        self.assertEqual(self.provider.checked, 1)

    def test_test_llm_cli_provider_needs_no_confirmation(self):
        # claude_code 的 check() 只查登录状态，不花钱，不需要确认。
        status, data = self.post("/api/setup/test-llm", body={})
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))

    def test_test_llm_failure_returned(self):
        self.provider = FakeProvider(ok=False, reason="not_logged_in")
        self.server.make_provider = lambda settings: self.provider
        status, data = self.post("/api/setup/test-llm", body={})
        self.assertEqual(status, 200)
        self.assertFalse(data.get("ok"))
        self.assertEqual(data.get("reason"), "not_logged_in")

    def test_first_import_number_saved(self):
        status, data = self.post("/api/setup/first-import", body={"limit": 50})
        self.assertEqual(status, 200)
        self.assertEqual(self.store.load_settings()["first_import_limit"], 50)

    def test_first_import_null_means_all(self):
        status, data = self.post("/api/setup/first-import", body={"limit": None})
        self.assertEqual(status, 200)
        # null（全部收藏）原样保存——runner._eligible 把空值当作不限，
        # 以前按上限 1000 保存导致"全部收藏"名不副实。
        self.assertIsNone(self.store.load_settings()["first_import_limit"])

    def test_first_import_invalid_rejected(self):
        status, data = self.post("/api/setup/first-import", body={"limit": "many"})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "first_import_limit_invalid")


# ---------------------------------------------------------------------------
# 首次向导：finish 的条件判断和完成动作
# ---------------------------------------------------------------------------

class TestSetupFinish(DaemonTestBase):

    def ready_settings(self):
        kb = Path(self.tmp.name) / "kb"
        kb.mkdir(exist_ok=True)
        self.store.update_settings({"kb_root": str(kb)})
        self.store.update_settings({"profile_url": FAKE_PROFILE_URL})

    def test_finish_rejected_when_nothing_ready(self):
        status, data = self.post("/api/setup/finish")
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "setup_incomplete")
        for missing in ("kb_root", "login"):  # 引擎默认是健康的假引擎
            self.assertIn(missing, data.get("missing", []))
        self.assertFalse(self.store.load_settings()["setup_completed"])
        self.assertEqual(self.spawner.calls, [])

    def test_finish_rejected_when_engine_unhealthy(self):
        self.ready_settings()
        self.server.make_engine = lambda settings: FakeEngine(
            ok=False, reason="engine_db_error")
        status, data = self.post("/api/setup/finish")
        self.assertEqual(status, 400)
        self.assertIn("engine", data.get("missing", []))
        self.assertEqual(self.spawner.calls, [])

    def test_finish_rejected_without_api_key(self):
        self.ready_settings()
        self.store.update_settings({"llm": {"provider": "anthropic",
                                            "model": "m", "base_url": None}})
        status, data = self.post("/api/setup/finish")
        self.assertEqual(status, 400)
        self.assertIn("llm_key", data.get("missing", []))

    def test_finish_accepted_and_runs(self):
        self.ready_settings()  # 默认 provider 是 claude_code：不需要 Key
        status, data = self.post("/api/setup/finish")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("setup_completed"))
        self.assertTrue(self.store.load_settings()["setup_completed"])
        # 立即以 trigger=manual 开跑。
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertIn("manual", self.spawner.calls[0])

    def test_finish_accepted_with_api_key_saved(self):
        self.ready_settings()
        self.store.update_settings({"llm": {"provider": "anthropic",
                                            "model": "m", "base_url": None}})
        self.keychain.save("anthropic", FAKE_KEY)
        status, data = self.post("/api/setup/finish")
        self.assertEqual(status, 200)
        self.assertEqual(len(self.spawner.calls), 1)


# ---------------------------------------------------------------------------
# 失败重试、打开知识库
# ---------------------------------------------------------------------------

class TestRetryAndOpenKb(DaemonTestBase):

    def test_retry_resets_failed_note(self):
        note_id = self.add_failed_note()
        status, data = self.post("/api/notes/retry", body={"note_id": note_id})
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        with self.server.ledger_scope() as ledger:
            self.assertEqual(ledger.get(note_id)["stage"], "discovered")

    def test_retry_unknown_note_400(self):
        status, data = self.post("/api/notes/retry",
                                 body={"note_id": "ff" * 12})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "note_not_found")

    def test_retry_not_failed_note_400(self):
        note_id = self.add_failed_note()
        with self.server.ledger_scope() as ledger:
            ledger.retry(note_id)  # 先退回 discovered
        status, data = self.post("/api/notes/retry", body={"note_id": note_id})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "note_not_failed")

    def test_retry_missing_note_id_400(self):
        status, data = self.post("/api/notes/retry", body={})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "note_id_invalid")

    def test_open_kb_opens_kb_root_only(self):
        # 只用 /usr/bin/open -R 在访达里选中 resolve 之后的目录。
        kb = Path(self.tmp.name) / "kb"
        kb.mkdir(exist_ok=True)
        self.store.update_settings({"kb_root": str(kb)})
        status, data = self.post("/api/open-kb")
        self.assertEqual(status, 200)
        self.assertEqual(self.opener_calls,
                         [["/usr/bin/open", "-R", str(kb.resolve())]])

    def test_open_kb_sends_extra_body_ignored(self):
        # 接口不接受任何参数：即使带了 path 也只打开 kb_root。
        kb = Path(self.tmp.name) / "kb"
        kb.mkdir(exist_ok=True)
        self.store.update_settings({"kb_root": str(kb)})
        status, _ = self.post("/api/open-kb", body={"path": "/etc"})
        self.assertEqual(status, 200)
        self.assertEqual(self.opener_calls,
                         [["/usr/bin/open", "-R", str(kb.resolve())]])


# ---------------------------------------------------------------------------
# 知识库统计和 30 秒缓存
# ---------------------------------------------------------------------------

class FakeMonoClock:
    """可手动推进的单调时钟（秒），代替 time.monotonic。"""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class TestKbStats(DaemonTestBase):

    def test_count_files(self):
        kb = Path(self.tmp.name) / "kb"
        (kb / "主题").mkdir(parents=True)
        (kb / "来源").mkdir(parents=True)
        for i in range(3):
            (kb / "主题" / f"t{i}.md").write_text("x", encoding="utf-8")
        (kb / "来源" / "s.md").write_text("x", encoding="utf-8")
        self.assertEqual(daemon.count_files(kb / "主题"), 3)
        self.assertEqual(daemon.count_files(kb / "来源"), 1)
        self.assertEqual(daemon.count_files(kb / "不存在"), 0)

    def test_kb_stats_in_status(self):
        kb = Path(self.tmp.name) / "kb"
        (kb / "主题").mkdir(parents=True)
        (kb / "主题" / "t.md").write_text("x", encoding="utf-8")
        self.store.update_settings({"kb_root": str(kb)})
        self.add_failed_note()
        # 让笔记进入 deferred 阶段：暂缓数从台账来（见 daemon.kb_stats）。
        with self.server.ledger_scope() as ledger:
            with ledger._conn:
                ledger._conn.execute(
                    "UPDATE notes SET stage='deferred' WHERE note_id=?",
                    (FAKE_NOTE_IDS[0],))
        _, data = self.request("GET", "/api/status")
        kb_data = json.loads(data)["kb"]
        self.assertEqual(kb_data["topics"], 1)
        self.assertEqual(kb_data["sources"], 0)
        self.assertEqual(kb_data["deferred"], 1)

    def test_kb_stats_cached_30_seconds(self):
        clock = FakeMonoClock()
        self.server.clock = clock
        self.server._kb_cache = None
        kb = Path(self.tmp.name) / "kb"
        (kb / "主题").mkdir(parents=True)
        self.store.update_settings({"kb_root": str(kb)})
        seen = []
        real_count = daemon.count_files

        def counting(directory):
            seen.append(directory)
            return real_count(directory)

        daemon.count_files = counting
        try:
            for _ in range(3):
                self.server.cached_kb_stats(str(kb), {})
            self.assertEqual(len(seen), 2)  # 主题 + 来源 各数一次，无重复
            clock.t += 31
            self.server.cached_kb_stats(str(kb), {})
            self.assertEqual(len(seen), 4)  # 过期后重算
        finally:
            daemon.count_files = real_count


# ---------------------------------------------------------------------------
# cli：print-launch-agent（plist 可解析、路径不在 ~/Documents）
# ---------------------------------------------------------------------------

class TestPrintLaunchAgent(unittest.TestCase):

    def test_build_launch_agent_valid_plist(self):
        agent = cli.build_launch_agent("/usr/bin/python3")
        parsed = plistlib.loads(plistlib.dumps(agent))
        self.assertEqual(parsed["Label"], "io.github.xhs-wiki.service")
        self.assertTrue(parsed["RunAtLoad"])
        self.assertTrue(parsed["KeepAlive"])
        # -I（隔离模式），主目录里的同名 .py 不会被优先加载。
        self.assertEqual(parsed["ProgramArguments"],
                         ["/usr/bin/python3", "-I", "-m", "xhs_wiki.cli",
                          "serve"])

    def test_launch_agent_paths_outside_documents(self):
        parsed = cli.build_launch_agent()
        documents = Path.home() / "Documents"
        self.assertEqual(parsed["WorkingDirectory"], str(Path.home()))
        for key in ("WorkingDirectory", "StandardOutPath", "StandardErrorPath"):
            self.assertFalse(Path(parsed[key]).is_relative_to(documents))
        self.assertEqual(parsed["StandardOutPath"],
                         str(Path.home() / "Library" / "Logs" / "xhs-wiki"
                             / "daemon.log"))
        self.assertEqual(parsed["StandardErrorPath"],
                         parsed["StandardOutPath"])

    def test_launch_agent_env_home_user_path(self):
        # PATH 是"安装时的 PATH + 兜底系统目录"，只保留
        # 存在的目录；没有代理变量时环境里只有 HOME、USER、PATH。代理和
        # PATH 的细节见 tests/test_y2_console_security.py。
        environ = {"HOME": str(Path.home()), "USER": "generated-user",
                   "PATH": "/usr/bin:/bin"}
        parsed = cli.build_launch_agent("/usr/bin/python3", environ=environ)
        env = parsed["EnvironmentVariables"]
        self.assertEqual(set(env), {"HOME", "USER", "PATH"})
        entries = env["PATH"].split(":")
        for part in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
            self.assertIn(part, entries)
        self.assertTrue(all(Path(entry).is_dir() for entry in entries))

    def test_print_launch_agent_output_parses_as_plist(self):
        # 输出要能直接存成 plist：除 plist 本身不能有任何别的输出。
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.print_launch_agent("/usr/bin/python3")
        parsed = plistlib.loads(buf.getvalue().encode("utf-8"))
        self.assertEqual(parsed["Label"], "io.github.xhs-wiki.service")
        self.assertEqual(parsed["ProgramArguments"][0], "/usr/bin/python3")

    def test_cli_main_print_launch_agent(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["print-launch-agent", "--python", "/x/y/python"])
        self.assertEqual(code, 0)
        parsed = plistlib.loads(buf.getvalue().encode("utf-8"))
        self.assertEqual(parsed["ProgramArguments"][0], "/x/y/python")

    def test_print_launch_agent_creates_nothing(self):
        # 只打印、不写入：日志目录不应该被创建。
        before = (Path.home() / "Library" / "Logs" / "xhs-wiki").exists()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.print_launch_agent()
        after = (Path.home() / "Library" / "Logs" / "xhs-wiki").exists()
        self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# cli doctor：不创建任何目录
# ---------------------------------------------------------------------------

class FakeDoctorDeps:
    """doctor 用的假依赖：不跑 claude、不碰钥匙串。"""

    def __init__(self):
        self.provider = FakeProvider()

    def make_provider(self, settings):
        return self.provider

    def key_exists(self, name):
        return True


class TestDoctorNoCreate(unittest.TestCase):

    def run_doctor(self, home: Path) -> str:
        os.environ["XHS_WIKI_LOG_DIR"] = str(home / "logs")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["--home", str(home), "doctor"],
                            deps_factory=FakeDoctorDeps)
        self.assertEqual(code, 0)
        return buf.getvalue()

    def test_doctor_does_not_create_missing_kb_root(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name) / "app"
        home.mkdir()
        kb = Path(tmp.name) / "deep" / "kb"  # 不存在，父目录也不存在
        (home / "settings.json").write_text(
            json.dumps({"kb_root": str(kb)}), encoding="utf-8")
        output = self.run_doctor(home)
        self.assertFalse(kb.exists())
        self.assertFalse((Path(tmp.name) / "deep").exists())
        # 结果里写明"将创建"。
        self.assertIn("将创建", output)

    def test_doctor_checks_existing_kb_root_in_place(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name) / "app"
        home.mkdir()
        kb = Path(tmp.name) / "kb"
        kb.mkdir()
        (home / "settings.json").write_text(
            json.dumps({"kb_root": str(kb)}), encoding="utf-8")
        output = self.run_doctor(home)
        self.assertIn("✓ 知识库目录可写", output)
        self.assertNotIn("将创建", output)
        # 目录里没有留下任何探针文件。
        self.assertEqual(list(kb.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
