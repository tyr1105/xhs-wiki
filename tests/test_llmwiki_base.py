# -*- coding: utf-8 -*-
"""tests/test_llmwiki_base.py —— LLM Wiki 引擎地基模块的测试。

这个文件做什么：覆盖 engines/llmwiki/ 的五个模块：
- client.py：用一个测试内启动的假 LLM Wiki HTTP 服务器（http.server，
  端口 0）验证 Host / Origin / 令牌 / 幂等键 / 请求体上限 / 错误解析；
- backend.py：用一个假的"可执行文件"（测试写到临时目录的脚本）代替
  真实 LLM Wiki：它从环境变量读令牌、启动假服务器，并把我们关心的
  事实（令牌是否在环境变量里、命令行参数、每个请求的头）记录成
  JSONL（记录里只有布尔值和头部名，绝不写令牌本身）；
- journal.py：用假客户端验证复用 key、签名变化、结果不明、归档上限；
- project.py：用真实的 git（只在临时目录里，不碰全局配置）验证
  scaffold 的文件结构、首次提交和拒绝非空目录；
- agents.py：用假的 claude / codex 脚本验证包装脚本追加的参数和环境变量。

所有测试数据都是代码生成的虚构数据；不访问网络（只连 127.0.0.1 的
测试进程）、不调用真实模型、不启动真实 LLM Wiki。
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from xhs_wiki.engines.llmwiki import (
    ApiError,
    Backend,
    Client,
    EngineUnavailable,
    Journal,
    agents,
    open_project,
    scaffold,
)

# 一个虚构的、格式合法的 projectId（uuid v4 形状）。
FAKE_PROJECT_ID = "11111111-2222-4333-8444-555555555555"


# ---------------------------------------------------------------------------
# 假 LLM Wiki HTTP 服务器（client / project 测试直接在进程内启动）
# ---------------------------------------------------------------------------


class FakeWikiHandler(BaseHTTPRequestHandler):
    """模拟 LLM Wiki 本地 API 的最小行为。

    行为通过 server.state 这个 dict 控制：
    - "requests"：记录每个请求的 {method, path, host, origin, auth, idem}；
    - "token"：期望的 Bearer 令牌；
    - "next_error"：可选 (status, code)，下一个请求直接回这个错误。
    """

    def _record(self, method: str, body: bytes | None = None) -> None:
        entry = {
            "method": method,
            "path": self.path,
            "host": self.headers.get("Host"),
            "origin": self.headers.get("Origin"),
            "auth": self.headers.get("Authorization") == "Bearer " + self.server.state["token"],
            "idem": self.headers.get("Idempotency-Key"),
        }
        if body is not None:
            entry["body"] = json.loads(body.decode("utf-8"))
        self.server.state["requests"].append(entry)

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        self._record(method, body)
        error = self.server.state.get("next_error")
        if error:
            self.server.state["next_error"] = None
            self._send(error[0], {"error": {"code": error[1], "message": "fake"}})
            return
        if not self.server.state["requests"][-1]["auth"]:
            self._send(401, {"error": {"code": "API_UNAUTHORIZED", "message": "fake"}})
            return
        if method == "GET" and self.path == "/api/v1/health":
            self._send(200, {"data": {"ready": True, "webviewWindowCount": 0}})
        elif method == "POST" and self.path == "/api/v1/projects/open":
            self._send(200, {"data": {"projectId": FAKE_PROJECT_ID}})
        else:
            self._send(404, {"error": {"code": "API_NOT_FOUND", "message": "fake"}})

    def do_GET(self):  # noqa: N802（http.server 的接口名）
        self._handle("GET")

    def do_POST(self):  # noqa: N802
        self._handle("POST")

    def log_message(self, *args):  # 静默，别污染 pytest 输出
        pass


@pytest.fixture()
def fake_server(tmp_path):
    """在 127.0.0.1 上起一个假 API 服务器（端口 0 = 系统分配）。"""

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeWikiHandler)
    server.state = {"requests": [], "token": "f" * 64}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def make_client(server) -> Client:
    return Client(server.server_address[1], lambda: server.state["token"])


# ---------------------------------------------------------------------------
# client.py 的测试
# ---------------------------------------------------------------------------


class TestClient:
    def test_health_get_ok(self, fake_server):
        status, payload = make_client(fake_server).request("GET", "/health")
        assert status == 200
        assert payload["data"]["ready"] is True
        assert payload["data"]["webviewWindowCount"] == 0

    def test_host_header_is_loopback_with_port(self, fake_server):
        make_client(fake_server).request("GET", "/health")
        request = fake_server.state["requests"][-1]
        assert request["host"] == f"127.0.0.1:{fake_server.server_address[1]}"

    def test_no_origin_header(self, fake_server):
        make_client(fake_server).request("POST", "/projects/open", {"path": "/tmp/x"}, str(uuid.uuid4()))
        assert fake_server.state["requests"][-1]["origin"] is None

    def test_bearer_token_sent(self, fake_server):
        make_client(fake_server).request("GET", "/health")
        assert fake_server.state["requests"][-1]["auth"] is True

    def test_wrong_token_rejected_as_api_error(self, fake_server):
        client = Client(fake_server.server_address[1], lambda: "0" * 64)
        with pytest.raises(ApiError) as info:
            client.request("GET", "/health")
        assert info.value.status == 401
        assert info.value.code == "API_UNAUTHORIZED"
        assert info.value.retryable is False

    def test_post_requires_idempotency_key(self, fake_server):
        with pytest.raises(ValueError, match="idempotency_key_invalid"):
            make_client(fake_server).request("POST", "/projects/open", {"path": "/tmp/x"})

    @pytest.mark.parametrize("bad_key", ["", "有中文", "a b", "x" * 129, "a/b"])
    def test_post_rejects_invalid_key_format(self, fake_server, bad_key):
        with pytest.raises(ValueError, match="idempotency_key_invalid"):
            make_client(fake_server).request("POST", "/projects/open", {"path": "/tmp/x"}, bad_key)

    def test_post_key_max_length_accepted(self, fake_server):
        key = "k" * 128
        status, _ = make_client(fake_server).request("POST", "/projects/open", {"path": "/tmp/x"}, key)
        assert status == 200
        assert fake_server.state["requests"][-1]["idem"] == key

    def test_post_sends_idempotency_key(self, fake_server):
        key = str(uuid.uuid4())
        make_client(fake_server).request("POST", "/projects/open", {"path": "/tmp/x"}, key)
        assert fake_server.state["requests"][-1]["idem"] == key

    def test_body_over_2mib_rejected_before_send(self, fake_server):
        big = {"blob": "x" * (2 * 1024 * 1024 + 100)}
        with pytest.raises(ValueError, match="body_too_large"):
            make_client(fake_server).request("POST", "/projects/open", big, str(uuid.uuid4()))
        # 请求根本没发出去
        assert fake_server.state["requests"] == []

    def test_error_parsed_to_retryable_api_error(self, fake_server):
        fake_server.state["next_error"] = (429, "API_RETRY_CACHE_FULL")
        with pytest.raises(ApiError) as info:
            make_client(fake_server).request("POST", "/projects/open", {"path": "/tmp/x"}, str(uuid.uuid4()))
        assert info.value.status == 429
        assert info.value.code == "API_RETRY_CACHE_FULL"
        assert info.value.retryable is True

    def test_409_conflict_not_retryable(self, fake_server):
        fake_server.state["next_error"] = (409, "API_IDEMPOTENCY_CONFLICT")
        with pytest.raises(ApiError) as info:
            make_client(fake_server).request("POST", "/projects/open", {"path": "/tmp/x"}, str(uuid.uuid4()))
        assert info.value.retryable is False

    def test_transport_error_retryable(self):
        # 端口 1 上没有任何监听：连接被拒 → transport_error。
        client = Client(1, lambda: "f" * 64)
        with pytest.raises(ApiError) as info:
            client.request("GET", "/health", timeout=2)
        assert info.value.status == 0
        assert info.value.code == "transport_error"
        assert info.value.retryable is True

    def test_invalid_path_rejected(self, fake_server):
        with pytest.raises(ValueError, match="api_path_invalid"):
            make_client(fake_server).request("GET", "health")
        with pytest.raises(ValueError, match="api_path_invalid"):
            make_client(fake_server).request("GET", "//health")

    def test_token_provider_called_per_request(self, fake_server):
        calls = []

        def provider():
            calls.append(1)
            return fake_server.state["token"]

        client = Client(fake_server.server_address[1], provider)
        client.request("GET", "/health")
        client.request("GET", "/health")
        assert len(calls) == 2

    def test_repr_has_no_token(self, fake_server):
        client = make_client(fake_server)
        assert "f" * 64 not in repr(client)


# ---------------------------------------------------------------------------
# 假后台可执行文件（backend 测试用；也内嵌一份假服务器实现）
# ---------------------------------------------------------------------------

# 假后台的服务器脚本：从环境变量读令牌，把 argv / 令牌格式 / 每个请求
# 的头部事实记进 JSONL（记录里只有布尔值，不写令牌本身），再启动
# HTTP 服务器。由测试写到临时目录，用当前 venv 的 python 执行。
FAKE_SERVER_PY = r'''
import json, os, re, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

args = sys.argv[1:]
# args[0] 是垫片自身的路径（也就是 Backend 的 binary）；--api-port 在后面
port = int(args[args.index("--api-port") + 1])
token = os.environ.get("LLM_WIKI_API_TOKEN", "")
record = os.environ["FAKE_RECORD"]
with open(record, "a") as f:
    f.write(json.dumps({
        "argv": args,
        "token_in_env": bool(re.fullmatch(r"[0-9a-f]{64}", token)),
    }) + "\n")

class H(BaseHTTPRequestHandler):
    def _send(self, s, o):
        b = json.dumps(o).encode()
        self.send_response(s)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def _ok(self):
        return self.headers.get("Authorization") == "Bearer " + token
    def _record(self, method):
        with open(record, "a") as f:
            f.write(json.dumps({
                "method": method, "path": self.path,
                "host": self.headers.get("Host"),
                "origin": self.headers.get("Origin"),
                "auth": self._ok(),
                "idem": self.headers.get("Idempotency-Key"),
            }) + "\n")
    def do_GET(self):
        self._record("GET")
        if not self._ok():
            return self._send(401, {"error": {"code": "API_UNAUTHORIZED", "message": "x"}})
        if self.path == "/api/v1/health":
            return self._send(200, {"data": {"ready": True, "webviewWindowCount": 0}})
        self._send(404, {"error": {"code": "API_NOT_FOUND", "message": "x"}})
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        self._record("POST")
        if not self._ok():
            return self._send(401, {"error": {"code": "API_UNAUTHORIZED", "message": "x"}})
        if self.path == "/api/v1/projects/open":
            return self._send(200, {"data": {"projectId": "%s", "path": body.decode()}})
        self._send(404, {"error": {"code": "API_NOT_FOUND", "message": "x"}})
    def log_message(self, *a):
        pass

ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
''' % FAKE_PROJECT_ID


def _write_fake_backend(tmp_path: Path) -> tuple[Path, Path]:
    """生成假的 LLM Wiki 可执行文件：一个 shell 垫片 + 服务器脚本。

    返回 (binary, record_path)。binary 就是 Backend 要启动的"程序"；
    record_path 是它写下的 JSONL 观察记录。
    """

    record = tmp_path / "backend-record.jsonl"
    server_py = tmp_path / "fake-server.py"
    server_py.write_text(FAKE_SERVER_PY, encoding="utf-8")
    binary = tmp_path / "fake-llm-wiki"
    # FAKE_RECORD 路径直接写死在垫片里：Backend 给子进程的环境是固定的
    # 四个变量，没法额外传观察文件路径。"$0"（垫片自身路径，即 Backend
    # 的 binary）也传给服务器脚本，这样记录的 argv 和 ps 看到的命令行
    # 都完整包含 binary 路径。
    binary.write_text(
        "#!/bin/sh\n"
        f"FAKE_RECORD='{record}' exec '{sys.executable}' '{server_py}' \"$0\" \"$@\"\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary, record


def _read_record(record: Path) -> list[dict]:
    return [json.loads(line) for line in record.read_text().splitlines()]


def _pick_free_port() -> int:
    """让内核分配一个空闲端口再让出（测试之间避免撞端口）。"""

    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TestBackend:
    def _make(self, tmp_path, binary, port, **kwargs):
        project_root = tmp_path / "wiki"
        import_root = tmp_path / "imports"
        project_root.mkdir()
        import_root.mkdir()
        agent_bin = tmp_path / "llmwiki-bin"
        agent_bin.mkdir()
        backend = Backend(
            binary,
            project_root,
            import_root,
            port,
            agent_bin_dir=agent_bin,
            **kwargs,
        )
        if "ps" not in kwargs:
            # 默认注入"命令行符合预期"的假 ps：真实的 ps 在部分开发沙箱里
            # 被禁跑，命令行核对逻辑本身由下面的专用测试覆盖。
            backend._ps = lambda pid: " ".join(backend._expected_argv())
        return backend

    def test_start_until_ready(self, tmp_path):
        binary, record = _write_fake_backend(tmp_path)
        port = _pick_free_port()
        backend = self._make(tmp_path, binary, port)
        try:
            backend.start()
            assert backend.is_running()
            status, payload = backend.client().request("GET", "/health")
            assert status == 200 and payload["data"]["ready"] is True
        finally:
            backend.stop()

    def test_token_in_env_not_in_argv(self, tmp_path):
        binary, record = _write_fake_backend(tmp_path)
        port = _pick_free_port()
        backend = self._make(tmp_path, binary, port)
        try:
            backend.start()
            entries = _read_record(record)
            startup = entries[0]
            # 令牌通过环境变量传给了子进程（64 位十六进制）
            assert startup["token_in_env"] is True
            # 命令行参数里只有规定的那些（见 backend._expected_argv），绝无令牌
            argv = " ".join(startup["argv"])
            assert not re.search(r"[0-9a-f]{64}", argv)
            assert argv.startswith(str(binary))
            assert "--serve-api --headless" in argv
        finally:
            backend.stop()

    def test_request_headers_from_backend_client(self, tmp_path):
        binary, record = _write_fake_backend(tmp_path)
        port = _pick_free_port()
        backend = self._make(tmp_path, binary, port)
        try:
            backend.start()
            backend.client().request("GET", "/health")
            backend.client().request(
                "POST", "/projects/open", {"path": "/tmp/x"}, str(uuid.uuid4())
            )
            requests = [e for e in _read_record(record) if "method" in e]
            assert requests[-2]["host"] == f"127.0.0.1:{port}"
            assert requests[-2]["origin"] is None
            assert requests[-1]["idem"]  # POST 带了幂等键
            assert requests[-1]["auth"] is True
        finally:
            backend.stop()

    def test_start_rejects_when_desktop_running(self, tmp_path):
        # 桌面版占着单实例锁的表现：进程马上退出。
        binary = tmp_path / "busy-llm-wiki"
        binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        backend = self._make(tmp_path, binary, _pick_free_port())
        with pytest.raises(EngineUnavailable) as info:
            backend.start()
        assert info.value.code == "llmwiki_desktop_running"

    def test_start_timeout_with_injected_clock(self, tmp_path):
        # 一个永远不退出但也不监听端口的假程序 + 快进时钟 → 启动超时。
        binary = tmp_path / "silent-llm-wiki"
        binary.write_text("#!/bin/sh\nsleep 300\n", encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        ticks = [0.0]

        def fast_clock():  # 每次看表都直接跳到超时之后
            ticks[0] += 200.0
            return ticks[0]

        backend = self._make(tmp_path, binary, _pick_free_port(), clock=fast_clock)
        try:
            with pytest.raises(ValueError, match="backend_start_timeout"):
                backend.start()
        finally:
            if backend.is_running():
                backend._process.terminate()
                backend._process.wait(timeout=10)

    def test_start_requires_existing_roots(self, tmp_path):
        binary, _ = _write_fake_backend(tmp_path)
        project_root = tmp_path / "no-wiki"
        import_root = tmp_path / "imports"
        import_root.mkdir()
        agent_bin = tmp_path / "llmwiki-bin"
        agent_bin.mkdir()
        backend = Backend(
            binary, project_root, import_root, _pick_free_port(),
            agent_bin_dir=agent_bin,
        )
        with pytest.raises(ValueError, match="project_root_missing"):
            backend.start()

    def test_stop_terminates_process(self, tmp_path):
        binary, _ = _write_fake_backend(tmp_path)
        port = _pick_free_port()
        backend = self._make(tmp_path, binary, port)
        backend.start()
        pid = backend.pid
        backend.stop()
        assert not backend.is_running()
        # 进程确实退出了：再给它发信号会被告知"没有这个进程"
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    def test_restart_rejects_foreign_pid(self, tmp_path):
        binary, _ = _write_fake_backend(tmp_path)
        port = _pick_free_port()
        backend = self._make(tmp_path, binary, port, ps=lambda pid: "/usr/bin/something_else --web")
        backend.start()
        try:
            with pytest.raises(ValueError, match="backend_pid_mismatch"):
                backend.restart_for_retry_cache()
            assert backend.is_running()  # 没有误杀
        finally:
            backend.stop()

    def test_stop_busy_when_task_running(self, tmp_path):
        binary, _ = _write_fake_backend(tmp_path)
        port = _pick_free_port()
        backend = self._make(tmp_path, binary, port)
        backend.start()
        try:
            tasks = backend._project_root / ".app" / "tasks"
            tasks.mkdir(parents=True, exist_ok=True)
            (tasks / "task-1.json").write_text(
                json.dumps({"task": {"id": "task-1", "status": "running"}})
            )
            with pytest.raises(ValueError, match="backend_busy"):
                backend.stop()
            assert backend.is_running()  # 一个字节都没发，进程还在
        finally:
            (tasks / "task-1.json").unlink()
            backend.stop()

    def test_restart_for_retry_cache(self, tmp_path):
        binary, _ = _write_fake_backend(tmp_path)
        port = _pick_free_port()
        backend = self._make(tmp_path, binary, port)
        backend.start()
        first_pid = backend.pid
        try:
            backend.restart_for_retry_cache()
            assert backend.is_running()
            assert backend.pid != first_pid  # 确实换了新进程
            status, _ = backend.client().request("GET", "/health")
            assert status == 200
        finally:
            backend.stop()

    def test_restart_limit_two_per_round(self, tmp_path):
        binary, _ = _write_fake_backend(tmp_path)
        port = _pick_free_port()
        backend = self._make(tmp_path, binary, port)
        backend.start()
        try:
            backend.restart_for_retry_cache()
            backend.restart_for_retry_cache()
            with pytest.raises(ValueError, match="retry_cache_restart_limit"):
                backend.restart_for_retry_cache()
        finally:
            backend.stop()

    def test_token_not_in_repr(self, tmp_path):
        binary, _ = _write_fake_backend(tmp_path)
        backend = self._make(tmp_path, binary, _pick_free_port())
        text = repr(backend)
        assert not re.search(r"[0-9a-f]{64}", text)


# ---------------------------------------------------------------------------
# journal.py 的测试（用假客户端，不碰网络）
# ---------------------------------------------------------------------------


class FakeApiClient:
    """记录调用的假客户端：可配置返回值或抛出的 ApiError。"""

    def __init__(self, result=None, error: ApiError | None = None):
        self.result = result if result is not None else {"data": {"ok": True}}
        self.error = error
        self.calls = []
        self.on_call = None  # 测试挂钩：发送前回调（用来验证"先存盘"）

    def request(self, method, path, body=None, key=None, timeout=90):
        self.calls.append(
            {"method": method, "path": path, "body": body, "key": key}
        )
        if self.on_call:
            self.on_call(self)
        if self.error:
            raise self.error
        status = 200
        return status, self.result


class TestJournal:
    def test_key_persisted_before_request(self, tmp_path):
        journal = Journal(tmp_path / "journal.sqlite3")
        client = FakeApiClient()

        def check_persisted(_client):
            # 发送的那一刻，key 已经在数据库里了（崩溃也不会丢身份）
            import sqlite3

            conn = sqlite3.connect(journal.path)
            row = conn.execute(
                "SELECT key FROM operations WHERE name = ?", ("op-1",)
            ).fetchone()
            conn.close()
            assert row is not None and row[0] == _client.calls[-1]["key"]

        client.on_call = check_persisted
        journal.run("op-1", "/x", {"a": 1}, client)
        assert len(client.calls) == 1

    def test_same_input_reuses_key_and_caches_response(self, tmp_path):
        journal = Journal(tmp_path / "journal.sqlite3")
        client = FakeApiClient(result={"data": {"id": "r1"}})
        first = journal.run("op-1", "/x", {"a": 1}, client)
        key_before = journal.key_of("op-1")
        second = journal.run("op-1", "/x", {"a": 1}, client)
        # 第二次直接命中缓存：没有新请求、key 没变、回执一样
        assert len(client.calls) == 1
        assert journal.key_of("op-1") == key_before
        assert first == second == {"id": "r1"}

    def test_changed_input_rejected(self, tmp_path):
        journal = Journal(tmp_path / "journal.sqlite3")
        client = FakeApiClient()
        journal.run("op-1", "/x", {"a": 1}, client)
        with pytest.raises(ValueError, match="operation_input_changed"):
            journal.run("op-1", "/x", {"a": 2}, client)

    def test_changed_path_rejected(self, tmp_path):
        journal = Journal(tmp_path / "journal.sqlite3")
        client = FakeApiClient()
        journal.run("op-1", "/x", {"a": 1}, client)
        with pytest.raises(ValueError, match="operation_input_changed"):
            journal.run("op-1", "/y", {"a": 1}, client)

    def test_failure_recorded_status_and_code(self, tmp_path):
        journal = Journal(tmp_path / "journal.sqlite3")
        client = FakeApiClient(error=ApiError(500, "API_INTERNAL", True))
        with pytest.raises(ApiError):
            journal.run("op-1", "/x", {"a": 1}, client)
        outcome = journal.outcome_of("op-1")
        assert outcome["http_status"] == 500
        assert outcome["error_code"] == "API_INTERNAL"
        assert outcome["has_response"] is False

    def test_uncertain_after_failure_without_durable(self, tmp_path):
        journal = Journal(tmp_path / "journal.sqlite3")
        client = FakeApiClient(error=ApiError(500, "API_INTERNAL", True))
        with pytest.raises(ApiError):
            journal.run("op-1", "/x", {"a": 1}, client)
        with pytest.raises(ValueError, match="uncertain_api_write_requires_reconciliation"):
            journal.run("op-1", "/x", {"a": 1}, client)

    def test_durable_rerun_resends_with_same_key(self, tmp_path):
        journal = Journal(tmp_path / "journal.sqlite3")
        failing = FakeApiClient(error=ApiError(500, "API_INTERNAL", True))
        with pytest.raises(ApiError):
            journal.run("op-1", "/x", {"a": 1}, failing)
        key = journal.key_of("op-1")
        ok = FakeApiClient(result={"data": {"fixed": True}})
        journal.run("op-1", "/x", {"a": 1}, ok, durable=True)
        assert ok.calls[0]["key"] == key  # durable 重发复用原 key
        assert journal.outcome_of("op-1")["has_response"] is True

    def test_archive_and_rekey_gives_new_key_and_history(self, tmp_path):
        journal = Journal(tmp_path / "journal.sqlite3")
        client = FakeApiClient()
        journal.run("op-1", "/x", {"a": 1}, client)
        old_key = journal.key_of("op-1")
        new_key = journal.archive_and_rekey("op-1", "backend_restart")
        assert new_key != old_key
        assert journal.key_of("op-1") == new_key
        history = journal.history_of("op-1")
        assert len(history) == 1 and history[0]["key"] == old_key
        assert history[0]["reason"] == "backend_restart"
        # 归档后是"没有回执的新尝试"，调用方声明可核对结果才能重发
        journal.run("op-1", "/x", {"a": 1}, client, durable=True)
        assert client.calls[-1]["key"] == new_key

    def test_archive_limit_two_per_reason(self, tmp_path):
        journal = Journal(tmp_path / "journal.sqlite3")
        client = FakeApiClient()
        journal.run("op-1", "/x", {"a": 1}, client)
        journal.archive_and_rekey("op-1", "backend_restart")
        journal.archive_and_rekey("op-1", "backend_restart")
        with pytest.raises(ValueError, match="archive_limit_reached"):
            journal.archive_and_rekey("op-1", "backend_restart")
        assert journal.archive_count("backend_restart") == 2

    def test_archive_reasons_counted_separately(self, tmp_path):
        journal = Journal(tmp_path / "journal.sqlite3")
        client = FakeApiClient()
        journal.run("op-1", "/x", {"a": 1}, client)
        journal.archive_and_rekey("op-1", "reason-a")
        journal.archive_and_rekey("op-1", "reason-a")
        # reason-b 是另一类原因，有自己的 2 次额度
        journal.archive_and_rekey("op-1", "reason-b")
        assert journal.archive_count("reason-b") == 1

    def test_archive_unknown_operation(self, tmp_path):
        journal = Journal(tmp_path / "journal.sqlite3")
        with pytest.raises(ValueError, match="operation_not_found"):
            journal.archive_and_rekey("nope", "backend_restart")

    def test_journal_not_created_inside_project_dir(self, tmp_path, monkeypatch):
        # 默认路径是 APP_HOME/llmwiki-journal.sqlite3（由 XHS_WIKI_HOME 控制）
        home = tmp_path / "app-home"
        monkeypatch.setenv("XHS_WIKI_HOME", str(home))
        journal = Journal()
        try:
            client = FakeApiClient()
            journal.run("op-1", "/x", {"a": 1}, client)
            assert journal.path == home / "llmwiki-journal.sqlite3"
            assert journal.path.is_file()
            assert not (tmp_path / "wiki").exists()  # 没碰知识库目录
        finally:
            journal.close()


# ---------------------------------------------------------------------------
# project.py 的测试（真实 git，只在临时目录）
# ---------------------------------------------------------------------------


def _tree(root: Path) -> set[str]:
    return {
        str(p.relative_to(root))
        for p in root.rglob("*")
        if ".git" not in p.parts
    }


class TestScaffold:
    def test_creates_expected_structure(self, tmp_path):
        root = tmp_path / "kb"
        project_id = scaffold(root)
        uuid.UUID(project_id)  # 是合法 uuid
        assert _tree(root) >= {
            "purpose.md",
            "schema.md",
            "wiki/index.md",
            "wiki/log.md",
            "wiki/overview.md",
            ".app/project.json",
            "raw/sources",
            ".app/tasks",
            "exports",
            "skills",
        }

    def test_purpose_md_from_rust_general_template(self, tmp_path):
        root = tmp_path / "kb"
        scaffold(root)
        text = (root / "purpose.md").read_text(encoding="utf-8")
        # 对照 Rust templates/projects/general/purpose.md 的关键句
        assert text.startswith("# Purpose\n")
        assert "A general-purpose local knowledge base" in text
        assert "## Key Questions" in text

    def test_schema_md_from_rust_general_template(self, tmp_path):
        root = tmp_path / "kb"
        scaffold(root)
        text = (root / "schema.md").read_text(encoding="utf-8")
        assert text.startswith("# Schema\n")
        assert "| entity | `wiki/entities/` |" in text
        assert "never create, modify, or summarize a page under `wiki/sources/`" in text

    def test_wiki_starters_use_directory_name(self, tmp_path):
        root = tmp_path / "my-kb"
        scaffold(root)
        index = (root / "wiki" / "index.md").read_text(encoding="utf-8")
        log = (root / "wiki" / "log.md").read_text(encoding="utf-8")
        overview = (root / "wiki" / "overview.md").read_text(encoding="utf-8")
        assert index.startswith("# my-kb\n")
        assert "- Project initialized." in log
        assert "my-kb knowledge base" in overview

    def test_project_json_contains_uuid(self, tmp_path):
        root = tmp_path / "kb"
        project_id = scaffold(root)
        data = json.loads((root / ".app" / "project.json").read_text())
        assert data == {"projectId": project_id}
        uuid.UUID(data["projectId"])

    def test_git_initial_commit_with_fixed_author(self, tmp_path):
        root = tmp_path / "kb"
        scaffold(root)
        log = subprocess.run(
            ["git", "-C", str(root), "log", "--format=%an|%ae|%s"],
            capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()
        assert log == ["xhs-wiki|xhs-wiki@localhost|Initial wiki project"]
        # 工作区干净（整理前的硬要求）
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert status == ""
        # 没有改用户的 git 全局配置（全局 user.name 不因此变化）
        global_name = subprocess.run(
            ["git", "config", "--global", "--get", "user.name"],
            capture_output=True, text=True,
        )
        assert global_name.stdout.strip() != "xhs-wiki"

    def test_rejects_non_empty_foreign_directory(self, tmp_path):
        root = tmp_path / "kb"
        root.mkdir()
        (root / "重要文件.txt").write_text("用户数据", encoding="utf-8")
        with pytest.raises(ValueError, match="project_root_not_empty"):
            scaffold(root)
        assert (root / "重要文件.txt").read_text() == "用户数据"  # 原样未动

    def test_idempotent_on_our_own_scaffold(self, tmp_path):
        root = tmp_path / "kb"
        first = scaffold(root)
        (root / "wiki" / "index.md").write_text("# 手工改过", encoding="utf-8")
        second = scaffold(root)
        assert second == first  # 复用原 projectId，不覆盖内容
        assert (root / "wiki" / "index.md").read_text(encoding="utf-8") == "# 手工改过"


class TestOpenProject:
    def test_open_returns_project_id(self, fake_server, tmp_path):
        client = make_client(fake_server)
        root = tmp_path / "kb"
        root.mkdir()
        assert open_project(client, root) == FAKE_PROJECT_ID

    def test_open_sends_resolved_path_and_key(self, fake_server, tmp_path):
        client = make_client(fake_server)
        root = tmp_path / "kb"
        root.mkdir()
        open_project(client, root)
        request = fake_server.state["requests"][-1]
        assert request["method"] == "POST"
        assert request["path"] == "/api/v1/projects/open"
        assert request["body"]["path"] == str(root.resolve())
        uuid.UUID(request["idem"])  # 幂等键是 uuid


# ---------------------------------------------------------------------------
# agents.py 的测试（假的 claude / codex 可执行文件）
# ---------------------------------------------------------------------------


def _fake_tool(tmp_path: Path, name: str) -> Path:
    """生成一个假的真实工具：把自己的 argv 和关键环境变量打印出来。"""

    path = tmp_path / f"real-{name}"
    path.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do echo "arg:$a"; done\n'
        'echo "home=$HOME"\n'
        'echo "ccd=${CLAUDE_CONFIG_DIR:-unset}"\n'
        'echo "mct=${CLAUDE_CODE_MAX_CONTEXT_TOKENS:-unset}"\n',
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture()
def agent_env(tmp_path, monkeypatch):
    """一个干净的包装脚本测试环境：假工具 + 独立 bin 目录 + 固定 HOME。"""

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USER", "tester")
    # X12 改为读取系统账户；替换 pwd 查询，避免脚本写入真实用户路径。
    monkeypatch.setattr(agents.pwd, "getpwuid", lambda uid: SimpleNamespace(
        pw_dir=str(tmp_path / "home"), pw_name="tester"))
    bin_dir = tmp_path / "llmwiki-bin"
    paths = agents.install_wrappers(
        claude_path=_fake_tool(tmp_path, "claude"),
        codex_path=_fake_tool(tmp_path, "codex"),
        bin_dir=bin_dir,
    )
    return bin_dir, paths


def _run(script: Path, *args: str) -> str:
    return subprocess.run(
        [str(script), *args], capture_output=True, text=True, timeout=30
    ).stdout


class TestAgentWrappers:
    def test_claude_print_appends_model(self, agent_env):
        bin_dir, paths = agent_env
        agents.write_model_config("claude-fake-4", bin_dir)
        out = _run(paths["claude"], "--print", "你好")
        assert "arg:--model" in out
        assert "arg:claude-fake-4" in out
        assert "arg:--print" in out

    def test_claude_5_5_gets_1m_suffix_and_context(self, agent_env):
        bin_dir, paths = agent_env
        agents.write_model_config("claude-fake-5-5", bin_dir)
        out = _run(paths["claude"], "-p", "hi")
        assert "arg:claude-fake-5-5[1m]" in out
        assert "mct=1000000" in out

    def test_claude_model_with_existing_suffix_not_doubled(self, agent_env):
        bin_dir, paths = agent_env
        agents.write_model_config("claude-fake-5-5[1m]", bin_dir)
        out = _run(paths["claude"], "--print", "hi")
        assert "arg:claude-fake-5-5[1m]" in out
        assert out.count("[1m]") == 1

    def test_claude_without_print_passes_through(self, agent_env):
        bin_dir, paths = agent_env
        agents.write_model_config("claude-fake-4", bin_dir)
        out = _run(paths["claude"], "--version")
        assert "arg:--model" not in out
        assert "arg:--version" in out

    def test_claude_empty_model_no_flag(self, agent_env):
        bin_dir, paths = agent_env
        agents.write_model_config("", bin_dir)
        out = _run(paths["claude"], "--print", "hi")
        assert "arg:--model" not in out

    def test_claude_restores_home_and_unsets_config_dir(self, agent_env):
        bin_dir, paths = agent_env
        agents.write_model_config("claude-fake-4", bin_dir)
        env = dict(os.environ)
        env.pop("HOME", None)
        env["CLAUDE_CONFIG_DIR"] = "/somewhere/else"
        result = subprocess.run(
            [str(paths["claude"]), "--print", "hi"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        out = result.stdout
        assert f"home={bin_dir.parent / 'home'}" in out
        assert "ccd=unset" in out

    def test_claude_no_debug_output(self, agent_env):
        bin_dir, paths = agent_env
        agents.write_model_config("claude-fake-4", bin_dir)
        _run(paths["claude"], "--print", "hi")
        # 除了两个脚本和 model.conf，目录里不能多出任何日志文件
        extra = [p.name for p in bin_dir.iterdir() if p.name not in {"claude", "codex", "model.conf"}]
        assert extra == []

    def test_codex_exec_appends_model(self, agent_env):
        bin_dir, paths = agent_env
        agents.write_model_config("gpt-fake", bin_dir)
        out = _run(paths["codex"], "exec", "hello")
        assert "arg:exec" in out
        assert "arg:-m" in out
        assert "arg:gpt-fake" in out
        assert "arg:hello" in out

    def test_codex_empty_model_omits_dash_m(self, agent_env):
        bin_dir, paths = agent_env
        agents.write_model_config("", bin_dir)
        out = _run(paths["codex"], "exec", "hello")
        assert "arg:-m" not in out
        assert "arg:hello" in out

    def test_codex_other_subcommands_pass_through(self, agent_env):
        bin_dir, paths = agent_env
        agents.write_model_config("gpt-fake", bin_dir)
        out = _run(paths["codex"], "--version")
        assert "arg:-m" not in out
        assert "arg:--version" in out

    def test_model_conf_change_takes_effect_immediately(self, agent_env):
        bin_dir, paths = agent_env
        agents.write_model_config("model-one", bin_dir)
        assert "arg:model-one" in _run(paths["claude"], "--print", "hi")
        # 改配置不用重新生成脚本，下一次调用立即生效
        agents.write_model_config("model-two", bin_dir)
        assert "arg:model-two" in _run(paths["claude"], "--print", "hi")

    def test_wrappers_are_private_and_executable(self, agent_env):
        _, paths = agent_env
        for script in paths.values():
            mode = script.stat().st_mode
            assert mode & stat.S_IXUSR
            assert not (mode & stat.S_IRWXO)  # 其他用户无任何权限

    def test_install_requires_real_executables(self, tmp_path):
        with pytest.raises(ValueError, match="claude_executable_not_found"):
            agents.install_wrappers(
                claude_path=tmp_path / "no-such-claude",
                codex_path=_fake_tool(tmp_path, "codex"),
                bin_dir=tmp_path / "bin",
            )
