# -*- coding: utf-8 -*-
"""engine.py —— LLM Wiki 引擎本体：实现 engines/base.py 的 Engine 协议。

这个文件做什么：LlmWikiAppEngine 把 xhs-wiki 和 nashsu/llm_wiki 桌面
应用衔接起来——
- health()：不花钱的运行前检查（应用装没装、版本够不够、项目结构
  对不对、整理用的提供方配没配好）；
- prepare()：运行前准备（应用没运行就后台启动、确认应用打开的就是
  这个知识库项目），runner 在 ingest 前调用；
- ingest()：把解析包写成项目里的来源 Markdown 文件，剩下的整理由
  应用自动完成；
- distill()：轮询应用的两个记录文件，逐条判定整理完成 / 失败 / 未完成；
- source_exists()：清理原件前核对"这条来源确实整理完成且页面还在"；
- skill_context()：查询 Skill 需要的目录说明（nashsu 布局）。

在整体里的位置：docs/architecture.md §6 的 Engine 实现（name="llmwiki"），
由 engines/__init__.py 的 make_engine 构造，runner / cleanup /
skill 只认 Engine 协议的形状。

和轻量引擎最大的不同：本引擎**不调用任何模型**。整理是 LLM Wiki
应用做的，xhs-wiki 只写来源文件、读记录文件；所以 distill 的"等待"
是轮询文件，而不是模型调用。

超时口径（可注入便于测试）：默认总时长 = 条数 × 3 分钟，至少 10
分钟、最多 60 分钟；轮询间隔默认 5 秒。到时仍未完成的笔记不写进
per_note（保持 ingested），下一次运行接着核对，绝不判失败。
"""

from __future__ import annotations

import time
from pathlib import Path

from ..base import DistillResult, IngestResult, Package
from . import app as app_module
from . import project


class EngineNeedsUser(Exception):
    """运行前准备发现需要用户处理的问题（如应用打开的不是这个知识库）。

    runner 捕获后走 needs_user 机制登记事项并结束本轮。code 是
    NEEDS_USER_TITLES 里的代码；detail 是给用户看的中文说明——
    绝不含路径之外的敏感内容，更不含任何密钥。
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# 队列失败原因 → 错误代码的归类规则（顺序即优先级，先匹配先得）。
# 文本全部小写比较；LLM Wiki 的 error 是接口返回的原文，可能含 JSON。
def classify_queue_error(text: str) -> str:
    """把队列里的失败原文归类成短错误代码。

    对照表（大白话说明在控制台 / N02）：
    - 含 temperature（模型拒绝旧式参数，Claude 5 系列常见）→
      llmwiki_model_param_rejected；
    - 含 401 / authentication / invalid x-api-key（密钥无效）→
      llmwiki_auth_failed；
    - 含 credit / balance / billing（余额或计费问题）→ llmwiki_quota；
    - 含 model 且含 not_found（模型名错误）→ llmwiki_model_not_found；
    - 其他一律 llmwiki_ingest_failed。
    """

    lowered = str(text or "").lower()
    if "temperature" in lowered:
        return "llmwiki_model_param_rejected"
    if "401" in lowered or "authentication" in lowered or "invalid x-api-key" in lowered:
        return "llmwiki_auth_failed"
    if "credit" in lowered or "balance" in lowered or "billing" in lowered:
        return "llmwiki_quota"
    if "model" in lowered and "not_found" in lowered:
        return "llmwiki_model_not_found"
    return "llmwiki_ingest_failed"


# 默认的"每条整理等待时间"等参数（秒）。
PER_NOTE_WAIT_S = 3 * 60      # 每条 3 分钟
MIN_TOTAL_WAIT_S = 10 * 60    # 至少 10 分钟
MAX_TOTAL_WAIT_S = 60 * 60    # 最多 60 分钟
DEFAULT_POLL_INTERVAL_S = 5.0  # 轮询间隔
DEFAULT_START_WAIT_S = 60.0    # prepare 启动应用后等进程出现的上限
DEFAULT_SETTLE_WAIT_S = 10.0   # 进程出现后再等应用加载项目的时间


class LlmWikiAppEngine:
    """对接 nashsu/llm_wiki 桌面应用的引擎（name="llmwiki"）。

    构造参数：
    - kb_root：知识库项目目录（LLM Wiki 里的一个项目）；
    - app_home：本工具的数据目录（保留参数以兼容工厂签名，本引擎
      不在里面存任何东西——所有状态都在知识库项目和应用的记录文件里）；
    - settings：完整设置（本引擎不使用 xhs-wiki 自己的模型设置，
      整理用的模型在 LLM Wiki 的设置里选）；
    - 其余关键字参数全部是可注入的外部依赖（应用目录、状态文件路径、
      进程检测、启动、睡眠、时钟、轮询间隔），测试全部换成假实现，
      不碰真实的应用和 ~/Library。
    """

    name = "llmwiki"

    def __init__(self, kb_root, app_home=None, settings=None, *,
                 app: app_module.AppController | None = None,
                 sleep=None, monotonic=None,
                 poll_interval: float = DEFAULT_POLL_INTERVAL_S) -> None:
        self.kb_root = Path(kb_root).expanduser()
        self.app = app or app_module.AppController()
        self.sleep = sleep or time.sleep
        self.monotonic = monotonic or time.monotonic
        self.poll_interval = poll_interval

    # ------------------------------------------------------------------
    # health：不花钱的运行前检查
    # ------------------------------------------------------------------

    def health_checks(self) -> list:
        """逐项检查，返回 [(名称, 是否通过, 说明), …]。

        health() 取第一个不满足项；doctor（cli.py）则把整张表逐行
        打印。检查项：应用已安装、版本不低于 0.6.13、知识库目录是
        LLM Wiki 项目、整理用提供方已配置。全部只读文件系统。
        """

        bundle = app_module.find_bundle(self.app.app_dirs)
        checks = []
        checks.append(("llmwiki_app", bundle is not None,
                       str(bundle) if bundle is not None else "llmwiki_app_missing"))
        version = app_module.read_bundle_version(bundle) if bundle else None
        checks.append(("llmwiki_version", app_module.version_ok(version),
                       app_module.version_text(version)))
        layout_ok = project.project_layout_ok(self.kb_root)
        checks.append(("llmwiki_project", layout_ok, "" if layout_ok
                       else "llmwiki_project_invalid"))
        model_ok = self.app.ingest_provider_ok()
        checks.append(("llmwiki_model", model_ok, "" if model_ok
                       else "llmwiki_model_missing"))
        return checks

    def health(self) -> tuple[bool, str]:
        """Engine 协议：引擎是否可用及原因（不启动应用、不花钱）。

        返回第一个不满足项的原因代码（见 health_checks 的顺序）；
        全部通过返回 (True, "ok")。当前打开的项目对不对不在 health 里
        判——应用可能没运行——那部分放到 prepare()。
        """

        mapping = {
            "llmwiki_app": "llmwiki_app_missing",
            "llmwiki_version": "llmwiki_app_too_old",
            "llmwiki_project": "llmwiki_project_invalid",
            "llmwiki_model": "llmwiki_model_missing",
        }
        for name, ok, _detail in self.health_checks():
            if not ok:
                return (False, mapping[name])
        return (True, "ok")

    # ------------------------------------------------------------------
    # prepare：运行前准备（runner 在 ingest 前调用）
    # ------------------------------------------------------------------

    def prepare(self, *, start_wait_s: float = DEFAULT_START_WAIT_S,
                settle_wait_s: float = DEFAULT_SETTLE_WAIT_S) -> None:
        """确保应用在运行、且打开的就是这个知识库项目。

        - 应用没运行：用 ``open -g -j -b com.llmwiki.app`` 后台启动
          （不弹前台），最多等 60 秒到进程出现，再等 10 秒让它加载
          项目；进程一直没出现按 llmwiki_app_missing 交给用户处理。
        - 读 app-state.json 的 lastProject.path，规范化（resolve、去尾
          斜杠——Path 本身不带尾斜杠）后与知识库目录比较；不一致抛
          EngineNeedsUser("llmwiki_wrong_project")，说明只写应用当前
          打开的项目名，不写完整路径。

        不切换项目、不修改 app-state.json、不点任何界面——遇到问题
        只能停下来报告。
        """

        if not self.app.process_running():
            self.app.launch()
            deadline = self.monotonic() + start_wait_s
            while self.monotonic() < deadline:
                if self.app.process_running():
                    break
                self.sleep(0.5)
            if not self.app.process_running():
                raise EngineNeedsUser(
                    "llmwiki_app_missing",
                    "尝试后台启动 LLM Wiki 应用失败，请手动打开它")
            # 进程刚出现，给应用一点时间恢复上一次打开的项目。
            self.sleep(settle_wait_s)
        state = self.app.read_state()
        current, name = self.app.last_project(state)
        if not current:
            raise EngineNeedsUser(
                "llmwiki_wrong_project",
                "读不到 LLM Wiki 当前打开的项目，请打开你的知识库项目")
        try:
            same = Path(current).expanduser().resolve() == self.kb_root.resolve()
        except (OSError, RuntimeError):
            same = False
        if not same:
            label = name or Path(current).name or "未知项目"
            raise EngineNeedsUser(
                "llmwiki_wrong_project",
                f"LLM Wiki 当前打开的是「{label}」，请在 LLM Wiki 里打开你的知识库")

    # ------------------------------------------------------------------
    # ingest：写来源文件（整理由应用自动完成）
    # ------------------------------------------------------------------

    def ingest(self, packages: list[Package]) -> list[IngestResult]:
        """把每条解析包写成 raw/sources/xhs/<note_id>.md。

        - 内容 = frontmatter（title/author/published_at/url/note_id/
          source，字符串用 JSON 转义的双引号形式）+ 空行 + package_md；
        - 先写同目录点开头的临时文件再 os.replace，应用不会读到半个
          文件（见 project.write_source_atomic）；
        - 文件已存在且内容相同则**不重写**——重写会触发应用重复整理；
        - engine_ref = {"source": "xhs/<note_id>.md", "sha256": 指纹}，
          供 distill / source_exists / 清理环节核对。
        """

        results = []
        for pkg in packages:
            content = project.render_source(pkg)
            path = project.source_path(self.kb_root, pkg.note_id)
            unchanged = False
            try:
                unchanged = path.read_text(encoding="utf-8") == content
            except OSError:
                unchanged = False
            if not unchanged:
                project.write_source_atomic(path, content)
            digest = project.sha256_bytes(content.encode("utf-8"))
            results.append(IngestResult(
                note_id=pkg.note_id,
                status="ok",
                source_file=project.source_rel(pkg.note_id),
                engine_ref={"source": project.source_rel(pkg.note_id),
                            "sha256": digest},
            ))
        return results

    # ------------------------------------------------------------------
    # distill：轮询记录文件，逐条判定
    # ------------------------------------------------------------------

    def distill(self, note_ids: list[str],
                timeout_s: float | None = None) -> DistillResult:
        """等待应用整理完这批来源，逐条写进 per_note。

        轮询口径：间隔 poll_interval（默认 5 秒，构造时可注入）；
        总时长默认 条数×3 分钟、至少 10 分钟、最多 60 分钟，可用
        timeout_s 覆盖（测试用秒级超时）。

        每条的判定（按顺序）：
        1. cache 里该键的 hash 等于来源文件当前指纹，且 filesWritten
           列的文件都在 → ok；
        2. 队列里该文件 status=failed 且 retryCount>=3（应用放弃重试）
           → failed，错误代码按 error 原文归类（classify_queue_error）；
        3. 到总时长仍未完成 → 不写进 per_note（保持 ingested），下一
           次运行接着核对。

        统计口径（粗算，写明在返回值和进度记录里）：
        - assigned = ok 条数；
        - pages_rewritten = ok 条的 filesWritten 文件数之和（应用的
          filesWritten 把新增页和被更新的旧页都列在一起，分不开，
          所以这是"写过的页面次数"而不是"新页面数"）；
        - new_topics = ok 条的 filesWritten 里位于五个主题目录
          （concepts/entities/synthesis/comparisons/queries）下的去重
          页面清单——不知道哪些是本轮新建，按"这些主题页被写过"计。

        记录文件损坏（JSON 坏了或形状不认识）：本轮当作"未完成"，
        不判失败也不抛异常；**连续两轮**都读不出时在 errors 里给
        llmwiki_record_unreadable（版本变化把格式改了的信号）。
        """

        ids = list(note_ids)
        total_timeout = timeout_s if timeout_s is not None else min(
            max(len(ids) * PER_NOTE_WAIT_S, MIN_TOTAL_WAIT_S),
            MAX_TOTAL_WAIT_S)
        per_note: dict[str, dict] = {}
        errors: list[dict] = []
        pending = list(ids)
        new_topics: list[str] = []
        seen_topics: set[str] = set()
        pages_rewritten = 0
        consecutive_unreadable = 0
        reported_unreadable = False
        deadline = self.monotonic() + total_timeout

        while pending and self.monotonic() < deadline:
            cache = project.read_ingest_cache(self.kb_root)
            queue = project.read_ingest_queue(self.kb_root)
            if cache is None or queue is None:
                # 损坏或形状不认识：本轮整体当作未完成，绝不判失败。
                consecutive_unreadable += 1
                if consecutive_unreadable >= 2 and not reported_unreadable:
                    errors.append({"code": "llmwiki_record_unreadable",
                                   "detail": ""})
                    reported_unreadable = True
            else:
                consecutive_unreadable = 0
                for note_id in list(pending):
                    rel = project.source_rel(note_id)
                    entry = cache.get(rel)
                    current = project.file_sha256(
                        project.source_path(self.kb_root, note_id))
                    if entry is not None and project.cache_entry_complete(
                            self.kb_root, entry, current):
                        per_note[note_id] = {"status": "ok", "code": None}
                        pages_rewritten += len(entry.get("filesWritten") or [])
                        for page in entry.get("filesWritten") or []:
                            page = str(page)
                            if any(page.startswith(d + "/")
                                   for d in project.WIKI_TOPIC_DIRS) \
                                    and page not in seen_topics:
                                seen_topics.add(page)
                                new_topics.append(page)
                        pending.remove(note_id)
                        continue
                    # 队列里找这条来源的失败任务（sourcePath 相对项目根）。
                    queue_rel = f"{project.SOURCES_DIR_REL.as_posix()}/{note_id}.md"
                    for item in queue:
                        if not isinstance(item, dict):
                            continue
                        if item.get("sourcePath") != queue_rel:
                            continue
                        if item.get("status") == "failed" \
                                and int(item.get("retryCount") or 0) >= 3:
                            per_note[note_id] = {
                                "status": "failed",
                                "code": classify_queue_error(
                                    item.get("error")),
                            }
                            pending.remove(note_id)
                            break
            if pending:
                self.sleep(self.poll_interval)

        status = "partial" if (errors or any(
            v["status"] == "failed" for v in per_note.values())) else "ok"
        return DistillResult(
            status=status,
            assigned=sum(1 for v in per_note.values()
                         if v["status"] == "ok"),
            new_topics=new_topics,
            pages_rewritten=pages_rewritten,
            errors=errors,
            per_note=per_note,
        )

    # ------------------------------------------------------------------
    # source_exists / skill_context
    # ------------------------------------------------------------------

    def source_exists(self, note_id: str, engine_ref: dict) -> bool:
        """这条来源是否确实整理完成且页面还在（清理原件前的核对）。

        engine_ref 是 ingest 时写进台账的引用（{"source": …, "sha256": …}），
        source 缺失时退回标准键 xhs/<note_id>.md。判定标准与 distill
        的完成判定一致：cache 记录的 hash 等于来源文件当前指纹，且
        filesWritten 列出的页面都存在。
        """

        rel = None
        if isinstance(engine_ref, dict):
            rel = engine_ref.get("source")
        if not rel:
            rel = project.source_rel(note_id)
        cache = project.read_ingest_cache(self.kb_root)
        if cache is None:
            return False
        entry = cache.get(rel)
        current = project.file_sha256(
            project.source_path(self.kb_root, note_id))
        return project.cache_entry_complete(self.kb_root, entry, current)

    def skill_context(self) -> dict:
        """查询 Skill 需要的路径和说明（nashsu 目录布局）。

        字段名和 skill.py 的用法对齐：index / topics_dir / sources_dir
        都是相对知识库根目录的写法；topic_dirs 额外给出全部主题目录，
        供说明文案（N02）使用。
        """

        return {
            "kb_root": str(self.kb_root),
            "index": project.WIKI_INDEX,
            "topics_dir": "wiki/concepts",
            "sources_dir": project.WIKI_SOURCES_DIR,
            "topic_dirs": list(project.WIKI_TOPIC_DIRS),
            "engine": self.name,
        }
