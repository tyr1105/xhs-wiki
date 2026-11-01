# -*- coding: utf-8 -*-
"""engines/llmwiki/engine.py —— LlmWikiEngine：实现 engines.base 的 Engine 协议。

这个文件做什么：把地基（backend / client / journal / project /
agents）和导入（importer）、整理（compiler + review）拼成一个
满足 Engine 协议的引擎：

- health()：二进制存在、没有桌面版占着单实例锁、后台能启动或已在
  运行、项目能打开（知识库不存在时先 scaffold）；
- ingest(packages)：逐条导入，engine_ref = {"source_id","version_id"}；
- distill(note_ids)：分组整理，per_note 给出 ok / failed / deferred；
- source_exists：用 GET /sources 核对来源和版本仍然存在；
- skill_context：给 Claude Skill 的路径说明；
- close()：runner 一次运行结束时调用，安全停掉后台。

后台的启动和停止由本引擎管理：第一次用到时启动（或用注入的 client），
close() 时若是我们启动的就停掉。来源引用（note_id → source/version）
持久保存在 APP_HOME/llmwiki-sources.json（0600），ingest 写、distill 和
source_exists 读。

在整体里的位置：runner.default_deps().make_engine 在 engine == "llmwiki"
时创建本类。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from ..base import DistillResult, IngestResult, Package
from . import agents as agents_module
from .backend import Backend, EngineUnavailable
from .client import ApiError
from .compiler import Compiler, GroupMember, ReviewRequired  # noqa: F401（runner 要用）
from .importer import ApiContext, import_package  # noqa: F401（re-export）
from .journal import Journal
from .project import scaffold

# 无窗口后台的默认端口。真实环境的端口冲突极少见，先取一个固定值，
# 需要时再做成设置。
DEFAULT_API_PORT = 18766

# 分组上限：最多 5 条来源、合计 80KB。
GROUP_MAX_SOURCES = 5
GROUP_MAX_BYTES = 80_000

# ingest 阶段遇到缓存满（429 API_RETRY_CACHE_FULL）的重启上限。
CACHE_RESTARTS_MAX = 2


def _atomic_write_json(path: Path, data) -> None:
    """把 data 原子写成 0600 的 JSON 文件（先写临时文件再 os.replace）。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class LlmWikiEngine:
    """LLM Wiki 引擎（进阶引擎，docs/architecture.md §6.2）。"""

    name = "llmwiki"

    def __init__(self, project_root, app_home, settings, *,
                 backend=None, client=None, sleep=None, clock=None,
                 port=None) -> None:
        self.project_root = Path(project_root).expanduser()
        self.app_home = Path(app_home).expanduser()
        self.app_home.mkdir(parents=True, exist_ok=True)
        os.chmod(self.app_home, 0o700)
        self.settings = settings or {}
        self.import_root = self.app_home / "llmwiki-import"
        self.import_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.import_root, 0o700)
        self._backend = backend
        self._client = client
        self._own_backend = backend is not None
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._port = int(port or DEFAULT_API_PORT)
        self._journal: Journal | None = None
        self._project_id: str | None = None
        # 缓存满（429 API_RETRY_CACHE_FULL）时 ingest 阶段的重启次数。
        self._ingest_restarts = 0

    # ---- 连接管理 -----------------------------------------------------------

    def _ensure(self) -> ApiContext:
        """保证：后台在运行（或用注入的 client）、项目已打开。

        - 注入了 client 且没有 backend：直接用外部连接（测试 / 后台由
          别处管理），不负责起停；
        - 否则按需创建 Backend（设置项 llmwiki_binary），没在运行就启动；
        - 项目打不开句柄就 scaffold（不存在时）+ open_project。
        """

        if self._client is None:
            if self._backend is None:
                binary = self.settings.get("llmwiki_binary")
                if not binary:
                    raise EngineUnavailable("llmwiki_binary_missing")
                if not Path(binary).is_file():
                    raise EngineUnavailable("llmwiki_binary_missing")
                self._backend = Backend(
                    Path(binary), self.project_root, self.import_root,
                    self._port, agent_bin_dir=self._agent_bin_dir(),
                    sleep=self._sleep, clock=self._clock)
                self._own_backend = True
            if not self._backend.is_running():
                self._backend.start()  # 桌面版占锁 → EngineUnavailable
            self._client = self._backend.client()
        if self._project_id is None:
            scaffold(self.project_root)  # 不存在时搭脚手架；已存在则幂等
            self._project_id = self._open()
        self._write_model_config()
        return ApiContext(
            client=self._client, journal=self._journal_or_new(),
            project_root=self.project_root, import_root=self.import_root,
            backend=self._backend if self._own_backend else None,
            project_id=self._project_id,
            sleep=self._sleep, clock=self._clock)

    def _open(self) -> str:
        from .project import open_project  # 延迟 import，减少加载面

        return open_project(self._client, self.project_root)

    def _journal_or_new(self) -> Journal:
        """请求日志：APP_HOME/llmwiki-journal.sqlite3（绝不进知识库）。"""

        if self._journal is None:
            self._journal = Journal(self.app_home / "llmwiki-journal.sqlite3")
        return self._journal

    def _agent_bin_dir(self) -> Path:
        """包装脚本目录：APP_HOME/llmwiki-bin（与 agents.py 用的位置一致）。"""

        directory = self.app_home / "llmwiki-bin"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _write_model_config(self) -> None:
        """把设置里选的整理模型写进 model.conf（后台每次调用现读）。"""

        llm = self.settings.get("llm") or {}
        model = llm.get("model") or ""
        try:
            agents_module.write_model_config(model, self._agent_bin_dir())
        except OSError:
            pass  # 写不进配置不该让引擎瘫痪；后台会用默认模型

    def close(self) -> None:
        """一次运行结束：安全停掉我们启动的后台，关掉请求日志。"""

        if self._own_backend and self._backend is not None:
            try:
                if self._backend.is_running():
                    self._backend.stop()
            except (ValueError, OSError):
                pass  # 停不掉（还有任务在跑等）留给下一次运行收尾
        if self._journal is not None:
            try:
                self._journal.close()
            except Exception:
                pass
            self._journal = None

    # ---- Engine 协议 ---------------------------------------------------------

    def health(self) -> tuple[bool, str]:
        """是否可用及原因（不调用模型、不花钱）。"""

        try:
            self._ensure()
            return True, "ok"
        except EngineUnavailable as error:
            return False, error.code
        except ApiError as error:
            return False, f"llmwiki_api_{error.code}"
        except (ValueError, OSError) as error:
            return False, str(error)

    def ingest(self, packages: list[Package]) -> list[IngestResult]:
        """逐条导入解析包；engine_ref 是 {"source_id","version_id"}。"""

        ctx = self._ensure()
        refs = self._load_refs()
        results = []
        for package in packages:
            outcome = None
            error = None
            try:
                outcome = self._import_with_cache_restart(ctx, package)
            except ApiError as err:
                error = f"llmwiki_api_{err.code}"
            except (ValueError, KeyError, OSError) as err:
                error = str(err)
            if outcome is None and error is None:
                error = "ingest_failed"
            if outcome is not None and outcome.get("status") == "ok":
                refs[package.note_id] = {
                    "source_id": outcome["source_id"],
                    "version_id": outcome["version_id"],
                }
                _atomic_write_json(self._refs_path(), refs)
                results.append(IngestResult(
                    note_id=package.note_id, status="ok",
                    engine_ref={"source_id": outcome["source_id"],
                                "version_id": outcome["version_id"]}))
            else:
                code = (outcome or {}).get("code") if outcome else None
                results.append(IngestResult(
                    note_id=package.note_id, status="failed",
                    error=str(code or error or "ingest_failed")))
        return results

    def _import_with_cache_restart(self, ctx: ApiContext, package: Package):
        """导入一条；遇到缓存满时重启后台，再用原键重发。"""

        while True:
            try:
                return import_package(ctx, package)
            except ApiError as error:
                if (error.code == "API_RETRY_CACHE_FULL"
                        and self._ingest_restarts < CACHE_RESTARTS_MAX
                        and self._own_backend and self._backend is not None):
                    self._ingest_restarts += 1
                    self._backend.restart_for_retry_cache()
                    self._project_id = None
                    ctx = self._ensure()
                    continue
                raise

    def distill(self, note_ids: list[str]) -> DistillResult:
        """把新入库的收藏按组整理成主题页面。"""

        ctx = self._ensure()
        refs = self._load_refs()
        # provider → routeSelection.agent（claude_code→claude）。
        provider = (self.settings.get("llm") or {}).get("provider")
        agent = {"claude_code": "claude", "codex": "codex"}.get(
            str(provider), "codex")
        compiler = Compiler(ctx, agent)

        pending: list[GroupMember] = []
        per_note: dict[str, dict] = {}
        for note_id in note_ids:
            ref = refs.get(note_id)
            if not ref:
                per_note[note_id] = {"status": "failed",
                                     "code": "source_ref_missing"}
                continue
            size = 0
            md = self.import_root / f"{note_id}.md"
            if md.is_file():
                size = md.stat().st_size
            pending.append(GroupMember(
                note_id=note_id, source_id=ref["source_id"],
                version_id=ref["version_id"], source_bytes=size))

        # 分组：最多 5 条、合计 80KB；凑不下一整组时剩下的自成一组的
        # 尾巴也照发（单条来源整理是后台支持的正常路径）。
        groups: list[list[GroupMember]] = []
        current: list[GroupMember] = []
        used = 0
        for member in pending:
            if current and (len(current) >= GROUP_MAX_SOURCES
                            or used + member.source_bytes > GROUP_MAX_BYTES):
                groups.append(current)
                current, used = [], 0
            current.append(member)
            used += member.source_bytes
        if current:
            groups.append(current)

        round_id = uuid.uuid4().hex[:12]  # 本轮唯一；请求名/键都带它
        for index, group in enumerate(groups, 1):
            try:
                outcomes = compiler.compile(group, round_id, index)
            except (ApiError, ValueError, OSError) as error:
                code = (f"llmwiki_api_{error.code}"
                        if isinstance(error, ApiError) else str(error))
                outcomes = {m.note_id: {"status": "failed", "code": code}
                            for m in group}
            per_note.update(outcomes)

        ok = sum(1 for v in per_note.values() if v.get("status") == "ok")
        status = "ok" if ok == len(note_ids) else "partial"
        result = DistillResult(status=status)
        result.assigned = ok
        result.pages_rewritten = len(groups)
        result.per_note = per_note
        for note_id, outcome in per_note.items():
            if outcome.get("status") == "failed":
                result.errors.append({"code": outcome.get("code"),
                                      "detail": note_id})
        return result

    def source_exists(self, note_id: str, engine_ref: dict) -> bool:
        """这条笔记的来源和版本是否仍然在知识库里（清理前的核对）。"""

        ctx = self._ensure()
        source_id = (engine_ref or {}).get("source_id")
        version_id = (engine_ref or {}).get("version_id")
        if not source_id or not version_id:
            return False
        from .importer import _source_exists  # 复用分页查询

        return _source_exists(ctx, source_id, version_id)

    def skill_context(self) -> dict:
        """Claude Skill 需要的路径和说明（以真实目录结构为准）。"""

        return {
            "kb_root": str(self.project_root),
            "engine": "llmwiki",
            "index": "wiki/index.md",
            "topics_dir": "wiki",
            "sources_dir": "raw/sources",
        }

    # ---- 来源引用的持久化 ------------------------------------------------------

    def _refs_path(self) -> Path:
        return self.app_home / "llmwiki-sources.json"

    def _load_refs(self) -> dict:
        try:
            data = json.loads(self._refs_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}
