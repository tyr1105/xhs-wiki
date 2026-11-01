"""轻量引擎的主体：把解析包整理成 Markdown 知识库。

这个文件在整体架构（docs/architecture.md §6.1）里是引擎的编排层：
- ingest：每条收藏一次模型调用，产出"笔记卡片"，并由代码生成来源页；
- distill：先把新卡片按 20 条一批归类到主题，再逐个重写受影响的主题页，
  最后由代码生成索引页和暂缓页。

三条硬约束贯穿全文件：
1. 不预设任何内容：提示词模板里没有任何具体主题或类别；
2. 输入有上限：每次模型调用的提示词长度都有硬性上限（见各常量）；
3. 结果可追溯 + 出错可恢复：主题页里的 [[来源/…]] 引用必须属于本主题成员；
   所有中间结果存 engine.sqlite3（见 store.py），重复执行不重复调用已完成的步骤。

模型提供方通过 Provider 协议注入：协议声明在 engines/base.py（引擎
共用类型集中在那里）；模型失败只依赖 llm.base 的统一 LLMError，
不依赖具体 SDK 或 CLI 实现。

模块衔接相关的设计：
- Package / IngestResult / DistillResult 从本文件移到 engines/base.py，
  这里改为导入——"from xhs_wiki.engines.markdown.engine import Package"
  这样的老导入路径仍然可用；
- DistillResult 增加 per_note（逐条说明这次整理的结果）；
- 新增 source_exists（清理原件前的第 1 道核对）；
- ingest 支持并发（构造参数 concurrency，默认 3）：并发只用在模型调用
  上，写数据库和写文件仍然串行执行。
"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any, Callable

from ..base import (  # noqa: F401  re-export：老导入路径保持可用
    DistillResult,
    Engine,
    IngestResult,
    Package,
    Provider,
    package_from_dict,
)
from ...llm.base import LLMError
from . import pages
from .store import Store, norm_topic_name

# ---------------------------------------------------------------------------
# Provider 协议（与 docs/architecture.md §8 一致，见 engines/base.py）
# ---------------------------------------------------------------------------
# Provider 已从 engines/base.py 导入；这里保留分节注释说明它的出处。


# ---------------------------------------------------------------------------
# 输入上限相关的常量
# ---------------------------------------------------------------------------

# 解析包发给模型前的截断长度（字数）
PACKAGE_CHAR_LIMIT = 12000
# 截断时加在末尾的标记
_TRUNCATION_MARK = "（以下省略）"
# 主题归类每批新卡片数
ASSIGN_BATCH_SIZE = 20
# 现有主题超过这个数时，先按重合度筛候选再发给模型
TOPIC_CANDIDATE_LIMIT = 80
# 主题页成员卡片上限，超出部分以"更多来源"形式列出
TOPIC_MEMBER_LIMIT = 60
# 每次调用发给 provider 的提示词总长硬上限（字符数）。渲染后超长会截断变量部分。
CARD_PROMPT_LIMIT = 16000
ASSIGN_PROMPT_LIMIT = 24000
TOPIC_PROMPT_LIMIT = 32000
# 传给模型的旧页面概述长度上限
OLD_SUMMARY_LIMIT = 2000

# 错误代码（只含短代码，不含任何链接或敏感内容）
CARD_INVALID = "card_invalid_output"
ASSIGN_INVALID = "assign_invalid_output"
TOPIC_PAGE_INVALID = "topic_page_invalid"
# 熔断表示整个提供方暂时不可继续使用；与 runner 的登录、额度和凭据分支一致。
_INGEST_STOP_CODES = frozenset({"not_logged_in", "quota_or_rate_limit", "auth_failed"})

# 引用记法 [[来源/xxx]] 的提取正则
_CITATION_RE = re.compile(r"\[\[来源/([^\]|]+?)(?:\|[^\]]*)?\]\]")


# ---------------------------------------------------------------------------
# 对外结果类型
# ---------------------------------------------------------------------------
# Package / IngestResult / DistillResult 已移到 engines/base.py，
# 文件顶部已经导入并 re-export，这里不再重复声明。


class StepError(Exception):
    """整理过程中一步失败（重试后仍不行）。code 是上面的错误代码。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class IngestInterrupted(LLMError):
    """入库被提供方熔断，同时携带已经逐条确认的结果供 runner 更新台账。

    completed_results 只含已写入的成功/暂缓，以及确实发生的普通失败；
    未执行的笔记不放进来，避免取消调用也被误扣重试次数。
    failed_note_id 只标识触发熔断的笔记；异常文字只含错误代码。
    """

    def __init__(self, code: str, failed_note_id: str,
                 completed_results: list[IngestResult]) -> None:
        super().__init__(code)
        self.failed_note_id = failed_note_id
        self.completed_results = completed_results


# ---------------------------------------------------------------------------
# 提示词工具
# ---------------------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    """读提示词模板文件。模板用 {{变量}} 占位。"""

    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


def render_prompt(template: str, **variables: str) -> str:
    """把模板里的 {{变量}} 替换成实际值；漏传变量是编程错误，直接抛出。"""

    out = template
    for key, value in variables.items():
        out = out.replace("{{" + key + "}}", value)
    leftover = re.search(r"\{\{(\w+)\}\}", out)
    if leftover:
        raise ValueError(f"prompt_var_missing:{leftover.group(1)}")
    return out


def clip_text(text: str, limit: int) -> str:
    """把一段文本截到 limit 个字符，截断时在末尾注明。"""

    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATION_MARK


def extract_json_object(text: str) -> dict:
    """从模型输出里截取第一个完整的 JSON 对象并解析。

    模型经常在 JSON 外面包 ```json 代码块，或前后多几句说明。做法：
    从第一个 { 开始，逐字符扫描并跟踪字符串/转义状态和花括号深度，
    找到配对的 } 就切出来解析；失败就试下一个 {。
    """

    for start in range(len(text)):
        if text[start] != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        obj = json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # 这一段不是合法 JSON，试下一个 {
                    if isinstance(obj, dict):
                        return obj
                    break
        # 内层扫描没找到配对或解析失败，继续找下一个 {
    raise StepError(CARD_INVALID, "no_json_object")


# ---------------------------------------------------------------------------
# 卡片校验（不引入 JSON Schema 依赖，手写类型/条数/长度检查）
# ---------------------------------------------------------------------------


def validate_card(obj: Any) -> list[str]:
    """校验笔记卡片 JSON。返回错误原因列表；空列表表示通过。

    字段规格见 docs/architecture.md §6.1 第 1 步。
    每条原因都是给人（也是给模型重试时）看的短句。
    """

    def _is_str_list(v: Any, lo: int, hi: int, name: str) -> list[str]:
        if not isinstance(v, list):
            return [f"{name} 必须是数组"]
        if not (lo <= len(v) <= hi):
            return [f"{name} 的条数应在 {lo} 到 {hi} 之间"]
        for item in v:
            if not isinstance(item, str) or not item.strip():
                return [f"{name} 里每一项都必须是非空字符串"]
        return []

    errors: list[str] = []
    if not isinstance(obj, dict):
        return ["输出必须是 JSON 对象"]
    if not isinstance(obj.get("title"), str) or not obj["title"].strip():
        errors.append("title 必须是非空字符串")
    gist = obj.get("gist")
    if not isinstance(gist, str) or not gist.strip():
        errors.append("gist 必须是非空字符串")
    elif len(gist) > 60:
        errors.append("gist 不能超过 60 个字")
    sufficient = obj.get("sufficient")
    if not isinstance(sufficient, bool):
        errors.append("sufficient 必须是 true 或 false")
    # 内容充足时要点必须有 3–8 条；暂缓时模型可能给空列表，放宽到 0 条
    if sufficient is True:
        errors += _is_str_list(obj.get("key_points"), 3, 8, "key_points")
        errors += _is_str_list(obj.get("topic_hints"), 1, 3, "topic_hints")
        if not isinstance(obj.get("category"), str) or not obj["category"].strip():
            errors.append("category 必须是非空字符串")
    else:
        errors += _is_str_list(obj.get("key_points", []), 0, 8, "key_points")
        errors += _is_str_list(obj.get("topic_hints", []), 0, 3, "topic_hints")
    errors += _is_str_list(obj.get("details", []), 0, 30, "details")
    errors += _is_str_list(obj.get("scenarios", []), 0, 20, "scenarios")
    reason = obj.get("insufficient_reason")
    if sufficient is False and (not isinstance(reason, str) or not reason.strip()):
        errors.append("sufficient 为 false 时 insufficient_reason 必须是说明文字")
    if sufficient is True and reason not in (None, ""):
        # 允许 null 或空串；给了文字不算错，忽略
        pass
    return errors


def validate_assignments(
    obj: Any,
    batch_ids: list[str],
    topics_by_id: dict[int, dict],
) -> list[str]:
    """校验归类输出 JSON。返回错误原因列表；空列表表示通过。

    规则：
    - 每条新卡片都必须被归类（恰好一次）；
    - topic_id 必须是本次发给模型的已有主题；
    - 新主题名称 2–20 字、描述不超过 80 字。
    与已有主题重名不算错误，由引擎在应用时并入已有主题。
    """

    if not isinstance(obj, dict) or not isinstance(obj.get("assignments"), list):
        return ["输出必须是含 assignments 数组的 JSON 对象"]
    errors: list[str] = []
    seen: dict[str, int] = {}
    for item in obj["assignments"]:
        if not isinstance(item, dict) or not isinstance(item.get("note_id"), str):
            errors.append("assignments 里每一项都必须含字符串 note_id")
            continue
        nid = item["note_id"]
        seen[nid] = seen.get(nid, 0) + 1
        if "topic_id" in item and item["topic_id"] is not None:
            tid = item["topic_id"]
            if not (isinstance(tid, int) or (isinstance(tid, str) and tid.isdigit())):
                errors.append(f"{nid}：topic_id 必须是数字")
            elif int(tid) not in topics_by_id:
                errors.append(f"{nid}：topic_id 不在现有主题列表里")
        elif isinstance(item.get("new_topic"), dict):
            nt = item["new_topic"]
            name = nt.get("name")
            if not isinstance(name, str) or not (2 <= len(name.strip()) <= 20):
                errors.append(f"{nid}：新主题名称需要 2 到 20 个字")
            desc = nt.get("description")
            if not isinstance(desc, str) or not desc.strip():
                errors.append(f"{nid}：新主题缺少描述")
            elif len(desc) > 80:
                errors.append(f"{nid}：新主题描述不能超过 80 个字")
        else:
            errors.append(f"{nid}：必须给 topic_id 或 new_topic 之一")
    for nid in batch_ids:
        if seen.get(nid, 0) == 0:
            errors.append(f"{nid}：这条卡片没有被归类")
        elif seen[nid] > 1:
            errors.append(f"{nid}：这条卡片被归类了多次")
    for nid in seen:
        if nid not in batch_ids:
            errors.append(f"{nid}：不在本批新卡片里")
    return errors


def _validate_topic_page(
    markdown: str,
    valid_refs: set[str],
) -> list[str]:
    """校验主题页输出：固定章节齐全；所有 [[来源/…]] 引用都属于本主题成员。"""

    errors: list[str] = []
    stripped = markdown.strip()
    if not stripped.startswith("#"):
        errors.append("页面必须以一级标题 # 开头")
    for section in ("## 概述", "## 要点", "## 对比与推荐", "## 注意事项", "## 来源"):
        if section not in markdown:
            errors.append(f"缺少规定的章节：{section}")
    for ref in _CITATION_RE.findall(markdown):
        if ref.strip() not in valid_refs:
            errors.append(f"引用了不属于本主题的来源页：{ref.strip()}")
    return errors


def _bigrams(text: str) -> set[str]:
    """字符串的字符二元组集合（相邻两字一组）；单字串就返回它自己。

    用来在主题超过 80 个时，按卡片提示与主题名称/描述的重合度筛候选：
    两个集合的交集越大，说明卡片和这个主题越可能相关。
    """

    s = str(text)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


# ---------------------------------------------------------------------------
# 引擎主体
# ---------------------------------------------------------------------------


class MarkdownEngine:
    """把解析包整理成 Markdown 知识库的引擎（architecture.md §6 的 Engine 实现）。"""

    name = "markdown"

    def __init__(
        self,
        kb_root: Path | str,
        provider: Provider,
        *,
        clock: Callable[[], datetime] | None = None,
        concurrency: int = 3,
    ) -> None:
        """concurrency：ingest 时同时进行的模型调用数（默认 3）。

        并发只用在模型调用上——生成笔记卡片（网络/子进程最耗时）可以同时
        做；写数据库和写文件仍然串行执行，保证 SQLite 不被多线程同时写，
        返回结果的顺序仍与输入一致。并发数为 1 时完全退化为串行。
        """

        self.kb_root = Path(kb_root).expanduser()
        self.provider = provider
        self.clock = clock or datetime.now
        self.concurrency = max(1, int(concurrency))
        self._store: Store | None = None

    # -- 目录与存储 ------------------------------------------------------

    @property
    def topics_dir(self) -> Path:
        return self.kb_root / "主题"

    @property
    def sources_dir(self) -> Path:
        return self.kb_root / "来源"

    @property
    def db_path(self) -> Path:
        return self.kb_root / ".xhs-wiki" / "engine.sqlite3"

    @property
    def store(self) -> Store:
        """懒打开状态库；health 检查失败时不会走到这里。"""

        if self._store is None:
            # ingest 也可能直接调用而不先 health，因此在首次打开库前建好私有根目录。
            # mkdir 的 mode 只作用于新目录，不会修改用户已有目录的权限。
            self.kb_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._store = Store(self.db_path)
        return self._store

    def _now_iso(self) -> str:
        return self.clock().isoformat()

    def _now_date(self) -> str:
        return self.clock().strftime("%Y-%m-%d")

    def _complete(self, *, system: str, prompt: str, max_output_tokens: int) -> str:
        """调用一次模型。提示词长度做最后的硬性保护（不应触发，防患于未然）。"""

        return self.provider.complete(
            system=system,
            prompt=prompt,
            max_output_tokens=max_output_tokens,
            timeout_s=300,
        )

    # -- 对外接口 --------------------------------------------------------

    def health(self) -> tuple[bool, str]:
        """检查知识库目录可写、数据库可打开。provider 可用性由 runner 另查。"""

        try:
            for d in (self.kb_root, self.topics_dir, self.sources_dir):
                # 仅新建目录使用私有权限；用户选择的已有目录保持原权限。
                d.mkdir(mode=0o700, parents=True, exist_ok=True)
            probe = self.kb_root / ".xhs-wiki-write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            st = self.store  # 打开（并建好）数据库
            st.conn.execute("SELECT COUNT(*) FROM topics").fetchone()
        except OSError as exc:
            return (False, f"kb_not_writable:{type(exc).__name__}")
        except Exception as exc:  # sqlite3.Error 等
            return (False, f"engine_db_error:{type(exc).__name__}")
        return (True, "ok")

    def skill_context(self) -> dict:
        """Claude Skill 需要的路径和说明。"""

        return {
            "kb_root": str(self.kb_root),
            "index": "索引.md",
            "topics_dir": "主题",
            "sources_dir": "来源",
            "engine": "markdown",
        }

    def source_exists(self, note_id: str, engine_ref: dict) -> bool:
        """这条笔记的来源是否确实已入引擎（清理原件前的第 1 道核对）。

        判定标准：来源页文件存在，并且 engine.sqlite3 里有
        这条笔记的卡片——两个都在才算入库。engine_ref 是 ingest 时写进
        台账的引用（{"source": 来源页文件名}）；引用里没有 source 时退回
        卡片记录的文件名（老数据兜底）。
        """

        row = self.store.get_card(note_id)
        if row is None:
            return False
        source = None
        if isinstance(engine_ref, dict):
            source = engine_ref.get("source")
        if not source:
            source = row["source_file"]
        if not source:
            return False
        return (self.sources_dir / str(source)).is_file()

    # -- ingest：笔记卡片 + 来源页 ---------------------------------------

    def ingest(self, packages: list[Package]) -> list[IngestResult]:
        """逐条生成卡片、串行落库，单条调用失败不丢弃其他已付费的结果。

        已存在的卡片直接返回，不重复调用模型。并发时最多派发 concurrency
        条任务，完成一条才补一条，结果按输入下标保存、最终按输入顺序返回。
        普通 LLMError 只产生一条 failed；登录/额度/凭据错误则立即停止派发，
        取消尚未开始的任务，并收取仍在运行的成功结果后抛 IngestInterrupted。
        """

        results: list[IngestResult | None] = [None] * len(packages)
        todo: list[int] = []
        for i, pkg in enumerate(packages):
            if self.store.has_card(pkg.note_id):
                row = self.store.get_card(pkg.note_id)
                results[i] = IngestResult(
                    note_id=pkg.note_id,
                    status=row["status"],
                    source_file=row["source_file"],
                    insufficient_reason=row["insufficient_reason"],
                    engine_ref={"source": row["source_file"], "cached": True},
                )
            else:
                todo.append(i)

        # Event 是可在线程之间传递的停止信号。工作线程一发现熔断就置位，
        # 不必等主线程处理结果，尚未开始调用模型的任务便能及时退出。
        stopped = Event()
        cancelled = object()
        interruption: tuple[int, LLMError] | None = None

        def _call(i: int):
            """工作线程只调用模型；把可预料错误作为单条结果交回主线程。"""
            if stopped.is_set():
                return cancelled
            try:
                return self._generate_card(packages[i])
            except LLMError as exc:
                if exc.code in _INGEST_STOP_CODES:
                    stopped.set()
                return exc

        def _collect(i: int, outcome) -> None:
            """只在主线程写来源页和数据库，保留成功及实际失败的逐条证据。"""
            nonlocal interruption
            pkg = packages[i]
            if outcome is cancelled:
                return
            if isinstance(outcome, LLMError):
                if outcome.code in _INGEST_STOP_CODES:
                    if interruption is None:
                        interruption = (i, outcome)
                    return
                results[i] = IngestResult(note_id=pkg.note_id,
                                          status="failed", error=outcome.code)
            elif outcome is None:
                results[i] = IngestResult(note_id=pkg.note_id,
                                          status="failed", error=CARD_INVALID)
            else:
                results[i] = self._finalize_ingest(pkg, outcome)

        if self.concurrency > 1 and len(todo) > 1:
            pool = ThreadPoolExecutor(max_workers=self.concurrency)
            pending = {}
            remaining = iter(todo)
            try:
                while True:
                    # 只补充当前空闲的名额，熔断后不会继续提交整批剩余任务。
                    while not stopped.is_set() and len(pending) < self.concurrency:
                        i = next(remaining, None)
                        if i is None:
                            break
                        pending[pool.submit(_call, i)] = i
                    if not pending:
                        break
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in sorted(done, key=lambda item: pending[item]):
                        i = pending.pop(future)
                        if not future.cancelled():
                            _collect(i, future.result())
                    if stopped.is_set():
                        # 先保存本轮已完成的成功结果，再取消尚未开跑的 future。
                        # 正在运行的调用无法强制取消，下一轮仍收取并落库其结果。
                        for future in pending:
                            future.cancel()
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
        else:
            for i in todo:
                _collect(i, _call(i))
                if stopped.is_set():
                    break

        completed = [result for result in results if result is not None]
        if interruption is not None:
            i, error = interruption
            raise IngestInterrupted(error.code, packages[i].note_id, completed) from error
        return completed

    def _generate_card(self, pkg: Package) -> dict | None:
        """调模型生成一张笔记卡片（只做模型调用和校验，不写任何东西）。

        校验失败就把原因附在提示词后面重试 1 次（共最多 2 次调用）。
        两次都不合格返回 None（由调用方按 failed 处理）。这个方法可以被
        多线程同时执行——它不碰 self.store、不写文件。
        """

        template = load_prompt("card.md")
        pkg_text = clip_text(pkg.package_md, PACKAGE_CHAR_LIMIT)
        prompt = render_prompt(template, package=pkg_text)[:CARD_PROMPT_LIMIT]

        for _attempt in range(2):
            output = self._complete(
                system="你是知识库整理助手，严格按照要求的格式输出。",
                prompt=prompt,
                max_output_tokens=2000,
            )
            try:
                obj = extract_json_object(output)
                errors = validate_card(obj)
            except StepError as exc:
                errors = [exc.detail or "输出里没有 JSON 对象"]
            if not errors:
                return obj
            prompt = (
                prompt
                + "\n\n上一次输出不合格，原因："
                + "；".join(errors)
                + "。请重新只输出一个符合要求的 JSON 对象。"
            )
        return None

    def _finalize_ingest(self, pkg: Package, card: dict) -> IngestResult:
        """把生成好的卡片落库并写来源页（串行段执行，不调模型）。"""

        now = self._now_iso()
        sufficient = bool(card["sufficient"])
        status = "ok" if sufficient else "deferred"
        reason = card.get("insufficient_reason") if not sufficient else None
        filename = source_filename_for(pkg, self._now_date())
        self._write_source_page(pkg, card, filename, status, topics=[])

        self.store.save_card(
            note_id=pkg.note_id,
            card_json=json.dumps(card, ensure_ascii=False),
            status=status,
            error=None,
            insufficient_reason=reason,
            title=pkg.title,
            url=pkg.url,
            author=pkg.author,
            published_at=pkg.published_at,
            list_position=pkg.list_position,
            source_file=filename,
            created_at=now,
            updated_at=now,
        )
        return IngestResult(
            note_id=pkg.note_id,
            status=status,
            source_file=filename,
            insufficient_reason=reason,
            engine_ref={"source": filename},
        )

    def _write_source_page(
        self,
        pkg: Package,
        card: dict,
        filename: str,
        status: str,
        topics: list[str],
    ) -> None:
        """生成来源页文件（纯代码，不调用模型）。"""

        text = pages.render_source_page(
            note_id=pkg.note_id,
            title=pkg.title,
            url=pkg.url,
            author=pkg.author,
            published_at=pkg.published_at,
            status=status,
            topics=topics,
            gist=card.get("gist", ""),
            key_points=card.get("key_points", []),
            details=card.get("details", []),
            scenarios=card.get("scenarios", []),
            raw_text=pkg.package_md,
        )
        pages.atomic_write(self.sources_dir / filename, text)

    # -- distill：主题归类 + 主题页重写 + 索引 ----------------------------

    def distill(self, note_ids: list[str] | None = None) -> DistillResult:
        """把新入库的收藏整合进主题。分四步，每步都从断点继续：

        1. 归类：待归类卡片（status=ok 且未入主题）每 20 条一批调一次模型；
        2. 回填：更新相关来源页 frontmatter 的 topics；
        3. 重写：pending_pages 队列里的主题逐个重写主题页；
        4. 索引：重新生成 索引.md 和 暂缓.md。

        任何一批失败都不影响其他批次（错误记进结果）。模型调用抛出的
        LLMError：非熔断错误（超时、暂不可用、输出解析失败）只算那一批或
        那一个主题失败；熔断类错误（未登录、额度、凭据）和
        其他异常向上传播，已完成的步骤都落了库，下次调用从断点继续。

        per_note 逐条说明结果：归类成功且主题页写入成功 → ok；
        主题页失败 → 该主题下本次新归入的笔记 failed（topic_page_invalid）；
        归类失败 → failed（对应错误代码）。
        """

        errors: list[dict] = []
        new_topics: list[str] = []
        assigned_count = 0
        per_note: dict[str, dict] = {}   # note_id -> {"status", "code"}
        # 不只跟踪本次新归类的成员，也读回上次中断前已经归类的成员。
        note_topics = self.store.note_topic_ids(note_ids)

        pending = self.store.cards_pending_assignment(note_ids)
        for start in range(0, len(pending), ASSIGN_BATCH_SIZE):
            batch = pending[start : start + ASSIGN_BATCH_SIZE]
            try:
                assignments = self._assign_batch(batch)
            except StepError as exc:
                errors.append({"code": exc.code, "detail": exc.detail})
                # 这一批的卡片全部记失败（下次 distill 还会重试）
                for row in batch:
                    per_note[row["note_id"]] = {"status": "failed",
                                                "code": exc.code}
                continue
            except LLMError as exc:
                # 模型调用本身出错（超时、服务暂不可用、输出
                # 解析失败）只算这一批失败，其他批次照常继续；只有熔断类
                # 错误（未登录、额度、凭据）才继续往上抛，交给 runner 熔断。
                if exc.code in _INGEST_STOP_CODES:
                    raise
                errors.append({"code": exc.code, "detail": ""})
                for row in batch:
                    per_note[row["note_id"]] = {"status": "failed",
                                                "code": exc.code}
                continue
            made_new, pairs = self._apply_assignments(assignments, batch)
            new_topics.extend(made_new)
            assigned_count += len(batch)
            for nid, topic_id in pairs:
                note_topics.setdefault(nid, set()).add(topic_id)

        # 归类结果已在 _apply_assignments 里回填来源页 frontmatter；
        # 先按当前结构出一版索引，主题页重写后再出最终版。
        self._write_index()

        page_ids = self.store.pending_page_ids()
        pending_ids = set(page_ids)
        # 这些成员的主题页已在上次运行写好，只是 runner 可能尚未推进台账；
        # 直接补记成功，不重复归类或调用模型。仍在队列里的成员等本次结果。
        for nid, topic_ids in note_topics.items():
            if not topic_ids & pending_ids:
                per_note[nid] = {"status": "ok", "code": None}

        pages_rewritten = 0
        for topic_id in page_ids:
            try:
                sha = self._rewrite_topic_page(topic_id)
            except StepError as exc:
                # 重试后仍不合格：保留旧页面，记错误，不影响其他主题；
                # 这个主题下本次请求的成员都记 topic_page_invalid，包含恢复的
                # 旧成员；保留待重写记录，防止下次被误当成已完成页面。
                errors.append({"code": exc.code, "detail": exc.detail})
                for nid, topic_ids in note_topics.items():
                    if topic_id in topic_ids:
                        per_note[nid] = {"status": "failed",
                                         "code": TOPIC_PAGE_INVALID}
                continue
            except LLMError as exc:
                # 重写这个主题页时模型调用出错（超时等非熔断
                # 错误），只算这一个主题失败——只给它的成员记这个错误码，
                # 页面留在待重写队列，下次再试；其他主题照常重写。以前整个
                # distill 直接抛出，runner 给这批所有笔记各记一次失败，主题页
                # 早已写好的笔记连续 3 次后也会被误判成 failed:distill。
                # 熔断类错误（未登录、额度、凭据）照旧往上抛。
                if exc.code in _INGEST_STOP_CODES:
                    raise
                errors.append({"code": exc.code, "detail": ""})
                for nid, topic_ids in note_topics.items():
                    if topic_id in topic_ids:
                        per_note[nid] = {"status": "failed",
                                         "code": exc.code}
                continue
            self.store.finish_page(topic_id, sha)
            pending_ids.discard(topic_id)
            for nid, topic_ids in note_topics.items():
                if topic_id in topic_ids and not topic_ids & pending_ids:
                    per_note[nid] = {"status": "ok", "code": None}
            pages_rewritten += 1

        # 主题结构可能变了（新主题、成员数变化），索引最后再写一次
        self._write_index()
        status = "partial" if errors else "ok"
        return DistillResult(
            status=status,
            assigned=assigned_count,
            new_topics=new_topics,
            pages_rewritten=pages_rewritten,
            errors=errors,
            per_note=per_note,
        )

    # 归类一步 ------------------------------------------------------------

    def _candidate_topics(self, batch: list) -> list:
        """主题超过 80 个时按重合度筛前 80 个候选，否则全部返回。

        重合度 = 卡片的 topic_hints+标题 的字符二元组 与 主题名称+描述的
        字符二元组 的交集大小。这是纯代码的粗筛，为的是不把几百个主题
        一次性塞给模型（输入上限约束）。
        """

        topics = self.store.list_topics()
        if len(topics) <= TOPIC_CANDIDATE_LIMIT:
            return list(topics)
        hint_grams: set[str] = set()
        for row in batch:
            card = json.loads(row["card_json"])
            hint_grams |= _bigrams("".join(card.get("topic_hints", [])))
            hint_grams |= _bigrams(row["title"] or "")

        counts = self.store.topic_member_counts()

        def score(topic) -> tuple:
            grams = _bigrams(topic["name"] or "") | _bigrams(topic["description"] or "")
            return (len(grams & hint_grams), -int(topic["id"]))

        ranked = sorted(topics, key=score, reverse=True)
        return ranked[:TOPIC_CANDIDATE_LIMIT]

    def _assign_batch(self, batch: list) -> list[dict]:
        """一批卡片调一次模型归类，带校验和 1 次重试。返回 assignments 列表。"""

        candidates = self._candidate_topics(batch)
        topics_by_id = {int(t["id"]): dict(t) for t in candidates}
        counts = self.store.topic_member_counts()

        topics_block = "\n".join(
            "- id={id}｜名称：{name}｜描述：{desc}｜类别：{cat}｜成员数：{n}".format(
                id=t["id"],
                name=t["name"],
                desc=t["description"] or "",
                cat=t["category"] or "",
                n=counts.get(int(t["id"]), 0),
            )
            for t in candidates
        )
        if not topics_block:
            topics_block = "（暂无主题）"

        # 现有类别清单：实测发现同一批收藏被分成
        # "意思相同的两个类别"，所以把现有类别也放进提示词，要求新主题
        # 优先沿用已有类别。类别从候选主题里汇总（按出现次数从多到少），
        # 内容全部来自用户自己的库，模板里不预设任何类别词。
        category_counts: dict[str, int] = {}
        for t in candidates:
            name = str(t["category"] or "").strip()
            if name:
                category_counts[name] = category_counts.get(name, 0) + 1
        categories_block = "、".join(
            name for name, _ in sorted(category_counts.items(),
                                       key=lambda kv: (-kv[1], kv[0]))
        ) or "（暂无类别）"

        cards_block = "\n".join(
            "- note_id：{nid}｜标题：{title}｜概要：{gist}｜主题提示：{hints}｜类别：{cat}".format(
                nid=row["note_id"],
                title=(row["title"] or "")[:60],
                gist=(json.loads(row["card_json"]).get("gist") or "")[:60],
                hints="、".join(json.loads(row["card_json"]).get("topic_hints", []))[:60],
                cat=(json.loads(row["card_json"]).get("category") or "")[:20],
            )
            for row in batch
        )

        template = load_prompt("assign.md")
        prompt = render_prompt(template, topics=topics_block,
                               categories=categories_block, cards=cards_block)
        # 输入上限保护：超长就截掉变量部分尾部
        if len(prompt) > ASSIGN_PROMPT_LIMIT:
            overhead = len(template) + 40
            budget = ASSIGN_PROMPT_LIMIT - overhead
            prompt = render_prompt(
                template,
                topics=clip_text(topics_block, budget // 2),
                categories=clip_text(categories_block, 200),
                cards=clip_text(cards_block, budget // 2),
            )

        batch_ids = [row["note_id"] for row in batch]
        errors: list[str] = []
        for _attempt in range(2):
            output = self._complete(
                system="你是知识库整理助手，严格按照要求的格式输出。",
                prompt=prompt,
                max_output_tokens=3000,
            )
            try:
                obj = extract_json_object(output)
                errors = validate_assignments(obj, batch_ids, topics_by_id)
            except StepError as exc:
                errors = [exc.detail or "输出里没有 JSON 对象"]
            if not errors:
                return obj["assignments"]
            prompt = (
                prompt
                + "\n\n上一次输出不合格，原因："
                + "；".join(errors)
                + "。请重新只输出一个符合要求的 JSON 对象。"
            )
        raise StepError(ASSIGN_INVALID, "；".join(errors[:5]))

    def _apply_assignments(self, assignments: list[dict], batch: list) -> tuple[list[str], list[tuple[str, int]]]:
        """把归类结果落库：建新主题、加成员、回填来源页、把主题排进重写队列。

        新主题与已有主题重名（按规范化名称比较）时并入已有主题。
        返回 (本次新建的主题名列表, [(note_id, 主题 id), …])；第二个值供
        distill 记 per_note——主题页重写失败时按主题反查受影响的笔记。
        """

        now = self._now_iso()
        rows_by_id = {row["note_id"]: row for row in batch}
        new_names: list[str] = []
        pairs: list[tuple[str, int]] = []

        for item in assignments:
            nid = item["note_id"]
            row = rows_by_id[nid]
            card = json.loads(row["card_json"])
            if item.get("topic_id") is not None:
                topic = self.store.conn.execute(
                    "SELECT * FROM topics WHERE id = ?", (int(item["topic_id"]),)
                ).fetchone()
            else:
                nt = item["new_topic"]
                name = str(nt["name"]).strip()
                existing = self.store.topic_by_norm(norm_topic_name(name))
                if existing is not None:
                    topic = existing
                else:
                    topic_id = self.store.create_topic(
                        name=name,
                        description=str(nt.get("description", "")).strip(),
                        category=str(nt.get("category") or card.get("category") or "").strip(),
                        now=now,
                    )
                    new_names.append(name)
                    topic = self.store.conn.execute(
                        "SELECT * FROM topics WHERE id = ?", (topic_id,)
                    ).fetchone()
            self.store.add_member(int(topic["id"]), nid, now)
            self.store.queue_page(int(topic["id"]), now)
            pairs.append((nid, int(topic["id"])))
            # 回填来源页 frontmatter 的 topics（重新生成整页也可，这里只改一行，
            # 保证"原文部分不变"是结构上成立的）
            self._backfill_source_topics(row, str(topic["name"]))
        return new_names, pairs

    def _backfill_source_topics(self, card_row, topic_name: str) -> None:
        """更新来源页 frontmatter 的 topics 为该笔记所属主题。"""

        path = self.sources_dir / card_row["source_file"]
        if not path.exists():
            return  # 来源页不在（理论上来 ingest 都写过），跳过而不是崩
        text = path.read_text(encoding="utf-8")
        updated = pages.replace_frontmatter_topics(text, [topic_name])
        pages.atomic_write(path, updated)

    # 主题页一步 ----------------------------------------------------------

    def _rewrite_topic_page(self, topic_id: int) -> str:
        """重写一个主题的主题页；成功返回页面内容的 sha256，失败抛 StepError。

        成员超过 60 张时只把 list_position 最小（最新收藏）的 60 张给模型，
        超出部分由代码在页面末尾以"更多来源"列出。
        """

        topic = self.store.conn.execute(
            "SELECT * FROM topics WHERE id = ?", (topic_id,)
        ).fetchone()
        members = self.store.members_with_cards(topic_id)
        shown = members[:TOPIC_MEMBER_LIMIT]
        overflow = members[TOPIC_MEMBER_LIMIT:]

        name = str(topic["name"])
        page_path = self.topics_dir / f"{pages.sanitize_for_filename(name, 50)}.md"
        old_summary = "（无，这是这个主题的第一个页面）"
        if page_path.exists():
            old = page_path.read_text(encoding="utf-8")
            section = pages.extract_section(old, "概述")
            if section:
                old_summary = clip_text(section, OLD_SUMMARY_LIMIT)

        # 所有成员的来源页文件名（去掉 .md）都是合法引用对象
        valid_refs = {str(m["source_file"])[:-3] for m in members}

        def card_block(m) -> str:
            card = json.loads(m["card_json"])
            points = "；".join(card.get("key_points", []))
            details = "；".join(card.get("details", []))
            return (
                f"### {m['title'] or '无标题'}（note_id：{m['note_id']}）\n"
                f"- 概要：{card.get('gist', '')}\n"
                f"- 要点：{points}\n"
                f"- 细节：{details}\n"
                f"- 来源页：{str(m['source_file'])[:-3]}\n"
            )

        cards_text = "\n".join(card_block(m) for m in shown)
        sources_text = "\n".join(
            f"- [[来源/{str(m['source_file'])[:-3]}]]" for m in members
        )

        template = load_prompt("topic_page.md")
        prompt = render_prompt(
            template,
            topic_name=name,
            topic_description=str(topic["description"] or ""),
            old_summary=old_summary,
            cards=cards_text,
            sources=sources_text,
        )
        # 输入上限保护
        if len(prompt) > TOPIC_PROMPT_LIMIT:
            overhead = len(template) + 200
            budget = TOPIC_PROMPT_LIMIT - overhead
            prompt = render_prompt(
                template,
                topic_name=name,
                topic_description=str(topic["description"] or ""),
                old_summary=clip_text(old_summary, 400),
                cards=clip_text(cards_text, int(budget * 0.8)),
                sources=clip_text(sources_text, int(budget * 0.2)),
            )

        errors: list[str] = []
        body = ""
        for _attempt in range(2):
            output = self._complete(
                system="你是知识库整理助手，严格按照要求的格式输出。",
                prompt=prompt,
                max_output_tokens=4000,
            )
            body = output.strip()
            errors = _validate_topic_page(body, valid_refs)
            if not errors:
                break
            prompt = (
                prompt
                + "\n\n上一次输出不合格，原因："
                + "；".join(errors[:5])
                + "。请重新只输出符合要求结构的 Markdown。"
            )
        if errors:
            # 保留旧页面（不写任何东西），向上报告错误代码
            raise StepError(TOPIC_PAGE_INVALID, "；".join(errors[:5]))

        if overflow:
            more = ["", "## 更多来源", "", "本主题成员较多，以下来源未参与本次综合："]
            more += [f"- [[来源/{str(m['source_file'])[:-3]}]]" for m in overflow]
            more.append("")
            body = body.rstrip() + "\n".join(more)

        pages.atomic_write(page_path, body)
        self.store.set_topic_updated(topic_id, self._now_iso())
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    # 索引 / 暂缓页 --------------------------------------------------------

    def _write_index(self) -> None:
        """由代码生成 索引.md 和 暂缓.md（不调用模型）。"""

        counts = self.store.topic_member_counts()
        topics = [dict(t) for t in self.store.list_topics()]
        for t in topics:
            t["count"] = counts.get(int(t["id"]), 0)
        # 类别按主题数从多到少排，同数按类别名稳定排序
        groups: dict[str, list[dict]] = {}
        for t in topics:
            groups.setdefault(t.get("category") or "未分类", []).append(t)
        ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))

        recent_rows = self.store.recent_cards(20)
        recent = [
            {"file_stem": str(r["source_file"])[:-3], "title": r["title"]}
            for r in recent_rows
        ]
        deferred_rows = self.store.deferred_cards()
        index_text = pages.render_index(
            groups=ordered,
            recent=recent,
            deferred_count=len(deferred_rows),
        )
        pages.atomic_write(self.kb_root / "索引.md", index_text)

        entries = [
            {
                "file_stem": str(r["source_file"])[:-3],
                "title": r["title"],
                "reason": r["insufficient_reason"],
            }
            for r in deferred_rows
        ]
        pages.atomic_write(self.kb_root / "暂缓.md", pages.render_deferred_page(entries))


def source_filename_for(pkg: Package, ingest_date: str) -> str:
    """来源页文件名（抽成模块级函数方便测试）：日期优先发布时间，缺陷入库日期。"""

    return pages.source_filename(
        title=pkg.title,
        published_at=pkg.published_at,
        ingest_date=ingest_date,
        note_id=pkg.note_id,
    )
