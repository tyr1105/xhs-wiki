# -*- coding: utf-8 -*-
"""tests/test_llmwiki_engine.py —— LLM Wiki 引擎导入/整理流程的测试。

这个文件做什么：用一个**有状态的模拟后台**（http.server，端口 0，进程
内）覆盖引擎的导入、整理和衔接部分：
- importer.py：导入正常流程、预览先于解析任务、409/400 换键重发、会话
  里有不认识的项时停下、解析质量失败、发现需要确认、缓存满重启；
- compiler.py：整理正常流程、暂缓、丢引用自动驳回（前后文件没变）、
  10 个以上既有页面变更交给用户、读差异 git 超时重读、缓存满重启、
  额度用完熔断、completed 但没有 finalCommit、分组上限；
- review.py：light_review 纯函数（16 个用例）；
- engine.py：health / skill_context / source_exists / 来源引用落盘 /
  私有文件权限；
- 衔接：store 的 llmwiki_binary 校验、runner 的 llmwiki 引擎工厂、
  distill 的 deferred / needs_user 处理；
- 红线：令牌和签名链接不出现在日志、journal、引用文件和异常里。

模拟后台实现了 local-api.md 的关键行为：幂等键缓存（成功和**失败**
结果都缓存、换输入 409）、导入会话状态机、任务轮询推进、overview 三
个计数、workflows 状态流转、按游标翻页的差异、confirm / cancel、
sources 分页。所有数据都是代码生成的虚构数据；不访问网络（只连
127.0.0.1 的测试进程）、不调用真实模型、不启动真实 LLM Wiki。
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_runner import FIXED_NOW, FakeEngine, Rig, fake_items, nid  # noqa: F401

from xhs_wiki.engines.base import Package
from xhs_wiki.engines.llmwiki import (
    ApiError,
    Client,
    EngineUnavailable,
    LlmWikiEngine,
    ReviewRequired,
    light_review,
)
from xhs_wiki.llm.base import LLMError

FAKE_PROJECT_ID = "11111111-2222-4333-8444-555555555555"
TOKEN = "e" * 64  # 测试令牌：断言"绝不外泄"用的目标字符串


def make_package(i: int, text: str = "虚构正文") -> Package:
    """生成一条虚构解析包（编号、标题、正文全部代码生成）。"""

    return Package(
        note_id=nid(i), title=f"虚构笔记{i}",
        url=f"https://www.xiaohongshu.com/explore/{nid(i)}",
        author="虚构作者", published_at=None, list_position=i,
        package_md=f"# 虚构笔记{i}\n\n{text}\n",
    )


class _HttpError(Exception):
    """模拟后台内部抛出的错误响应。"""

    def __init__(self, status: int, code: str) -> None:
        self.status, self.code = status, code
        super().__init__(code)


class FakeState:
    """模拟后台的全部状态和测试脚本开关（见各字段的中文注释）。"""

    def __init__(self) -> None:
        self.token = TOKEN
        self.requests: list[dict] = []      # 只记方法/路径/键/布尔，绝不记令牌
        self.key_cache: dict = {}           # 幂等键 → 回执（失败也缓存）
        self.project_id = FAKE_PROJECT_ID
        self.project_root: Path | None = None
        self.sessions: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.task_polls: dict[str, int] = {}
        self.workflows: dict[str, dict] = {}
        self.sources: dict[str, dict] = {}
        self.rev = 1
        self.digest = "d1" * 8
        self.counters = {"session": 0, "task": 0, "source": 0, "workflow": 0}
        # ---- 测试脚本开关（默认全部关闭）----
        self.commit_error_once: tuple[int, str] | None = None  # 第一次 commit 回这个错
        self.cache_full_paths: list[str] = []   # 命中路径子串的下一个 POST 回 429
        self.diff_timeouts = 0                  # 前 N 次读差异回 400 GIT_COMMAND_TIMEOUT
        self.discovery_confirmation = False     # 发现任务要求 scans/accept
        self.preview_before_task = 0            # 解析任务拖 N 轮才成功
        self.parse_failure_code: str | None = None  # 解析任务失败码
        self.unknown_sibling = False            # 会话里带一个不认识的旧失败项
        self.workflow_plan: dict = {}           # 整理任务的候选/结果脚本

    def note(self, code: str) -> None:
        """占位（保持与 test_llmwiki_base.py 同款的入口命名）。"""

    # ---- 请求筛选辅助 ----

    def posts(self, fragment: str) -> list[dict]:
        return [r for r in self.requests
                if r["method"] == "POST" and fragment in r["path"]]

    def gets(self, fragment: str) -> list[dict]:
        return [r for r in self.requests
                if r["method"] == "GET" and fragment in r["path"]]


class StatefulWikiHandler(BaseHTTPRequestHandler):
    """有状态的模拟后台（路由和状态机见 FakeState / 模块 docstring）。"""

    # ---- 基础设施 -----------------------------------------------------------

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _ok(self, data: dict) -> tuple[int, dict]:
        return 200, {"data": data}

    def _err(self, status: int, code: str) -> tuple[int, dict]:
        return status, {"error": {"code": code, "message": "fake"}}

    def log_message(self, *args):  # 静默
        pass

    def do_GET(self):  # noqa: N802
        self._route("GET")

    def do_POST(self):  # noqa: N802
        self._route("POST")

    def _route(self, method: str) -> None:
        state = self.server.state
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        self.path, _, _query = self.path.partition("?")
        state.requests.append({
            "method": method, "path": self.path,
            "auth": self.headers.get("Authorization") == "Bearer " + state.token,
            "idem": self.headers.get("Idempotency-Key"),
            "body": body or None,
        })
        if not state.requests[-1]["auth"]:
            return self._send(*self._err(401, "API_UNAUTHORIZED"))
        try:
            if method == "POST":
                status, payload = self._cached_post(self.path, raw, body)
            else:
                status, payload = self._dispatch_get(self.path)
        except _HttpError as error:
            status, payload = error.status, {"error": {"code": error.code,
                                                       "message": "fake"}}
        self._send(status, payload)

    def _cached_post(self, path: str, raw: bytes, body: dict):
        """POST 统一走幂等键缓存：同键同输入回同一回执（失败也算），
        同键不同输入 409；重启后台（测试清空 key_cache）后同键会真正
        重新执行——这正是缓存满重启后"用原键重发"的依据。"""

        state = self.server.state
        key = self.headers.get("Idempotency-Key") or ""
        digest = hashlib.sha256(raw).hexdigest()
        cached = state.key_cache.get(key)
        if cached is not None:
            if cached["digest"] != digest or cached["path"] != path:
                return self._err(409, "API_IDEMPOTENCY_CONFLICT")
            return cached["status"], cached["payload"]
        for index, fragment in enumerate(list(state.cache_full_paths)):
            if fragment in path:
                state.cache_full_paths.pop(index)
                result = self._err(429, "API_RETRY_CACHE_FULL")
                state.key_cache[key] = {"digest": digest, "path": path,
                                        "status": result[0],
                                        "payload": result[1]}
                return result
        status, payload = self._dispatch_post(path, body)
        state.key_cache[key] = {"digest": digest, "path": path,
                                "status": status, "payload": payload}
        return status, payload

    # ---- GET 路由 -----------------------------------------------------------

    def _dispatch_get(self, path: str):
        state = self.server.state
        if path == "/api/v1/health":
            return self._ok({"ready": True, "webviewWindowCount": 0})
        if path == "/api/v1/projects/open":
            raise _HttpError(404, "API_NOT_FOUND")  # open 是 POST
        m = re.fullmatch(r"/api/v1/projects/([^/]+)/sources", path)
        if m:
            rows = [{"sourceId": sid, "versionId": row["versionId"],
                     "title": row["title"], "consumed": row["consumed"]}
                    for sid, row in sorted(state.sources.items())]
            return self._ok({"sources": rows, "total": len(rows),
                             "nextOffset": None, "unavailable": 0})
        m = re.fullmatch(r"/api/v1/projects/([^/]+)/imports/([^/]+)/overview", path)
        if m:
            return self._ok({
                "counts": {"active": 0},
                "selection": {"selected": 1, "newSources": 1},
                "selectionRevision": state.rev,
                "confirmationDigest": state.digest,
            })
        m = re.fullmatch(r"/api/v1/projects/([^/]+)/imports/([^/]+)", path)
        if m:
            self._settle_items(state, m.group(2))
            items = [{k: v for k, v in item.items() if k != "_locator_file"}
                     for item in state.sessions[m.group(2)]["items"]]
            return self._ok({"sessionId": m.group(2), "items": items})
        m = re.fullmatch(r"/api/v1/projects/([^/]+)/tasks/([^/]+)", path)
        if m:
            return self._ok(self._poll_task(state, m.group(2)))
        m = re.fullmatch(r"/api/v1/projects/([^/]+)/workflows/([^/]+)", path)
        if m:
            return self._ok(self._poll_workflow(state, m.group(2)))
        raise _HttpError(404, "API_NOT_FOUND")

    # ---- POST 路由 -----------------------------------------------------------

    def _dispatch_post(self, path: str, body: dict):
        state = self.server.state
        if path == "/api/v1/projects/open":
            return self._ok({"projectId": state.project_id})
        m = re.fullmatch(r"/api/v1/projects/([^/]+)/imports", path)
        if m:
            state.counters["session"] += 1
            sid = f"sess-{state.counters['session']:03d}"
            items = []
            if state.unknown_sibling:
                items.append({"itemId": "item-old", "status": "failed",
                              "input": {"normalizedLocator": "/other/old.md",
                                        "sourceIdentity": {"sha256": "0" * 64}},
                              "taskId": "task-old", "selected": False})
            state.sessions[sid] = {"items": items}
            return self._ok({"sessionId": sid})
        m = re.fullmatch(r"/api/v1/projects/([^/]+)/imports/([^/]+)/paths", path)
        if m:
            return self._ok(self._start_discovery(state, m.group(2), body))
        m = re.fullmatch(
                r"/api/v1/projects/([^/]+)/imports/([^/]+)/scans/([^/]+)/accept",
                path)
        if m:
            return self._ok({"accepted": True})
        m = re.fullmatch(r"/api/v1/projects/([^/]+)/imports/([^/]+)/commit", path)
        if m:
            return self._ok(self._commit(state, m.group(2), body))
        m = re.fullmatch(r"/api/v1/projects/([^/]+)/workflows/update-wiki", path)
        if m:
            return self._ok(self._start_workflow(state, body))
        m = re.fullmatch(
                r"/api/v1/projects/([^/]+)/workflows/([^/]+)/diff", path)
        if m:
            return self._ok(self._read_diff(state, m.group(2), body))
        m = re.fullmatch(
                r"/api/v1/projects/([^/]+)/workflows/([^/]+)/confirm", path)
        if m:
            return self._ok(self._confirm(state, m.group(2), body))
        m = re.fullmatch(
                r"/api/v1/projects/([^/]+)/workflows/([^/]+)/cancel", path)
        if m:
            run = state.workflows[m.group(2)]
            run["displayStatus"] = "cancelled"
            run["_auto"] = []
            run["pendingAction"] = None
            run["decisionReview"] = None
            return self._ok({"cancelled": True})
        raise _HttpError(404, "API_NOT_FOUND")

    # ---- 导入会话状态机 -------------------------------------------------------

    def _start_discovery(self, state: FakeState, sid: str, body: dict) -> dict:
        locator = body["sourcePaths"][0]
        content = Path(locator).read_bytes()
        state.counters["task"] += 1
        discovery_id = f"task-{state.counters['task']:03d}"
        task = {"id": discovery_id, "status": "succeeded"}
        if state.discovery_confirmation:
            task["requiresConfirmation"] = True
            task["scanToken"] = "scan-token-fake"
        state.tasks[discovery_id] = task
        state.counters["task"] += 1
        parse_id = f"task-{state.counters['task']:03d}"
        state.tasks[parse_id] = {
            "id": parse_id, "status": "running",
            "polls_left": state.preview_before_task,
            "failure_code": state.parse_failure_code,
        }
        item = {
            "itemId": f"item-{state.counters['task']:03d}",
            "status": "preview_ready" if state.preview_before_task else "parsing",
            "taskId": parse_id,
            "selected": True,
            "restrictedContent": False,
            "input": {"normalizedLocator": locator,
                      "sourceIdentity": {"sha256": hashlib.sha256(content).hexdigest()}},
        }
        if state.preview_before_task:
            item["preview"] = {"resolution": {"kind": "new_source"}}
        state.sessions[sid]["items"].append(item)
        return {"id": discovery_id}

    def _settle_task(self, task: dict) -> None:
        """把一个 running 的任务按脚本推进到终态（异步完成的模拟）。"""

        if task.get("status") != "running":
            return
        if task.get("polls_left", 0) > 0:
            task["polls_left"] -= 1
            return
        if task.get("failure_code"):
            task["status"] = "failed"
            task["error"] = {"code": task.pop("failure_code")}
        else:
            task["status"] = "succeeded"

    def _settle_items(self, state: FakeState, sid: str) -> None:
        """按解析任务的实际状态推进条目（任务异步完成，与轮询无关；
        预览可以早于任务最终状态落盘——preview_before_task 的场景）。"""

        for item in state.sessions[sid]["items"]:
            task_id = item.get("taskId")
            if item.get("status") != "parsing" or not task_id:
                continue
            task = state.tasks.get(task_id)
            if not task:
                continue
            self._settle_task(task)
            if task["status"] == "succeeded":
                item["status"] = "preview_ready"
                item["preview"] = {"resolution": {"kind": "new_source"}}
            elif task["status"] == "failed":
                item["status"] = "failed"
                item["errorCode"] = (task.get("error") or {}).get("code")

    def _commit(self, state: FakeState, sid: str, body: dict) -> dict:
        if state.commit_error_once is not None:
            error = state.commit_error_once
            state.commit_error_once = None
            state.rev += 1                      # 拒绝后快照刷新
            state.digest = "d2" * 8
            raise _HttpError(*error)
        if (body.get("expectedSelectionRevision") != state.rev
                or body.get("expectedConfirmationDigest") != state.digest):
            raise _HttpError(409, "IMPORT_V2_SELECTION_STALE")
        targets = [i for i in state.sessions[sid]["items"]
                   if i.get("status") in ("preview_ready", "completed")]
        if len(targets) != 1:
            raise _HttpError(400, "API_REQUEST_INVALID")
        state.counters["task"] += 1
        commit_id = f"task-{state.counters['task']:03d}"
        state.counters["source"] += 1
        source_id = f"src-{state.counters['source']:03d}"
        version_id = f"ver-{state.counters['source']:03d}"
        locator = targets[0]["input"]["normalizedLocator"]
        # 与真实后台一致（2026-11-02 真机联调）：导入条目本身不带来源编号，
        # 编号只写在提交任务结果的 reference.completion.newSources 里，
        # contentHash 是导入文件的 sha256。
        state.tasks[commit_id] = {
            "id": commit_id, "status": "succeeded",
            "result": {"reference": {"completion": {
                "newSources": [{
                    "sourceId": source_id, "versionId": version_id,
                    "wikiPath": f"wiki/sources/{source_id}.md",
                    "contentHash": hashlib.sha256(
                        Path(locator).read_bytes()).hexdigest(),
                }],
                "updatedSources": [], "duplicateSkips": [],
                "warnings": [], "failures": [],
            }}},
        }
        targets[0].update({"status": "completed", "selected": False})
        state.sources[source_id] = {
            "versionId": version_id, "title": Path(locator).name,
            "consumed": False,
        }
        # 真实后台会把来源页提升进 wiki/sources/；模拟后台照做一份，
        # 这样 light_review 的 known_source_paths 和 source_exists 都真实。
        page = state.project_root / "wiki" / "sources" / f"{source_id}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(Path(locator).read_text(encoding="utf-8"),
                        encoding="utf-8")
        return {"id": commit_id, "sourceId": source_id}

    # ---- 任务与工作流状态机 ----------------------------------------------------

    def _poll_task(self, state: FakeState, task_id: str) -> dict:
        task = state.tasks.get(task_id)
        if task is None:
            raise _HttpError(404, "API_NOT_FOUND")
        if task.get("status") == "running":
            if task.get("polls_left", 0) > 0:
                task["polls_left"] -= 1
            elif task.get("failure_code"):
                task["status"] = "failed"
                task["error"] = {"code": task.pop("failure_code")}
            else:
                task["status"] = "succeeded"
        state.task_polls[task_id] = state.task_polls.get(task_id, 0) + 1
        return {k: v for k, v in task.items()
                if k not in ("polls_left", "failure_code")}

    def _start_workflow(self, state: FakeState, body: dict) -> dict:
        state.counters["workflow"] += 1
        wid = f"wf-{state.counters['workflow']:03d}"
        run = {
            "taskId": wid,
            "displayStatus": "queued",
            "scope": {"kind": "update_wiki", "mode": "changed_sources",
                      "sourceVersions": body["selection"]["sourceVersions"]},
            "pendingAction": None, "decisionReview": None,
            "result": None, "error": None,
            "_auto": list(state.workflow_plan.get(
                "auto", ["running", "waiting_for_confirmation"])),
        }
        state.workflows[wid] = run
        return {"run": {"taskId": wid}}

    def _poll_workflow(self, state: FakeState, wid: str) -> dict:
        run = state.workflows.get(wid)
        if run is None:
            raise _HttpError(404, "API_NOT_FOUND")
        plan = state.workflow_plan
        if (run["_auto"] and run["displayStatus"]
                not in ("completed", "failed", "cancelled")):
            status = run["_auto"].pop(0)
            run["displayStatus"] = status
            if status == "waiting_for_confirmation":
                run["pendingAction"] = {
                    "id": f"pa-{wid}",
                    "actionType": "apply_candidate",
                    "affectedPaths": [f["path"] for f in plan["files"]],
                    "checkpointHash": "c" * 40,
                }
                run["decisionReview"] = {
                    "reason": "candidate_ready",
                    "counts": plan.get("counts",
                                       {"created": len(plan["files"]),
                                        "modified": 0, "overwritten": 0,
                                        "deleted": 0}),
                    "userEditsDetected": bool(plan.get("userEditsDetected")),
                    "fileDiffs": [{"fileId": f["fileId"], "path": f["path"],
                                   "kind": "two_way", "diffBytes": 0,
                                   "diff": None} for f in plan["files"]],
                }
            elif status == "failed":
                run["error"] = plan.get("error", {"code": "COMPILE_FAILED"})
            elif status == "completed":
                run["result"] = self._build_result(plan, run["scope"])
        return {k: v for k, v in run.items() if not k.startswith("_")}

    def _build_result(self, plan: dict, scope: dict) -> dict:
        deferred = set(plan.get("deferred", ()))
        outcomes = []
        for version in scope["sourceVersions"]:
            status = ("deferred" if version["sourceId"] in deferred
                      else "integrated")
            outcomes.append({
                "sourceVersion": version,
                "status": status,
                "integratedPaths": [],
                "sourcePath": "",
                "reason": plan.get("deferred_reason") if status == "deferred"
                else None,
            })
        return {"kind": "update_wiki", "created": 1, "updated": 0,
                "skipped": 0, "deleted": 0, "conflicted": 0,
                "sourceOutcomes": outcomes, "affectedPaths": [],
                "finalCommit": plan.get("final_commit", "a" * 40)}

    def _read_diff(self, state: FakeState, wid: str, body: dict) -> dict:
        run = state.workflows[wid]
        if run.get("displayStatus") not in ("waiting_for_confirmation",):
            raise _HttpError(400, "WORKFLOW_DIFF_NOT_FOUND")
        if state.diff_timeouts > 0:
            state.diff_timeouts -= 1
            raise _HttpError(400, "GIT_COMMAND_TIMEOUT")
        entry = next(f for f in state.workflow_plan["files"]
                     if f["fileId"] == body["fileId"])
        text = entry["diff"]
        start = int(body.get("cursor") or 0)
        limit = int(body.get("limitBytes") or 65536)
        end = min(start + limit, len(text))
        nxt = end if end < len(text) else None
        return {"fileId": entry["fileId"], "path": entry["path"],
                "kind": "two_way", "diff": text[start:end],
                "nextCursor": nxt, "truncated": nxt is not None}

    def _confirm(self, state: FakeState, wid: str, body: dict) -> dict:
        run = state.workflows[wid]
        pending = run.get("pendingAction")
        if not pending or pending["id"] != body.get("actionId"):
            raise _HttpError(400, "API_REQUEST_INVALID")
        run["displayStatus"] = "running"
        run["_auto"] = ["completed"]
        run["pendingAction"] = None
        run["decisionReview"] = None
        return {"confirmed": True}


class FakeBackendCtl:
    """假的后台控制器：替我们"拥有"的后台进程（不真正起进程）。

    compiler 遇到 429 API_RETRY_CACHE_FULL 时调用 restart_for_retry_cache
    ——这里记录次数并清空模拟后台的幂等键缓存，模拟"重启
    后回执缓存被清空、同键重发会真正执行"。
    """

    def __init__(self, state: FakeState, port: int) -> None:
        self.state = state
        self.port = port
        self.restarts = 0
        self.stop_calls = 0

    def is_running(self) -> bool:
        return True

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.stop_calls += 1

    def client(self) -> Client:
        return Client(self.port, lambda: self.state.token)

    def restart_for_retry_cache(self) -> None:
        self.restarts += 1
        self.state.key_cache.clear()


class FakeClock:
    """每次看表前进 0.05 秒的假时钟（轮询永不超时，测试跑得飞快）。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += 0.05
        return self.now


SETTINGS = {"llmwiki_binary": None,
            "llm": {"provider": "claude_code", "model": "claude-fake-5-5",
                    "base_url": None}}


@pytest.fixture()
def wiki(tmp_path, monkeypatch):
    """一套完整假环境：模拟后台 + 注入的 client/后台控制器 + 引擎。"""

    monkeypatch.setenv("XHS_WIKI_HOME", str(tmp_path / "home"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), StatefulWikiHandler)
    state = FakeState()
    state.project_root = tmp_path / "kb"
    server.state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    wrapper = FakeBackendCtl(state, port)
    engine = LlmWikiEngine(
        state.project_root, tmp_path / "home", SETTINGS,
        client=Client(port, lambda: state.token), backend=wrapper,
        sleep=lambda seconds: None, clock=FakeClock())
    try:
        yield SimpleNamespace(server=server, state=state, engine=engine,
                              wrapper=wrapper, tmp=tmp_path)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _new_page_diff(path: str, ref_line: str, extra: str = "新增内容") -> str:
    """生成"新建页面"的差异文本（```diff 围栏 + 统一 diff）。"""

    return (
        "```diff\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +2 @@\n"
        f"+sources: [\"{ref_line}\"]\n"
        f"+{extra}\n"
        "```\n"
    )


def _rewrite_diff(path: str, removed: list[str], added: list[str]) -> str:
    """生成"修改既有页面"的差异文本。"""

    lines = ["```diff", f"--- a/{path}", f"+++ b/{path}",
             f"@@ -1,{len(removed)} +1,{len(added)} @@"]
    lines += ["-" + line for line in removed]
    lines += ["+" + line for line in added]
    return "\n".join(lines) + "\n```\n"


def _ingest_one(wiki, index: int) -> dict:
    """用引擎导入一条虚构笔记，返回 ingest 结果。"""

    results = wiki.engine.ingest([make_package(index)])
    assert len(results) == 1
    return results[0]


def _prepare_distill(wiki, count: int) -> list[str]:
    """导入 count 条笔记并返回编号（供整理测试使用）。"""

    ids = []
    for i in range(count):
        result = _ingest_one(wiki, i)
        assert result.status == "ok", result.error
        ids.append(result.note_id)
    return ids


# ---------------------------------------------------------------------------
# light_review 纯函数测试
# ---------------------------------------------------------------------------

KNOWN = {"wiki/sources/src-001.md", "wiki/sources/src-002.md"}


def _pending(paths, **extra):
    return {"id": "pa-1", "affectedPaths": list(paths), **extra}


def _file(path, diff, existed=False):
    return {"file_id": "f1", "path": path, "diff": diff,
            "existed_before": existed}


NEW_PAGE = _new_page_diff("wiki/concepts/topic-a.md", "wiki/sources/src-001.md")


class TestLightReview:
    def test_confirm_new_page_with_known_ref(self):
        decision = light_review(_pending(["wiki/concepts/topic-a.md"]),
                                [_file("wiki/concepts/topic-a.md", NEW_PAGE)],
                                KNOWN)
        assert decision == {"decision": "confirm", "reason": "ok"}

    def test_confirm_existing_page_adding_known_ref(self):
        diff = _rewrite_diff(
            "wiki/concepts/a.md",
            ['sources: ["wiki/sources/src-001.md"]', "旧句子"],
            ['sources: ["wiki/sources/src-001.md", "wiki/sources/src-002.md"]',
             "新句子"])
        decision = light_review(_pending(["wiki/concepts/a.md"]),
                                [_file("wiki/concepts/a.md", diff, existed=True)],
                                KNOWN)
        assert decision["decision"] == "confirm"

    def test_reject_dropped_existing_ref(self):
        diff = _rewrite_diff(
            "wiki/concepts/a.md",
            ['sources: ["wiki/sources/src-001.md"]'],
            ["正文"])
        decision = light_review(_pending(["wiki/concepts/a.md"]),
                                [_file("wiki/concepts/a.md", diff, existed=True)],
                                KNOWN)
        assert decision == {"decision": "reject",
                            "reason": "dropped_existing_source_refs"}

    def test_reject_unknown_new_ref(self):
        diff = _new_page_diff("wiki/concepts/a.md", "wiki/sources/unknown.md")
        decision = light_review(_pending(["wiki/concepts/a.md"]),
                                [_file("wiki/concepts/a.md", diff)], KNOWN)
        assert decision == {"decision": "reject",
                            "reason": "unknown_source_refs"}

    def test_needs_user_on_deletion(self):
        decision = light_review(
            _pending([], counts={"deleted": 1, "modified": 0,
                                 "overwritten": 0, "created": 0}),
            [], KNOWN)
        assert decision == {"decision": "needs_user", "reason": "deleted_pages"}

    def test_needs_user_on_overwrite(self):
        decision = light_review(
            _pending([], counts={"deleted": 0, "modified": 0,
                                 "overwritten": 2, "created": 0}),
            [], KNOWN)
        assert decision == {"decision": "needs_user",
                            "reason": "overwritten_pages"}

    def test_needs_user_on_user_edits(self):
        decision = light_review(_pending([], userEditsDetected=True), [], KNOWN)
        assert decision == {"decision": "needs_user",
                            "reason": "user_edits_detected"}

    def test_needs_user_on_ten_reported_modified(self):
        decision = light_review(
            _pending([], counts={"deleted": 0, "modified": 10,
                                 "overwritten": 0, "created": 0}),
            [], KNOWN)
        assert decision == {"decision": "needs_user",
                            "reason": "existing_pages_changed_limit"}

    def test_needs_user_on_ten_existing_changed_files(self):
        files = []
        for i in range(10):
            path = f"wiki/concepts/page-{i}.md"
            files.append(_file(path, NEW_PAGE, existed=True))
        decision = light_review(_pending([f["path"] for f in files]),
                                files, KNOWN)
        assert decision == {"decision": "needs_user",
                            "reason": "existing_pages_changed_limit"}

    def test_ten_new_pages_do_not_trigger_user(self):
        files = [_file(f"wiki/concepts/new-{i}.md", NEW_PAGE, existed=False)
                 for i in range(12)]
        decision = light_review(_pending([f["path"] for f in files]),
                                files, KNOWN)
        assert decision["decision"] == "confirm"

    def test_empty_context_diffs_are_not_changes(self):
        # V8 的"空差异"（只有围栏和文件头）不算改动，affectedPaths 为空。
        empty = "```diff\n--- a/wiki/index.md\n+++ b/wiki/index.md\n```\n"
        decision = light_review(_pending([]),
                                [_file("wiki/index.md", empty, existed=True)],
                                KNOWN)
        assert decision["decision"] == "confirm"

    def test_needs_user_on_affected_paths_mismatch(self):
        decision = light_review(_pending(["wiki/concepts/other.md"]),
                                [_file("wiki/concepts/a.md", NEW_PAGE)], KNOWN)
        assert decision == {"decision": "needs_user",
                            "reason": "affected_paths_mismatch"}

    def test_needs_user_on_path_outside_wiki(self):
        decision = light_review(_pending(["purpose.md"]),
                                [_file("purpose.md", NEW_PAGE)], KNOWN)
        assert decision == {"decision": "needs_user", "reason": "path_invalid"}

    def test_needs_user_on_path_escape(self):
        decision = light_review(_pending(["wiki/../evil.md"]),
                                [_file("wiki/../evil.md", NEW_PAGE)], KNOWN)
        assert decision["decision"] == "needs_user"

    def test_needs_user_on_empty_diff_entry(self):
        decision = light_review(_pending(["wiki/concepts/a.md"]),
                                [_file("wiki/concepts/a.md", "  ")], KNOWN)
        assert decision == {"decision": "needs_user", "reason": "empty_diff"}

    def test_needs_user_on_source_page_modification(self):
        decision = light_review(
            _pending(["wiki/sources/src-001.md"]),
            [_file("wiki/sources/src-001.md",
                   _rewrite_diff("wiki/sources/src-001.md", ["原文"], ["改写"]),
                   existed=True)],
            KNOWN)
        assert decision == {"decision": "needs_user",
                            "reason": "source_page_modified"}

    def test_raw_sources_ref_also_recognized(self):
        # 兼容 raw/sources/... 的引用写法（原生布局的原始材料路径）。
        diff = _new_page_diff("wiki/concepts/a.md", "raw/sources/x/1/original.md")
        known = {"raw/sources/x/1/original.md"}
        decision = light_review(_pending(["wiki/concepts/a.md"]),
                                [_file("wiki/concepts/a.md", diff)], known)
        assert decision["decision"] == "confirm"


# ---------------------------------------------------------------------------
# importer：导入流程
# ---------------------------------------------------------------------------

class TestImport:
    def test_normal_flow(self, wiki):
        result = _ingest_one(wiki, 0)
        assert result.status == "ok"
        assert result.engine_ref["source_id"] == "src-001"
        assert result.engine_ref["version_id"] == "ver-001"
        # 导入的 md 已写进导入根目录（0600）
        md = wiki.tmp / "home" / "llmwiki-import" / f"{nid(0)}.md"
        assert md.is_file()
        assert stat.S_IMODE(md.stat().st_mode) == 0o600
        # 提交后用 /sources 核实过
        assert any("sources" in r["path"] and r["method"] == "GET"
                   for r in wiki.state.requests)
        # 来源引用持久化在 APP_HOME（0600）
        refs_file = wiki.tmp / "home" / "llmwiki-sources.json"
        refs = json.loads(refs_file.read_text())
        assert refs[nid(0)]["source_id"] == "src-001"
        assert stat.S_IMODE(refs_file.stat().st_mode) == 0o600

    def test_preview_ready_before_parse_task(self, wiki):
        wiki.state.preview_before_task = 2  # 条目立刻 preview_ready，任务还拖 2 轮
        result = _ingest_one(wiki, 1)
        assert result.status == "ok"
        # 解析任务确实被轮询到成功（预览早于状态落盘的等待）
        parse_task = wiki.state.tasks["task-002"]
        assert parse_task["status"] == "succeeded"
        assert wiki.state.task_polls["task-002"] >= 3

    def test_selection_stale_then_refresh(self, wiki):
        wiki.state.commit_error_once = (409, "IMPORT_V2_SELECTION_STALE")
        result = _ingest_one(wiki, 2)
        assert result.status == "ok"
        # 原请求被归档（原因 selection_stale_rejected），换键重发成功
        journal = wiki.engine._journal_or_new()
        history = journal.history_of(f"{nid(2)}-commit")
        assert len(history) == 1
        assert history[0]["reason"] == "selection_stale_rejected"
        commits = wiki.state.posts("/commit")
        assert len(commits) == 2
        assert commits[0]["idem"] != commits[1]["idem"]
        assert commits[1]["body"]["expectedSelectionRevision"] == 2

    def test_git_command_failed_then_refresh(self, wiki):
        wiki.state.commit_error_once = (400, "GIT_COMMAND_FAILED")
        result = _ingest_one(wiki, 3)
        assert result.status == "ok"
        journal = wiki.engine._journal_or_new()
        history = journal.history_of(f"{nid(3)}-commit")
        assert history[0]["reason"] == "git_command_failed_rejected"

    def test_unknown_session_item_stops(self, wiki):
        wiki.state.unknown_sibling = True
        results = wiki.engine.ingest([make_package(4)])
        assert results[0].status == "failed"
        assert results[0].error == "unknown_session_item"
        # 绝没有走到提交
        assert wiki.state.posts("/commit") == []

    def test_parse_quality_failed(self, wiki):
        wiki.state.parse_failure_code = "IMPORT_V2_QUALITY_FAILED"
        results = wiki.engine.ingest([make_package(5)])
        assert results[0].status == "failed"
        assert results[0].error == "IMPORT_V2_QUALITY_FAILED"
        assert wiki.state.posts("/commit") == []

    def test_discovery_requires_accept(self, wiki):
        wiki.state.discovery_confirmation = True
        result = _ingest_one(wiki, 6)
        assert result.status == "ok"
        assert len(wiki.state.posts("/accept")) == 1

    def test_cache_full_restarts_and_resends(self, wiki):
        wiki.state.cache_full_paths = ["/commit"]
        result = _ingest_one(wiki, 7)
        assert result.status == "ok"
        assert wiki.wrapper.restarts == 1
        # 同一个幂等键被重发（重启清空了后台的回执缓存）
        commits = wiki.state.posts("/commit")
        assert len(commits) == 2
        assert commits[0]["idem"] == commits[1]["idem"]

    def test_import_md_private_and_outside_kb(self, wiki):
        _ingest_one(wiki, 8)
        md = wiki.tmp / "home" / "llmwiki-import" / f"{nid(8)}.md"
        assert md.is_file()
        assert not (wiki.state.project_root / f"{nid(8)}.md").exists()


# ---------------------------------------------------------------------------
# compiler：整理流程
# ---------------------------------------------------------------------------

class TestDistill:
    def test_normal_flow_confirms_and_verifies(self, wiki):
        ids = _prepare_distill(wiki, 1)
        wiki.state.workflow_plan = {
            "files": [{"fileId": "f1", "path": "wiki/concepts/topic-a.md",
                       "diff": _new_page_diff(
                           "wiki/concepts/topic-a.md",
                           "wiki/sources/src-001.md" + " " * 70000)}],
            "counts": {"created": 1, "modified": 0, "overwritten": 0,
                       "deleted": 0},
        }
        result = wiki.engine.distill(ids)
        assert result.per_note[ids[0]]["status"] == "ok"
        assert result.status == "ok"
        # 读差异按游标翻了页（大差异被切成多段）
        diffs = wiki.state.posts("/diff")
        assert len(diffs) >= 2
        # 确认前重读过工作流并核对了 pendingAction.id，然后 confirm
        assert len(wiki.state.posts("/confirm")) == 1

    def test_deferred_source_outcome(self, wiki):
        ids = _prepare_distill(wiki, 1)
        wiki.state.workflow_plan = {
            "files": [{"fileId": "f1", "path": "wiki/concepts/a.md",
                       "diff": _new_page_diff("wiki/concepts/a.md",
                                              "wiki/sources/src-001.md")}],
            "deferred": ["src-001"], "deferred_reason": "single source",
        }
        result = wiki.engine.distill(ids)
        outcome = result.per_note[ids[0]]
        assert outcome["status"] == "deferred"
        assert outcome["code"] == "single source"
        assert result.status == "partial"

    def _drop_ref_plan(self, wiki):
        """既有页面 + 会丢掉来源引用的候选（已登记来源 src-001）。"""

        page = wiki.state.project_root / "wiki" / "concepts" / "existing.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            "---\nsources: [\"wiki/sources/src-001.md\"]\n---\n旧内容\n",
            encoding="utf-8")
        return {
            "files": [{
                "fileId": "f1", "path": "wiki/concepts/existing.md",
                "diff": _rewrite_diff(
                    "wiki/concepts/existing.md",
                    ["---", 'sources: ["wiki/sources/src-001.md"]', "---",
                     "旧内容"],
                    ["新内容"]),
            }],
            "counts": {"created": 0, "modified": 1, "overwritten": 0,
                       "deleted": 0},
        }

    def test_dropped_refs_rejected_without_changes(self, wiki):
        ids = _prepare_distill(wiki, 1)
        wiki.state.workflow_plan = self._drop_ref_plan(wiki)
        page = wiki.state.project_root / "wiki" / "concepts" / "existing.md"
        before = page.read_bytes()
        result = wiki.engine.distill(ids)
        assert result.per_note[ids[0]] == {
            "status": "failed",
            "code": "review_dropped_existing_source_refs"}
        # 驳回用的正式 cancel 接口；候选涉及文件前后没变
        assert len(wiki.state.posts("/cancel")) == 1
        assert page.read_bytes() == before

    def test_many_existing_pages_needs_user(self, wiki):
        ids = _prepare_distill(wiki, 1)
        files = []
        for i in range(11):
            path = f"wiki/concepts/page-{i}.md"
            target = wiki.state.project_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"旧内容 {i}\n", encoding="utf-8")
            files.append({"fileId": f"f{i}", "path": path,
                          "diff": _rewrite_diff(path, [f"旧内容 {i}"],
                                                [f"新内容 {i}"])})
        wiki.state.workflow_plan = {"files": files}
        with pytest.raises(ReviewRequired) as info:
            wiki.engine.distill(ids)
        assert info.value.reason == "existing_pages_changed_limit"
        # 没有确认、也没有驳回——等用户在桌面版里决定
        assert wiki.state.posts("/confirm") == []
        assert wiki.state.posts("/cancel") == []

    def test_diff_git_timeout_reread(self, wiki):
        ids = _prepare_distill(wiki, 1)
        wiki.state.diff_timeouts = 1
        wiki.state.workflow_plan = {
            "files": [{"fileId": "f1", "path": "wiki/concepts/a.md",
                       "diff": _new_page_diff("wiki/concepts/a.md",
                                              "wiki/sources/src-001.md")}],
        }
        result = wiki.engine.distill(ids)
        assert result.per_note[ids[0]]["status"] == "ok"
        # 第一次读差异超时，换新键重读成功（两个不同的幂等键）
        diffs = wiki.state.posts("/diff")
        assert len(diffs) == 2
        assert diffs[0]["idem"] != diffs[1]["idem"]

    def test_cache_full_restart_and_resend(self, wiki):
        ids = _prepare_distill(wiki, 1)
        wiki.state.cache_full_paths = ["update-wiki"]
        wiki.state.workflow_plan = {
            "files": [{"fileId": "f1", "path": "wiki/concepts/a.md",
                       "diff": _new_page_diff("wiki/concepts/a.md",
                                              "wiki/sources/src-001.md")}],
        }
        result = wiki.engine.distill(ids)
        assert result.per_note[ids[0]]["status"] == "ok"
        assert wiki.wrapper.restarts == 1
        updates = wiki.state.posts("update-wiki")
        assert len(updates) == 2
        assert updates[0]["idem"] == updates[1]["idem"]  # 原键重发

    def test_quota_exhausted_breaks(self, wiki):
        ids = _prepare_distill(wiki, 1)
        wiki.state.workflow_plan = {
            "auto": ["running", "failed"],
            "error": {"code": "AGENT_EXIT_FAILED",
                      "details": {"agentFailureCategory": "quota_limit"}},
        }
        with pytest.raises(LLMError) as info:
            wiki.engine.distill(ids)
        assert info.value.code == "quota_or_rate_limit"

    def test_not_logged_in_breaks(self, wiki):
        ids = _prepare_distill(wiki, 1)
        wiki.state.workflow_plan = {
            "auto": ["running", "failed"],
            "error": {"code": "AGENT_EXIT_FAILED",
                      "details": {"agentFailureCategory": "auth_required"}},
        }
        with pytest.raises(LLMError) as info:
            wiki.engine.distill(ids)
        assert info.value.code == "not_logged_in"

    def test_completed_without_final_commit_fails(self, wiki):
        ids = _prepare_distill(wiki, 1)
        wiki.state.workflow_plan = {
            "files": [{"fileId": "f1", "path": "wiki/concepts/a.md",
                       "diff": _new_page_diff("wiki/concepts/a.md",
                                              "wiki/sources/src-001.md")}],
            "final_commit": None,  # completed 但没有提交号
        }
        result = wiki.engine.distill(ids)
        assert result.per_note[ids[0]] == {"status": "failed",
                                           "code": "no_final_commit"}

    def test_workflow_failed_records_code(self, wiki):
        ids = _prepare_distill(wiki, 2)
        wiki.state.workflow_plan = {
            "auto": ["running", "failed"],
            "error": {"code": "COMPILE_OUTPUT_INVALID"},
        }
        result = wiki.engine.distill(ids)
        for note_id in ids:
            assert result.per_note[note_id] == {
                "status": "failed", "code": "COMPILE_OUTPUT_INVALID"}

    def test_grouping_limits(self, wiki):
        ids = _prepare_distill(wiki, 7)
        wiki.state.workflow_plan = {
            "files": [{"fileId": "f1", "path": "wiki/concepts/a.md",
                       "diff": _new_page_diff("wiki/concepts/a.md",
                                              "wiki/sources/src-001.md")}],
        }
        result = wiki.engine.distill(ids)
        updates = wiki.state.posts("update-wiki")
        assert len(updates) == 2  # 5 + 2 两组
        sizes = [len(u["body"]["selection"]["sourceVersions"]) for u in updates]
        assert sizes == [5, 2]
        assert all(u["body"]["routeSelection"] == {"kind": "agent",
                                                   "agent": "claude"}
                   for u in updates)
        assert result.status == "ok"  # 两组都成功

    def test_unknown_note_fails_with_ref_missing(self, wiki):
        result = wiki.engine.distill([nid(99)])
        assert result.per_note[nid(99)] == {"status": "failed",
                                            "code": "source_ref_missing"}


# ---------------------------------------------------------------------------
# engine：health / skill_context / source_exists / 后台管理
# ---------------------------------------------------------------------------

class TestEngine:
    def test_health_ok(self, wiki):
        assert wiki.engine.health() == (True, "ok")
        # 知识库脚手架已就位（不存在的目录会被 scaffold 搭出来）
        assert (wiki.state.project_root / ".app" / "project.json").is_file()

    def test_health_binary_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XHS_WIKI_HOME", str(tmp_path / "home"))
        engine = LlmWikiEngine(tmp_path / "kb", tmp_path / "home",
                               {"llmwiki_binary": None, "llm": {}})
        ok, reason = engine.health()
        assert ok is False
        assert reason == "llmwiki_binary_missing"

    def test_health_binary_not_a_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XHS_WIKI_HOME", str(tmp_path / "home"))
        engine = LlmWikiEngine(
            tmp_path / "kb", tmp_path / "home",
            {"llmwiki_binary": str(tmp_path / "no-such-binary"), "llm": {}})
        ok, reason = engine.health()
        assert ok is False
        assert reason == "llmwiki_binary_missing"

    def test_health_desktop_running(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XHS_WIKI_HOME", str(tmp_path / "home"))

        class Busy:
            def is_running(self):
                return False

            def start(self):
                raise EngineUnavailable("llmwiki_desktop_running")

        engine = LlmWikiEngine(tmp_path / "kb", tmp_path / "home",
                               SETTINGS, backend=Busy(),
                               wrapper_installer=lambda **kwargs: {})
        assert engine.health() == (False, "llmwiki_desktop_running")

    def test_skill_context(self, wiki):
        context = wiki.engine.skill_context()
        assert context == {
            "kb_root": str(wiki.state.project_root),
            "engine": "llmwiki",
            "index": "wiki/index.md",
            "topics_dir": "wiki",
            "sources_dir": "raw/sources",
        }

    def test_source_exists_true_and_false(self, wiki):
        result = _ingest_one(wiki, 20)
        ref = result.engine_ref
        assert wiki.engine.source_exists(result.note_id, ref) is True
        # 版本变了（比如来源被更新过）就不算"仍然是它"
        assert wiki.engine.source_exists(
            result.note_id, {"source_id": ref["source_id"],
                             "version_id": "ver-999"}) is False
        assert wiki.engine.source_exists(
            result.note_id, {"source_id": "src-999",
                             "version_id": "ver-001"}) is False

    def test_close_stops_backend(self, wiki):
        wiki.engine.close()
        assert wiki.wrapper.stop_calls == 1

    def test_model_config_written(self, wiki):
        wiki.engine.health()
        conf = wiki.tmp / "home" / "llmwiki-bin" / "model.conf"
        assert conf.is_file()
        assert "claude-fake-5-5" in conf.read_text()

    def test_codex_provider_maps_to_codex_agent(self, wiki):
        ids = _prepare_distill(wiki, 1)
        wiki.engine.settings = {"llmwiki_binary": None,
                                "llm": {"provider": "codex",
                                        "model": "gpt-fake", "base_url": None}}
        wiki.state.workflow_plan = {
            "files": [{"fileId": "f1", "path": "wiki/concepts/a.md",
                       "diff": _new_page_diff("wiki/concepts/a.md",
                                              "wiki/sources/src-001.md")}],
        }
        result = wiki.engine.distill(ids)
        assert result.per_note[ids[0]]["status"] == "ok"
        update = wiki.state.posts("update-wiki")[-1]
        assert update["body"]["routeSelection"] == {"kind": "agent",
                                                    "agent": "codex"}


# ---------------------------------------------------------------------------
# 红线：令牌 / 签名链接绝不外泄
# ---------------------------------------------------------------------------

class TestSecrets:
    def test_token_not_in_journal_or_files(self, wiki):
        _prepare_distill(wiki, 1)
        wiki.state.workflow_plan = {
            "files": [{"fileId": "f1", "path": "wiki/concepts/a.md",
                       "diff": _new_page_diff("wiki/concepts/a.md",
                                              "wiki/sources/src-001.md")}],
        }
        wiki.engine.distill([nid(0)])
        journal_bytes = (wiki.tmp / "home" / "llmwiki-journal.sqlite3"
                         ).read_bytes()
        assert TOKEN.encode() not in journal_bytes
        refs_bytes = (wiki.tmp / "home" / "llmwiki-sources.json"
                      ).read_bytes()
        assert TOKEN.encode() not in refs_bytes
        for path in (wiki.tmp / "home").rglob("*"):
            if path.is_file():
                assert TOKEN.encode() not in path.read_bytes(), path

    def test_no_signed_links_anywhere(self, wiki):
        _prepare_distill(wiki, 1)
        for path in (wiki.tmp / "home").rglob("*"):
            if path.is_file():
                assert b"xsec_token" not in path.read_bytes(), path
        assert wiki.state.posts("/commit") != []

    def test_requests_log_never_holds_token(self, wiki):
        _ingest_one(wiki, 30)
        for entry in wiki.state.requests:
            assert entry["auth"] is True  # 令牌每次都对了
            assert TOKEN not in json.dumps(entry)


# ---------------------------------------------------------------------------
# 衔接：store 的 llmwiki_binary、runner 的引擎工厂与事项
# ---------------------------------------------------------------------------

class TestStoreSettings:
    def _merged(self, **changes):
        from xhs_wiki.store import DEFAULT_SETTINGS
        merged = json.loads(json.dumps(DEFAULT_SETTINGS))
        merged.update(changes)
        return merged

    def test_default_has_llmwiki_binary(self):
        from xhs_wiki.store import DEFAULT_SETTINGS
        assert DEFAULT_SETTINGS["llmwiki_binary"] is None

    def test_validate_accepts_null_and_absolute(self):
        from xhs_wiki.store import validate_settings
        validate_settings(self._merged(llmwiki_binary=None))
        validate_settings(self._merged(
            llmwiki_binary="/Applications/Fake.app/Contents/MacOS/fake"))

    @pytest.mark.parametrize("bad", ["", "relative/path", "~/bin/fake", 42])
    def test_validate_rejects_bad_values(self, bad):
        from xhs_wiki.store import validate_settings
        with pytest.raises(ValueError, match="llmwiki_binary_invalid"):
            validate_settings(self._merged(llmwiki_binary=bad))

    def test_update_settings_can_set_binary(self, tmp_path):
        from xhs_wiki.store import ServiceStore
        store = ServiceStore(tmp_path / "service")
        settings = store.update_settings(
            {"llmwiki_binary": "/opt/fake/llm-wiki-desktop"})
        assert settings["llmwiki_binary"] == "/opt/fake/llm-wiki-desktop"

    def test_llmwiki_review_required_title_registered(self, tmp_path):
        import xhs_wiki.runner  # noqa: F401  注册在 import 时发生
        from xhs_wiki.store import NEEDS_USER_TITLES, ServiceStore
        assert "llmwiki_review_required" in NEEDS_USER_TITLES
        store = ServiceStore(tmp_path / "service")
        store.add_needs_user("llmwiki_review_required", "测试")
        items = store.read_status()["needs_user"]
        assert items[0]["code"] == "llmwiki_review_required"


class TestRunnerGlue:
    def test_default_deps_makes_llmwiki_engine(self, tmp_path, monkeypatch):
        from xhs_wiki import config, runner
        monkeypatch.setenv("XHS_WIKI_HOME", str(tmp_path / "home"))
        deps = runner.default_deps()
        engine = deps.make_engine({
            "engine": "llmwiki", "kb_root": str(tmp_path / "kb"),
            "llm": {"provider": "codex", "model": "gpt-fake",
                    "base_url": None}})
        assert isinstance(engine, LlmWikiEngine)
        assert engine.name == "llmwiki"

    def test_distill_review_required_pauses_run(self, tmp_path, monkeypatch):
        class ReviewEngine(FakeEngine):
            def distill(self, note_ids):
                raise ReviewRequired("existing_pages_changed_limit")

        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(1),
                  engine=ReviewEngine())
        record = rig.run()
        assert record["result"] == "needs_user"
        assert record["stop_reason"] == "llmwiki_review_required"
        # 笔记停在 ingested（等用户确认后重试），不算失败
        assert rig.note(0)["stage"] == "ingested"
        items = rig.store.read_status()["needs_user"]
        assert items[0]["code"] == "llmwiki_review_required"

    def test_distill_deferred_goes_to_ledger_deferred(self, tmp_path,
                                                      monkeypatch):
        class DeferredEngine(FakeEngine):
            def distill(self, note_ids):
                return SimpleNamespace(
                    status="partial",
                    per_note={n: {"status": "deferred", "code": "主题孤立"}
                              for n in note_ids},
                    errors=[])

        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(1),
                  engine=DeferredEngine())
        record = rig.run()
        assert record["result"] == "completed"
        note = rig.note(0)
        assert note["stage"] == "deferred"
        assert note["deferred_reason"] == "主题孤立"

    def test_engine_closed_after_run(self, tmp_path, monkeypatch):
        class ClosingEngine(FakeEngine):
            def __init__(self):
                super().__init__()
                self.closed = 0

            def close(self):
                self.closed += 1

        engine = ClosingEngine()
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(1),
                  engine=engine)
        rig.run()
        assert engine.closed == 1


# ---- 提交结果里认出我们这条来源（2026-11-02 真机联调后补） -----------------

def _commit_task(new=(), updated=()):
    """按真实后台的格式拼一个提交任务：编号在 result.reference.completion 里。"""

    return {"status": "succeeded", "result": {"reference": {"completion": {
        "newSources": list(new), "updatedSources": list(updated),
        "duplicateSkips": [], "warnings": [], "failures": []}}}}


class TestCommittedSource:
    """importer._committed_source：只认内容指纹等于导入文件 sha256 的那一项。"""

    HASH = "ab" * 32

    def _entry(self, sid, vid, content_hash):
        return {"sourceId": sid, "versionId": vid,
                "wikiPath": f"wiki/sources/local/{sid}.md", "contentHash": content_hash}

    def test_new_source_matched_by_content_hash(self):
        from xhs_wiki.engines.llmwiki.importer import _committed_source
        task = _commit_task(new=[self._entry("s1", "v1", self.HASH)])
        assert _committed_source(task, self.HASH) == ("s1", "v1")

    def test_updated_source_also_counts(self):
        from xhs_wiki.engines.llmwiki.importer import _committed_source
        task = _commit_task(updated=[self._entry("s2", "v9", self.HASH)])
        assert _committed_source(task, self.HASH) == ("s2", "v9")

    def test_other_content_is_not_ours(self):
        from xhs_wiki.engines.llmwiki.importer import _committed_source
        task = _commit_task(new=[self._entry("s1", "v1", "cd" * 32)])
        assert _committed_source(task, self.HASH) == (None, None)

    def test_ambiguous_match_stops(self):
        from xhs_wiki.engines.llmwiki.importer import _committed_source
        task = _commit_task(new=[self._entry("s1", "v1", self.HASH),
                                 self._entry("s2", "v2", self.HASH)])
        assert _committed_source(task, self.HASH) == (None, None)

    def test_missing_or_malformed_result_stops(self):
        from xhs_wiki.engines.llmwiki.importer import _committed_source
        for task in ({}, {"result": None}, {"result": {"reference": "x"}},
                     {"result": {"reference": {"completion": []}}}):
            assert _committed_source(task, self.HASH) == (None, None)
