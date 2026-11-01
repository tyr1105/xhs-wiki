# -*- coding: utf-8 -*-
"""engines/llmwiki/compiler.py —— 分组整理（Update Wiki）+ 轻量检查 + 核验。

这个文件做什么：实现分组整理流程——
- 分组：最多 5 条来源、合计 80KB（分组在 engine.distill 里做，这里消费
  一个已经定好的组）；
- 发起：POST /workflows/update-wiki，requestId 从"轮次 + 组号"确定性
  生成（uuid5），连同组成员和来源版本一起写进 journal 的 update 操作
  （请求体含 selection.sourceVersions，发送前先落盘——这就是"组成员
  和来源版本在发送前存进 journal"）；
- 轮询：每 3 秒一次，每次核对 taskId 和 scope；按 displayStatus 分流；
- 读差异：逐个文件、按游标翻页；GIT_COMMAND_TIMEOUT 只对这个只读请求
  换新键等 30 秒重读；请求名带 taskId，避免和别的任务撞名；
- 审阅：交给 review.light_review——confirm（重读核对 pendingAction.id
  没变再确认）/ reject（cancel 驳回，前后核对文件没变）/ needs_user
  （抛 ReviewRequired，由 runner 记 llmwiki_review_required 事项）；
- 核验：completed 不等于成功——要看 result.finalCommit 和每条来源的
  sourceOutcomes，deferred 算暂缓；
- 熔断：额度用完 / 未登录这类秒败抛 LLMError；
- 缓存满：429 API_RETRY_CACHE_FULL 时 backend.restart_for_retry_cache()
  重启、重新打开项目、用原键重发，每轮最多 2 次。

简化之处：attempt 预算由 runner 的台账（每阶段 3 次）承担，一次
distill 调用里每组只发起一次任务。

在整体里的位置：engine.distill 按组调用 Compiler.compile。
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass

from xhs_wiki.llm.base import LLMError

from .client import ApiError
from .importer import ApiContext, api_get
from .review import light_review

# 轮询整理任务：间隔 3 秒，总时长上限 1000 秒。
POLL_INTERVAL_S = 3
POLL_TIMEOUT_S = 1000

# 读差异：单页上限 64KB（后台最大 240KB）。
DIFF_LIMIT_BYTES = 65536
# GIT_COMMAND_TIMEOUT 后等 30 秒再重读，最多换键重读 2 次
#（初次 + 2 次重读 = 3 次尝试；上限同时受 journal 每原因 2 次归档约束）。
GIT_TIMEOUT_WAIT_S = 30
GIT_TIMEOUT_MAX_REREADS = 2
_GIT_TIMEOUT_REASON = "read_only_diff_git_timeout_retry"

# 缓存满（429 API_RETRY_CACHE_FULL）每轮最多重启 2 次。
CACHE_FULL_RESTARTS = 2

# finalCommit 必须是 40 位十六进制的 git 提交号。
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

# AGENT_EXIT_FAILED 的失败类别 → 熔断用的 LLMError 代码。
# 额度用完 / 未登录这类秒败不是笔记的问题，退还次数并停下。
_QUICK_BREAK_CODES = {
    "quota_limit": "quota_or_rate_limit",
    "auth_required": "not_logged_in",
    "authentication_or_access": "not_logged_in",
}


class ReviewRequired(Exception):
    """整理候选需要用户在桌面版或控制台里确认。

    message 是原因代码（如 existing_pages_changed_limit），runner 会把它
    记成 llmwiki_review_required 事项后暂停。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class GroupMember:
    """组里的一条来源：note_id 对应的 source/version 引用和大小（字节）。"""

    note_id: str
    source_id: str
    version_id: str
    source_bytes: int = 0


class Compiler:
    """整理一组来源。engine.distill 负责分组，这里执行单个组。"""

    def __init__(self, ctx: ApiContext, agent: str) -> None:
        self.ctx = ctx
        self.agent = agent  # routeSelection.agent：claude 或 codex

    # ---- 对外入口 ---------------------------------------------------------

    def compile(self, members: list[GroupMember], round_id: str, index: int) -> dict:
        """整理一组来源，返回 {note_id: {"status","code"}}。

        - ok：这条来源被整理进知识库（integrated / already_covered）；
        - deferred：模型主动暂缓（不算已消费），code 是后台给的原因；
        - failed：任务失败或核验不通过，code 是错误代码。
        抛 ReviewRequired 表示需要用户；抛 LLMError 表示额度/登录熔断。
        """

        restarts = 0
        while True:
            try:
                return self._compile_once(members, round_id, index)
            except ApiError as error:
                if (error.code == "API_RETRY_CACHE_FULL"
                        and restarts < CACHE_FULL_RESTARTS
                        and self.ctx.backend is not None):
                    # 缓存满：重启清空、重新打开项目、用原键重发。
                    restarts += 1
                    self.ctx.backend.restart_for_retry_cache()
                    self.ctx.reopen()
                    continue
                raise

    # ---- 一次完整尝试 ------------------------------------------------------

    def _compile_once(self, members: list[GroupMember],
                      round_id: str, index: int) -> dict:
        group_id = f"{round_id}-g{index:03d}"
        task_id = self._start(group_id, members)
        expected = {(m.source_id, m.version_id) for m in members}
        confirmed = False  # 已经发过 confirm，之后再看到等待确认就继续等
        deadline = self.ctx.clock() + POLL_TIMEOUT_S
        while True:
            run = self._run(task_id, expected)
            status = run.get("displayStatus")
            if status == "waiting_for_confirmation":
                if confirmed:
                    # 确认已发但后台还在等待态：继续轮询等终态。
                    pass
                else:
                    outcome = self._review_and_decide(group_id, task_id, run,
                                                      expected, members)
                    if outcome is not None:
                        return outcome  # reject：整组失败返回
                    confirmed = True
            elif status == "completed":
                return self._verify(run, members, task_id)
            elif status in ("failed", "cancelled", "interrupted"):
                self._quick_break(run)
                code = self._error_code(run)
                return {m.note_id: {"status": "failed", "code": code}
                        for m in members}
            if self.ctx.clock() > deadline:
                raise ValueError("compile_still_running")
            self.ctx.sleep(POLL_INTERVAL_S)

    # ---- 发起 --------------------------------------------------------------

    def _start(self, group_id: str, members: list[GroupMember]) -> str:
        """发起（或崩溃恢复后复用）一个 Update Wiki 任务。

        requestId 由"轮次 + 组号"确定性生成：journal 在发送前就把
        请求体（含 requestId 和全部 sourceVersions）的签名落盘，崩溃
        恢复后用同名操作重发会拿到同一个 requestId 和同一个任务。
        """

        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"xhs-wiki/{group_id}"))
        body = {
            "requestId": request_id,
            "mode": "changed_sources",
            "selection": {
                "kind": "selected",
                "sourceVersions": [
                    {"sourceId": m.source_id, "versionId": m.version_id}
                    for m in members
                ],
            },
            "routeSelection": {"kind": "agent", "agent": self.agent},
            "acknowledgeRemoteProvider": False,
        }
        created = self.ctx.journal.run(
            f"{group_id}-update", f"{self.ctx.base()}/workflows/update-wiki",
            body, self.ctx.client, durable=True)
        run = (created or {}).get("run") or {}
        task_id = run.get("taskId")
        if not task_id:
            raise ValueError("compile_task_missing")
        return task_id

    # ---- 轮询辅助 -----------------------------------------------------------

    def _run(self, task_id: str, expected: set) -> dict:
        """读一次工作流状态，并核对任务号和范围（每次轮询都核对）。"""

        run = api_get(self.ctx, f"{self.ctx.base()}/workflows/{task_id}")
        if run.get("taskId") != task_id:
            raise ValueError("compile_task_id_mismatch")
        scope = run.get("scope") or {}
        versions = scope.get("sourceVersions") or []
        actual = {(v.get("sourceId"), v.get("versionId")) for v in versions}
        if (scope.get("kind") != "update_wiki"
                or scope.get("mode") != "changed_sources"
                or actual != expected):
            raise ValueError("compile_scope_changed")
        return run

    @staticmethod
    def _error_code(run: dict) -> str:
        """从工作流状态里取失败错误码（拿不到就用状态本身）。"""

        error = run.get("error") or {}
        if isinstance(error, dict) and error.get("code"):
            code = str(error["code"])
            if re.fullmatch(r"[A-Z0-9_]{1,100}", code):
                return code
        return "COMPILE_FAILED"

    def _quick_break(self, run: dict) -> None:
        """额度用完 / 未登录的秒败：抛 LLMError 让 runner 熔断（E10）。"""

        error = run.get("error") or {}
        if not isinstance(error, dict) or error.get("code") != "AGENT_EXIT_FAILED":
            return
        details = error.get("details") or {}
        category = details.get("agentFailureCategory") if isinstance(details, dict) else None
        code = _QUICK_BREAK_CODES.get(category)
        if code:
            raise LLMError(code)

    # ---- 审阅 --------------------------------------------------------------

    def _review_and_decide(self, group_id: str, task_id: str, run: dict,
                           expected: set, members: list[GroupMember]):
        """读差异 → 轻量检查 → confirm / cancel。

        返回 None 表示已确认（继续轮询等终态）；返回 dict 表示整组驳回
        （失败结果）；抛 ReviewRequired 交给用户。
        """

        pending = run.get("pendingAction")
        review = run.get("decisionReview")
        if not pending or not review:
            raise ValueError("native_review_details_missing")
        file_diffs = []
        for file in review.get("fileDiffs") or []:
            diff = self._read_diff(group_id, task_id, pending["id"], file)
            path = file.get("path")
            file_diffs.append({
                "file_id": file.get("fileId"),
                "path": path,
                "diff": diff,
                # "改动前是否存在"由磁盘事实决定（新建页不算）。
                "existed_before": (self.ctx.project_root / str(path)).exists()
                if path else False,
            })
        # 已登记的来源页 = 磁盘上 wiki/sources/ 和 raw/sources/ 下的全部
        # .md（本组的来源也在其中——导入时已经落盘）。
        known = self._known_source_paths()
        merged = {**pending,
                  "counts": review.get("counts") or {},
                  "userEditsDetected": bool(review.get("userEditsDetected"))}
        decision = light_review(merged, file_diffs, known)
        if decision["decision"] == "confirm":
            self._confirm(group_id, task_id, run, expected, pending["id"])
            return None
        if decision["decision"] == "reject":
            return self._reject(group_id, task_id, run, members,
                                decision["reason"])
        raise ReviewRequired(decision["reason"])

    def _confirm(self, group_id: str, task_id: str, run: dict,
                 expected: set, action_id: str) -> None:
        """确认前重读一次，核对 pendingAction.id 没变，再 confirm。"""

        fresh = self._run(task_id, expected)
        pending = fresh.get("pendingAction")
        if not pending or pending.get("id") != action_id:
            raise ValueError("native_pending_action_changed")
        self.ctx.journal.run(
            f"{group_id}-confirm-{action_id}",
            f"{self.ctx.base()}/workflows/{task_id}/confirm",
            {"actionId": action_id}, self.ctx.client, durable=True)

    def _reject(self, group_id: str, task_id: str, run: dict,
                members: list[GroupMember], reason: str) -> dict:
        """驳回丢引用/未知引用的候选。

        用后台正式的 cancel 接口丢弃候选；取消前后逐个核对候选涉及的
        文件内容没变（真的什么都没写），然后整组记一次失败。
        """

        pending = run.get("pendingAction") or {}
        paths = [str(p) for p in pending.get("affectedPaths") or []]
        before = self._digests(paths)
        self.ctx.journal.run(
            f"{group_id}-reject-{task_id}",
            f"{self.ctx.base()}/workflows/{task_id}/cancel",
            {}, self.ctx.client, durable=True)
        fresh = api_get(self.ctx, f"{self.ctx.base()}/workflows/{task_id}")
        if (fresh.get("taskId") != task_id
                or fresh.get("displayStatus") != "cancelled"):
            raise ValueError("review_reject_not_cancelled")
        if self._digests(paths) != before:
            raise ValueError("review_reject_changed_files")
        return {m.note_id: {"status": "failed",
                            "code": f"review_{reason}"} for m in members}

    def _digests(self, paths: list[str]) -> dict:
        """候选涉及文件的当前内容指纹（不存在的文件记为 None）。"""

        result = {}
        for path in paths:
            target = self.ctx.project_root / path
            if target.is_file():
                result[path] = hashlib.sha256(
                    target.read_bytes()).hexdigest()
            else:
                result[path] = None
        return result

    def _read_diff(self, group_id: str, task_id: str,
                   pending_id: str, file: dict) -> str:
        """逐段读一个文件的候选差异，拼成完整差异文本。

        请求名带 taskId：不同任务的差异请求绝不撞名。git 超时
        只发生在这个只读请求上：等 30 秒、换新键重读，最多
        重读 2 次；再不行就停下（写请求超时本来就直接停）。
        """

        file_id = file.get("fileId")
        pieces: list[str] = []
        cursor = None
        segment = 0
        while True:
            name = f"{group_id}-{task_id}-diff-{file_id}-{segment}"
            path = f"{self.ctx.base()}/workflows/{task_id}/diff"
            body = {"pendingActionId": pending_id, "fileId": file_id,
                    "limitBytes": DIFF_LIMIT_BYTES}
            if cursor is not None:
                body["cursor"] = cursor
            chunk = self._run_diff_read(name, path, body)
            pieces.append(chunk.get("diff") or "")
            cursor = chunk.get("nextCursor")
            segment += 1
            if cursor is None:
                return "".join(pieces)

    def _run_diff_read(self, name: str, path: str, body: dict) -> dict:
        """发一次读差异请求，处理 GIT_COMMAND_TIMEOUT。"""

        rereads = 0
        while True:
            try:
                return self.ctx.journal.run(name, path, body,
                                            self.ctx.client, durable=True)
            except ApiError as error:
                if error.code != "GIT_COMMAND_TIMEOUT":
                    raise
                if rereads >= GIT_TIMEOUT_MAX_REREADS:
                    raise ValueError("diff_git_timeout") from None
                rereads += 1
                # 后台按请求键缓存结果（包括失败），原键重发只会拿回同一
                # 个超时；只有这个只读请求允许换键重读。
                self.ctx.journal.archive_and_rekey(name, _GIT_TIMEOUT_REASON)
                self.ctx.sleep(GIT_TIMEOUT_WAIT_S)

    def _known_source_paths(self) -> set[str]:
        """磁盘上全部已登记来源页的相对路径（posix 写法）。"""

        known: set[str] = set()
        for folder in ("wiki/sources", "raw/sources"):
            root = self.ctx.project_root / folder
            if not root.is_dir():
                continue
            for path in root.rglob("*.md"):
                known.add(path.relative_to(self.ctx.project_root).as_posix())
        return known

    # ---- 核验 --------------------------------------------------------------

    def _verify(self, run: dict, members: list[GroupMember],
                task_id: str) -> dict:
        """核验整理结果（completed 不等于成功）。

        必须有 40 位十六进制的 finalCommit，并且每条来源都在
        sourceOutcomes 里有明确的消费结果；deferred 算暂缓。
        """

        result = run.get("result") or {}
        if not _COMMIT_RE.fullmatch(str(result.get("finalCommit") or "")):
            return {m.note_id: {"status": "failed",
                                "code": "no_final_commit"} for m in members}
        outcomes = {}
        for outcome in result.get("sourceOutcomes") or []:
            version = (outcome or {}).get("sourceVersion") or {}
            outcomes[version.get("sourceId")] = outcome or {}
        per_note = {}
        for member in members:
            outcome = outcomes.get(member.source_id)
            if not outcome:
                per_note[member.note_id] = {
                    "status": "failed", "code": "consumption_not_verified"}
                continue
            status = outcome.get("status")
            if status == "deferred":
                per_note[member.note_id] = {
                    "status": "deferred",
                    "code": str(outcome.get("reason") or "deferred")}
            elif status in ("integrated", "already_covered"):
                per_note[member.note_id] = {"status": "ok", "code": None}
            else:
                per_note[member.note_id] = {
                    "status": "failed", "code": "consumption_not_verified"}
        return per_note
