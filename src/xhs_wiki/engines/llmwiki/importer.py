# -*- coding: utf-8 -*-
"""engines/llmwiki/importer.py —— 把一条解析包导入 LLM Wiki 知识库。

这个文件做什么：实现 7 步导入流程——
  1. 把 package_md 写进导入根目录（APP_HOME/llmwiki-import/<note_id>.md）；
  2. POST /imports 建会话（后台可能复用带旧失败项的会话）；
  3. POST .../paths 发起文件发现任务并等它成功（需要确认时走 scans/accept）；
  4. 轮询会话条目，用 normalizedLocator + sha256 锁定我们自己这一项；
  5. 等这一项的解析任务真正成功后再重读一次（预览比状态落盘早）；
  6. GET .../overview：active=0、selected=1、newSources=1 才允许提交；
  7. POST .../commit → 等任务成功 → 用 GET /sources 核实来源真的入库。

每个写请求都通过 journal 登记操作名、幂等键和请求签名：
先把键落盘再发送，崩溃恢复后不会盲目造出第二个导入。其他规则：
- 会话里有不认识的其他条目就停下（unknown_session_item），绝不把别人
  的项一起提交（不去逐项辨认这些条目，直接停下交给人）；
- 409 IMPORT_V2_SELECTION_STALE 和 400 GIT_COMMAND_FAILED 都发生在
  建任务之前：核对原项没变后丢弃原请求、取新快照、换新键
  重发，每种最多 2 次；
- 解析任务失败（IMPORT_* 错误码）记为 failed，code 用后台的返回。

在整体里的位置：engine.ingest 对每个解析包调用一次 import_package。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .client import ApiError
from .journal import Journal
from .project import open_project

# 轮询导入任务 / 会话条目的最长时间（秒）。
TASK_TIMEOUT_S = 180
SESSION_TIMEOUT_S = 180
# 每次轮询的间隔（秒）。
POLL_INTERVAL_S = 1

# 会话条目的状态：可以进入确认流程的两个状态。
_ITEM_READY = ("preview_ready", "completed")
# 会话条目全体到达这些状态之一后，才停止"等解析"的轮询。
_ITEM_SETTLED = ("preview_ready", "needs_merge", "failed", "completed")
# 任务仍在进行中的状态。
_TASK_ACTIVE = ("queued", "running", "cancelling")

# 409/400 重发时 journal 归档用的原因名（每种最多 2 次）。
_REASON_SELECTION_STALE = "selection_stale_rejected"
_REASON_GIT_COMMAND_FAILED = "git_command_failed_rejected"


def _atomic_write_private(path: Path, content: str) -> bytes:
    """把文本原子写成 0600 的私有文件，返回内容的 UTF-8 字节。

    先写临时文件再 os.replace（见 CONTRIBUTING.md 的代码规范）；中途失败清掉临时文件。
    """

    payload = content.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return payload


@dataclass
class ApiContext:
    """一次引擎连接的上下文：客户端、请求日志、项目句柄和可注入的时钟。

    - client / journal：HTTP 客户端和请求日志；
    - project_root / import_root：知识库目录和导入包目录（都已在磁盘上）；
    - backend：我们启动的无窗口后台（可以没有——测试直接注入 client）；
    - sleep / clock：可注入的睡眠和单调时钟，测试里分别换成空操作和
      可快进的假时钟；
    - project_id：打开项目后后台给的句柄；后台重启后会变化，所以提供
      reopen() 重新打开（重启后按原 taskId 查询，不重新建任务）。
    """

    client: object
    journal: Journal
    project_root: Path
    import_root: Path
    backend: object | None = None
    project_id: str | None = None
    sleep: object = None
    clock: object = None

    def __post_init__(self) -> None:
        if self.sleep is None:
            self.sleep = time.sleep
        if self.clock is None:
            self.clock = time.monotonic

    def base(self) -> str:
        """当前项目的 API 路径前缀 /projects/{projectId}。"""

        if not self.project_id:
            raise ValueError("project_not_open")
        return f"/projects/{self.project_id}"

    def reopen(self, timeout: float = 900) -> str:
        """重新打开项目（后台重启后项目句柄可能变化）。"""

        self.project_id = open_project(self.client, self.project_root, timeout=timeout)
        return self.project_id


def api_get(ctx: ApiContext, path: str) -> dict:
    """发一个只读 GET，返回回执里的 data（异常原样上抛）。"""

    _, payload = ctx.client.request("GET", path)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError("api_response_missing_data")
    return data


def _wait_task(ctx: ApiContext, task_id: str, *, timeout_s: float = TASK_TIMEOUT_S) -> dict:
    """轮询一个后台任务直到它脱离"进行中"状态。

    返回最后一次的任务对象（status 可能是 succeeded，也可能是 failed /
    cancelled 等，由调用方分辨）；超过时限抛 ValueError（错误代码带任务
    所处的状态，绝不带任务内容）。
    """

    deadline = ctx.clock() + timeout_s
    while True:
        task = api_get(ctx, f"{ctx.base()}/tasks/{task_id}")
        if task.get("status") not in _TASK_ACTIVE:
            return task
        if ctx.clock() > deadline:
            raise ValueError(f"native_task_still_running:{task.get('status')}")
        ctx.sleep(POLL_INTERVAL_S)


def _checked_target(items: list, locator: str, sha256_hex: str) -> dict:
    """在会话条目里锁定我们自己这一项（normalizedLocator + sha256）。

    - 匹配不到或匹配出多条：import_requires_review（结果不明就停下）；
    - 输入摘要变了：confirmation_original_input_changed；
    - 存在"不认识的其他条目"（既不是我们这项，也拿不出它是已入库来源
      的证据）：unknown_session_item——后台复用旧会话时可能带进来别人
      的失败项，绝不能把别人的项一起提交。
    """

    matches = [i for i in items
               if isinstance(i, dict)
               and (i.get("input") or {}).get("normalizedLocator") == locator]
    if len(matches) != 1:
        raise ValueError("import_requires_review")
    target = matches[0]
    identity = (target.get("input") or {}).get("sourceIdentity") or {}
    if identity.get("sha256") != sha256_hex:
        raise ValueError("confirmation_original_input_changed")
    for item in items:
        if item is target or not isinstance(item, dict):
            continue
        # 唯一认识的"其他项"是已经完成的条目（它对应知识库里真实存在的
        # 来源，不会进入本次提交）；其余一律停下。
        if item.get("status") != "completed":
            raise ValueError("unknown_session_item")
    return target


def _overview_snapshot(ctx: ApiContext, session_root: str) -> tuple[dict, dict]:
    """读确认快照（overview），并检查三个计数达标（导入流程第 6 步）。

    返回 (overview, 目标条目)。active 不是 0、或选中的不是恰好 1 条
    新来源，都说明会话状态和我们预期不符，停下来对账而不是硬提交。
    """

    overview = api_get(ctx, session_root + "/overview")
    items = api_get(ctx, session_root).get("items") or []
    # 快照和条目是两次独立读取，锁定目标所需的 locator/sha 由调用方
    # 语境保证一致（这里只做计数门禁，目标核对在 _commit_body 里做）。
    target = next((i for i in items if isinstance(i, dict)), {})
    counts = overview.get("counts") or {}
    selection = overview.get("selection") or {}
    if (counts.get("active") != 0
            or selection.get("selected") != 1
            or selection.get("newSources") != 1):
        raise ValueError("import_confirmation_snapshot_not_ready")
    return overview, target


def import_package(ctx: ApiContext, package) -> dict:
    """把一个解析包（engines.base.Package）导入知识库。

    返回 {"status": "ok"|"failed", "source_id", "version_id", "code"}。
    - ok：来源已提交并经 GET /sources 核实存在；
    - failed：解析任务失败（code 是后台的 IMPORT_* 错误码）或提交任务
      失败（code 是 native_task_<状态>）；
    - 其余一切"结果不明/需要人核对"的情况直接抛 ValueError（错误代码），
      由 engine 按单条失败处理，绝不带着不确定状态继续跑。
    """

    note_id = package.note_id
    md_path = ctx.import_root / f"{note_id}.md"
    payload = _atomic_write_private(md_path, package.package_md)
    sha256_hex = hashlib.sha256(payload).hexdigest()
    locator = str(md_path)

    # ---- 第 1 步：建会话（journal 先落盘键再发送；durable=True 因为
    # 会话可以用 GET 读回来核对，崩溃恢复后允许用原键重发）。
    session = ctx.journal.run(
        f"{note_id}-session", f"{ctx.base()}/imports",
        {"resourceMode": "balanced"}, ctx.client, durable=True)
    session_id = session.get("sessionId")
    if not session_id:
        raise ValueError("import_session_missing")
    session_root = f"{ctx.base()}/imports/{session_id}"

    # ---- 第 2 步：发起文件发现任务（路径必须在导入根目录内——我们只
    # 把 md 写进 llmwiki-import，天然满足）。需要确认时走 scans/accept。
    discovery = ctx.journal.run(
        f"{note_id}-paths", session_root + "/paths",
        {"sourcePaths": [locator]}, ctx.client, durable=True)
    task_id = discovery.get("id")
    if not task_id:
        raise ValueError("import_discovery_task_missing")
    task = _wait_task(ctx, task_id)
    if task.get("status") != "succeeded":
        return {"status": "failed", "source_id": None, "version_id": None,
                "code": f"native_task_{task.get('status')}"}
    if task.get("requiresConfirmation"):
        # 后台在导入根目录之外/需要用户确认发现结果时会给一个扫描令牌；
        # 我们的路径全在导入根目录内，正常走不到这里，走到了就按后台
        # 给的令牌原样确认（文件发现需要确认时的 scans/{task}/accept 分支）。
        ctx.journal.run(
            f"{note_id}-scan-accept", session_root + f"/scans/{task_id}/accept",
            {"token": task.get("scanToken")}, ctx.client, durable=True)

    # ---- 第 3/4 步：等会话条目落定，锁定我们自己这一项。
    items = _wait_session_items(ctx, session_root)
    target = _checked_target(items, locator, sha256_hex)
    if target.get("status") == "failed":
        return {"status": "failed", "source_id": None, "version_id": None,
                "code": _item_failure_code(ctx, target)}
    if target.get("status") not in _ITEM_READY:
        raise ValueError("import_requires_review")

    # ---- 第 5 步：等这一项的解析任务真正结束，再重读一次
    #（预览发布早于解析任务最后一次状态落盘，实际遇到过）。
    parse_task_id = target.get("taskId")
    if not parse_task_id:
        raise ValueError("import_parse_task_missing")
    task = _wait_task(ctx, parse_task_id)
    if task.get("status") != "succeeded":
        error = task.get("error") or {}
        code = error.get("code") if isinstance(error, dict) else None
        return {"status": "failed", "source_id": None, "version_id": None,
                "code": str(code or "IMPORT_V2_QUALITY_FAILED")}
    items = _wait_session_items(ctx, session_root)
    target = _checked_target(items, locator, sha256_hex)
    if target.get("status") == "failed":
        return {"status": "failed", "source_id": None, "version_id": None,
                "code": _item_failure_code(ctx, target)}
    if target.get("status") != "preview_ready":
        raise ValueError("import_requires_review")

    # ---- 第 6/7 步：取确认快照并提交；409/400 换键重发。
    committed = _commit_with_refresh(ctx, note_id, session_root,
                                     locator, sha256_hex)
    commit_task = committed.get("id")
    if not commit_task:
        raise ValueError("import_commit_task_missing")
    task = _wait_task(ctx, commit_task)
    if task.get("status") != "succeeded":
        return {"status": "failed", "source_id": None, "version_id": None,
                "code": f"native_task_{task.get('status')}"}

    # ---- 核实：条目完成，来源真的出现在 /sources 里。
    items = api_get(ctx, session_root).get("items") or []
    target = _checked_target(items, locator, sha256_hex)
    if target.get("status") != "completed":
        raise ValueError("native_source_commit_not_verified")
    source_id = target.get("sourceId")
    version_id = target.get("versionId") or target.get("currentVersionId")
    if not source_id or not version_id:
        raise ValueError("native_source_commit_not_verified")
    if not _source_exists(ctx, source_id, version_id):
        raise ValueError("native_source_commit_not_verified")
    return {"status": "ok", "source_id": source_id,
            "version_id": version_id, "code": None}


def _wait_session_items(ctx: ApiContext, session_root: str) -> list:
    """轮询会话直到条目非空且全部落定（preview_ready / needs_merge /
    failed / completed 之一），返回条目列表。"""

    deadline = ctx.clock() + SESSION_TIMEOUT_S
    while True:
        items = api_get(ctx, session_root).get("items") or []
        if items and all(i.get("status") in _ITEM_SETTLED for i in items):
            return items
        if ctx.clock() > deadline:
            raise ValueError("native_parse_still_running")
        ctx.sleep(POLL_INTERVAL_S)


def _item_failure_code(ctx: ApiContext, target: dict) -> str:
    """条目失败时取后台的错误码（IMPORT_*_QUALITY_FAILED 等）。"""

    code = target.get("errorCode") or target.get("code")
    if code:
        return str(code)
    task_id = target.get("taskId")
    if task_id:
        task = api_get(ctx, f"{ctx.base()}/tasks/{task_id}")
        error = task.get("error") or {}
        if isinstance(error, dict) and error.get("code"):
            return str(error["code"])
    return "IMPORT_V2_QUALITY_FAILED"


def _commit_with_refresh(ctx: ApiContext, note_id: str, session_root: str,
                         locator: str, sha256_hex: str) -> dict:
    """提交导入会话，处理 409 选择过期 / 400 git 失败。

    两种拒绝都发生在后台创建入库任务之前（实际遇到过，也核对过后端
    源码）：什么都没写、没有任务。处理办法：
    1. 重读会话，核对原项的 SHA 和路径没变、仍是选中的新来源、没有
       受限内容（任何一条不满足都停下来，不盲重发）；
    2. 把原请求从 journal 丢弃（discard_operation，写进历史并计数）；
    3. 取新的确认快照（selectionRevision / confirmationDigest 变了），
       换新键重发。每种原因最多 2 次（journal 的计数兜底）。
    """

    body = _commit_body(ctx, session_root, locator, sha256_hex)
    name = f"{note_id}-commit"
    while True:
        try:
            return ctx.journal.run(name, session_root + "/commit", body,
                                   ctx.client, durable=True)
        except ApiError as error:
            reason = {
                (409, "IMPORT_V2_SELECTION_STALE"): _REASON_SELECTION_STALE,
                (400, "GIT_COMMAND_FAILED"): _REASON_GIT_COMMAND_FAILED,
            }.get((error.status, error.code))
            if reason is None:
                raise  # 其他错误码：结果可能不明，停下对账
            _verify_target_for_refresh(ctx, session_root, locator, sha256_hex)
            ctx.journal.discard_operation(name, reason)
            body = _commit_body(ctx, session_root, locator, sha256_hex)


def _commit_body(ctx: ApiContext, session_root: str,
                 locator: str, sha256_hex: str) -> dict:
    """构造提交请求体：先核对目标项仍符合预期，再取新的确认快照。"""

    _verify_target_for_refresh(ctx, session_root, locator, sha256_hex)
    overview, _ = _overview_snapshot(ctx, session_root)
    return {
        "expectedSelectionRevision": overview.get("selectionRevision"),
        "expectedConfirmationDigest": overview.get("confirmationDigest"),
        "decisions": [],
        "acknowledgeRestrictedContent": False,
    }


def _verify_target_for_refresh(ctx: ApiContext, session_root: str,
                               locator: str, sha256_hex: str) -> dict:
    """重发前核对原项：输入没变、仍是选中的 preview_ready 新来源、
    没有受限内容。"""

    target = _checked_target(
        api_get(ctx, session_root).get("items") or [], locator, sha256_hex)
    original = target.get("input") or {}
    identity = original.get("sourceIdentity") or {}
    if (identity.get("sha256") != sha256_hex
            or original.get("normalizedLocator") != locator):
        raise ValueError("confirmation_original_input_changed")
    if (target.get("status") != "preview_ready"
            or not target.get("selected")
            or target.get("restrictedContent")
            or (target.get("preview") or {}).get("resolution", {}).get("kind") != "new_source"):
        raise ValueError("confirmation_selection_requires_review")
    return target


def _source_exists(ctx: ApiContext, source_id: str, version_id: str) -> bool:
    """分页查 GET /sources，确认来源和版本仍然存在（engine.source_exists
    也复用这个函数）。"""

    offset = 0
    while True:
        data = api_get(ctx, f"{ctx.base()}/sources?offset={offset}")
        for row in data.get("sources") or []:
            if row.get("sourceId") == source_id:
                return row.get("versionId") == version_id
        offset = data.get("nextOffset")
        if offset is None:
            return False
