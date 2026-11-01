# -*- coding: utf-8 -*-
"""engines/llmwiki/review.py —— 整理候选的轻量检查（纯函数）。

这个文件做什么：对 LLM Wiki 后台生成的一组"整理候选改动"做机械检查，
输出三种决定之一：confirm（采用）、reject（自动驳回）、needs_user（交给
用户在桌面版或控制台里决定）。不调用模型、不做语义对照
（semantic_comparison_performed=false）：这里只核对改动范围与来源引用。

在整体里的位置：compiler.py 读完全部候选差异后调用本模块的 light_review；
通过 → confirm，丢引用/未知引用 → cancel 驳回，其余一律停下交给用户。

术语说明（给非程序员）：
- "整理候选"（pending action）：模型整理知识库时产生的改动包，必须经过
  确认（confirm）才会真正写进知识库；驳回（cancel）则整包丢弃。
- "来源引用"（source reference）：派生页面里指向来源原件的链接。原生
  知识库里来源页放在 wiki/sources/ 下（原始材料在 raw/sources/ 下），派生
  页面用 frontmatter `sources: [...]` 和 `> Sources:` 行引用它们。引用路径
  的正则以 LLM Wiki 后端源码为准（compile_instructions.rs：来源以
  "wiki/sources/.../original.md" 的完整路径引用）；旧 compat 布局的
  "raw/sources/local/..." 路径也一并识别，保持兼容。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

# 交给用户决定的门槛：改动前就已存在、且差异里真有增删的页面数达到
# 这个值（新建页和空差异不算）。
NEEDS_USER_EXISTING_PAGES = 10

# 来源引用的路径模式：原生知识库的 wiki/sources/ 或 raw/sources/ 下的
# .md 文件。路径里可能有中文、空格、子目录，但不能包含空白和 Markdown
# 链接的收尾符号（] ) " |）。
SOURCE_REF_RE = re.compile(r"(?:raw|wiki)/sources/[^\s\]\)\"|]+?\.md")

# 差异文本里不算"增删行"的前缀：+++ / --- 文件头、@@ 行号、``` 围栏、
# "\ No newline" 标记。真正的增删行以 + 或 - 开头。
_DIFF_META_PREFIXES = ("+++", "---", "@@", "```", "\\ No newline")


def diff_has_changes(diff: str) -> bool:
    """差异文本里是否有真正的增删行。

    后台（V8 起）会给没改动的结构页附一条只有上下文的"空差异"，这种
    不算改动；affectedPaths 的比对和"10 个既有页面"的计数都要跳过它们。
    """

    for line in str(diff or "").splitlines():
        if line.startswith(_DIFF_META_PREFIXES):
            continue
        if line.startswith(("+", "-")):
            return True
    return False


def _is_new_file(diff: str) -> bool:
    """从差异本身判断这是不是新建文件（改动前不存在）。

    统一 diff 里新建文件的旧侧是 /dev/null。调用方（compiler）知道磁盘
    上的实际情况，会在 file_diffs 条目里带上 existed_before；这个函数
    只在调用方没给时兜底使用。
    """

    for line in str(diff or "").splitlines():
        if line.startswith("---"):
            return line.split("\t")[0].strip() == "--- /dev/null"
    return False


def _refs_by_side(diff: str) -> tuple[set[str], set[str]]:
    """把一条统一差异拆成（改动前引用, 改动后引用）两个集合。

    规则：上下文行（空格开头）两侧都有；删除行（- 开头）属于改动前；
    新增行（+ 开头）属于改动后。文件头、@@、围栏等元数据行跳过。
    """

    before: set[str] = set()
    after: set[str] = set()
    for line in str(diff or "").splitlines():
        if line.startswith(_DIFF_META_PREFIXES):
            continue
        if not line:
            continue
        marker, text = line[0], line[1:]
        if marker in (" ", "-"):
            before.update(SOURCE_REF_RE.findall(text))
        if marker in (" ", "+"):
            after.update(SOURCE_REF_RE.findall(text))
    return before, after


def _path_is_safe(path: str) -> bool:
    """路径必须在 wiki/ 下、是 .md 文件、且没有 .. 之类的目录逃逸。"""

    if not isinstance(path, str) or not path.startswith("wiki/") or not path.endswith(".md"):
        return False
    parts = PurePosixPath(path).parts
    return ".." not in parts and "." not in parts


def light_review(pending_action, file_diffs, known_source_paths) -> dict:
    """轻量检查一份整理候选，返回 {"decision", "reason"}。

    参数：
    - pending_action：后台的 pendingAction（dict，含 id / affectedPaths）。
      调用方会把 decisionReview 里的 counts 和 userEditsDetected 合并进来
      （这两个事实在后台的 decisionReview 里，不在 pendingAction 本身）。
    - file_diffs：每个改动文件的列表，每项是
      {"file_id", "path", "diff", "existed_before"}；existed_before 表示
      改动前文件是否已存在（compiler 按磁盘实际情况填，缺省时用差异里的
      /dev/null 标记推断）。
    - known_source_paths：已登记的全部来源页路径（wiki/sources/… 相对
      路径的集合）。本组要整理的来源也必须由调用方并进这个集合。

    决定规则：
    1. 有删除、覆盖或用户编辑冲突 → needs_user；
    2. 改动前已存在且真有增删的页面 ≥ 10（或后台报告 modified ≥ 10）
       → needs_user；新建页和空差异不计入；
    3. affectedPaths 必须恰好等于真正有增删的路径集合，且都在 wiki/ 下、
       不碰 wiki/sources/（来源页由导入独占，模型不能改）→ 否则 needs_user；
    4. 改动前的来源引用一条都不能少 → reject（丢了引用的候选整页重写时
       常见，驳回不写知识库、来源未消费，可留给重试）；
    5. 新增引用只能指向已登记的来源 → 否则 reject；
    6. 其余情况 → confirm。
    """

    pending = pending_action if isinstance(pending_action, dict) else {}
    counts = pending.get("counts") or {}
    diffs = list(file_diffs or [])
    known = {str(p) for p in (known_source_paths or set())}

    # ---- 规则 1：删除 / 覆盖 / 用户编辑冲突，必须由用户决定 --------------
    if pending.get("userEditsDetected"):
        return {"decision": "needs_user", "reason": "user_edits_detected"}
    if int(counts.get("deleted") or 0) > 0:
        return {"decision": "needs_user", "reason": "deleted_pages"}
    if int(counts.get("overwritten") or 0) > 0:
        return {"decision": "needs_user", "reason": "overwritten_pages"}

    # ---- 先逐条做结构检查，顺便收集"真正有改动的路径" --------------------
    changed_paths: set[str] = set()
    existing_changed = 0  # 改动前已存在、且真有增删的页面数
    for item in diffs:
        path = (item or {}).get("path")
        diff = (item or {}).get("diff") or ""
        if not _path_is_safe(path):
            return {"decision": "needs_user", "reason": "path_invalid"}
        if not str(diff).strip():
            # 后台给没改动的结构页附带空差异；但列进 fileDiffs 的条目必须
            # 有内容，空差异说明审阅材料不完整，交给人看。
            return {"decision": "needs_user", "reason": "empty_diff"}
        if path.startswith("wiki/sources/"):
            # 来源页由导入独占（schema.md），模型改它一定有问题。
            return {"decision": "needs_user", "reason": "source_page_modified"}
        if diff_has_changes(diff):
            changed_paths.add(path)
            existed = item.get("existed_before")
            if existed is None:
                existed = not _is_new_file(diff)
            if existed:
                existing_changed += 1

    # ---- 规则 2：10 个及以上"既有页面"变更交给用户 ------------------------
    if existing_changed >= NEEDS_USER_EXISTING_PAGES or int(counts.get("modified") or 0) >= NEEDS_USER_EXISTING_PAGES:
        return {"decision": "needs_user", "reason": "existing_pages_changed_limit"}

    # ---- 规则 3：affectedPaths 必须等于真正有改动的路径 -------------------
    affected = {str(p) for p in (pending.get("affectedPaths") or [])}
    if affected != changed_paths:
        return {"decision": "needs_user", "reason": "affected_paths_mismatch"}

    # ---- 规则 4/5：来源引用一条不能少、新增引用必须已登记 -----------------
    for item in diffs:
        before_refs, after_refs = _refs_by_side((item or {}).get("diff") or "")
        if not before_refs <= after_refs:
            return {"decision": "reject",
                    "reason": "dropped_existing_source_refs"}
        unknown = after_refs - before_refs - known
        if unknown:
            return {"decision": "reject", "reason": "unknown_source_refs"}

    return {"decision": "confirm", "reason": "ok"}
