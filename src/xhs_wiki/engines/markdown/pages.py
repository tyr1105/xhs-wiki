"""轻量引擎的页面生成：来源页、主题页辅助、索引页、暂缓页。

这个文件在整体架构（docs/architecture.md §6.1）里负责"由代码生成、不调用模型"的
那部分页面。所有页面都是纯 Markdown，放在用户的知识库目录里，用户可以直接用
Obsidian 或任何编辑器打开。

设计要点：
- frontmatter（文件开头两行 --- 之间的元信息）用手写的简单格式生成，不引入 YAML
  依赖；字符串值用 JSON 的双引号写法，这同时也是合法的 YAML；
- 所有写文件都走 atomic_write：先写临时文件再 os.replace，读者永远不会看到
  写了一半的页面。
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

# 文件名里不允许出现的字符（macOS 主要是 / 和 :，再加上 Windows 也不允许的一批，
# 保证知识库目录拷到别的系统也不出问题）
_FILENAME_FORBIDDEN = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def sanitize_for_filename(text: str, max_chars: int = 20) -> str:
    """把任意文本清理成可以用作文件名的形式。

    去掉文件名里不允许的字符和首尾空白、点号；再截到 max_chars 个字。
    清理后为空就返回"无标题"，保证文件名永远非空。
    """

    cleaned = _FILENAME_FORBIDDEN.sub("", str(text))
    cleaned = cleaned.strip().strip(".").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:max_chars] if cleaned else "无标题"


def atomic_write(path: Path, text: str) -> None:
    """原子写文件：先写同目录临时文件，再 os.replace 覆盖。

    os.replace 在同一文件系统上是原子的，所以任何时刻读到的都是完整的一个版本。

    页面按私有文件写（权限 0600，只有本人能读）。
    以前用默认权限（通常 0644），知识库放在用户已有的、别人能进的目录时，
    索引页里的主题名、最近收藏标题、暂缓原因，同机其他账户都能读到。
    tempfile.mkstemp 创建的临时文件天生就是 0600，名字也不会和别的写入
    撞车；写入中途出错时删掉临时文件，原样抛出异常。
    缺的上级目录按私有权限新建（0700），已存在的目录不改权限。
    """

    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".",
                                        suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# 来源页
# ---------------------------------------------------------------------------

def _yaml_value(value: str) -> str:
    """frontmatter 里的字符串值：用 JSON 双引号写法（也是合法 YAML）。"""

    import json

    return json.dumps(str(value), ensure_ascii=False)


def source_filename(
    title: str,
    published_at: str | None,
    ingest_date: str,
    note_id: str,
) -> str:
    """来源页的文件名（不含目录）：<日期>-<标题前20字>-<note_id前6位>.md。

    日期优先用发布时间里的日期部分；发布时间缺失或解析不出日期时，
    用入库日期（调用方传 ingest_date，格式 YYYY-MM-DD）。
    """

    date = ""
    if published_at:
        # 发布时间可能是 "2024-05-01" 或 "2024-05-01T08:00:00"，取前 10 位日期
        m = re.match(r"(\d{4}-\d{2}-\d{2})", str(published_at))
        if m:
            date = m.group(1)
    if not date:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", ingest_date)
        date = m.group(1) if m else "0000-00-00"
    stem = f"{date}-{sanitize_for_filename(title, 20)}-{str(note_id)[:6]}"
    return stem + ".md"


def render_source_page(
    *,
    note_id: str,
    title: str,
    url: str,
    author: str,
    published_at: str | None,
    status: str,
    topics: list[str],
    gist: str,
    key_points: list[str],
    details: list[str],
    scenarios: list[str],
    raw_text: str,
) -> str:
    """渲染来源页全文。

    结构：frontmatter → 标题 → 概要/要点/细节/场景 → "原文"（用 <details> 折叠）。
    原文部分放解析包的全文（正文、转写、OCR 都在里面）；用 HTML <details> 包起来，
    Obsidian 和 GitHub 都能正确折叠显示，页面平时只露摘要。
    """

    lines: list[str] = []
    lines.append("---")
    lines.append(f"note_id: {_yaml_value(note_id)}")
    lines.append(f"url: {_yaml_value(url)}")
    lines.append(f"author: {_yaml_value(author)}")
    lines.append(f"published_at: {_yaml_value(published_at or '')}")
    # topics 用 JSON 数组写法：空是 []，回填后是 ["主题A"]，都是合法 YAML
    import json as _json

    lines.append("topics: " + _json.dumps(topics, ensure_ascii=False))
    lines.append(f"status: {status}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title or '无标题'}")
    lines.append("")
    lines.append(f"**概要**：{gist}")
    lines.append("")
    if key_points:
        lines.append("## 要点")
        lines.append("")
        for p in key_points:
            lines.append(f"- {p}")
        lines.append("")
    if details:
        lines.append("## 细节")
        lines.append("")
        for d in details:
            lines.append(f"- {d}")
        lines.append("")
    if scenarios:
        lines.append("## 适用场景")
        lines.append("")
        for s in scenarios:
            lines.append(f"- {s}")
        lines.append("")
    lines.append("## 原文")
    lines.append("")
    lines.append("<details>")
    lines.append("<summary>展开查看原文（正文、语音转写、图片文字识别）</summary>")
    lines.append("")
    lines.append(raw_text.rstrip())
    lines.append("")
    lines.append("</details>")
    lines.append("")
    return "\n".join(lines)


def replace_frontmatter_topics(text: str, topics: list[str]) -> str:
    """把来源页 frontmatter 里的 topics 行替换成新值，其余内容（含原文）原样保留。

    主题归类完成后回填用。只动第一个 --- 块里的 topics: 那一行。
    """

    import json as _json

    lines = text.split("\n")
    # frontmatter 必须是第一行 --- 开始；找到闭合 --- 为止
    if not lines or lines[0].strip() != "---":
        raise ValueError("frontmatter_missing")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise ValueError("frontmatter_missing")
    new_line = "topics: " + _json.dumps(topics, ensure_ascii=False)
    for i in range(1, end):
        if lines[i].startswith("topics:"):
            lines[i] = new_line
            return "\n".join(lines)
    # 没有 topics 行就插到闭合 --- 之前
    lines.insert(end, new_line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主题页辅助
# ---------------------------------------------------------------------------

def extract_section(markdown: str, name: str) -> str | None:
    """取出 Markdown 里 "## <name>" 一节的内容（到下一个 ## 或文末）。

    主题页重写时用来读旧页面的"概述"一节，作为参考传给模型。
    没有这一节就返回 None。
    """

    lines = markdown.split("\n")
    collecting = False
    buf: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if collecting:
                break
            collecting = line[3:].strip() == name
            continue
        if collecting:
            buf.append(line)
    text = "\n".join(buf).strip()
    return text or None


# ---------------------------------------------------------------------------
# 索引页 / 暂缓页
# ---------------------------------------------------------------------------

def render_index(
    *,
    groups: list[tuple[str, list[dict]]],
    recent: list[dict],
    deferred_count: int,
) -> str:
    """渲染索引页。

    groups 是 [(类别, [主题项])]，类别已按主题数从多到少排好；
    主题项形如 {"name", "description", "count"}；
    recent 是最近新增的来源页，形如 {"file_stem", "title"}。
    """

    lines: list[str] = []
    lines.append("# 索引")
    lines.append("")
    if groups:
        for category, topics in groups:
            lines.append(f"## {category}")
            lines.append("")
            for t in topics:
                lines.append(
                    f"- [[主题/{t['name']}]]：{t['description']}（{t['count']} 条）"
                )
            lines.append("")
    else:
        lines.append("（还没有主题）")
        lines.append("")
    lines.append("## 最近新增")
    lines.append("")
    if recent:
        for r in recent:
            lines.append(f"- [[来源/{r['file_stem']}]]：{r['title'] or '无标题'}")
    else:
        lines.append("暂无")
    lines.append("")
    lines.append("## 暂缓")
    lines.append("")
    if deferred_count:
        lines.append(f"内容不足而暂缓整理的收藏共 {deferred_count} 条，详见 [[暂缓]]。")
    else:
        lines.append("暂无。")
    lines.append("")
    return "\n".join(lines)


def render_deferred_page(entries: list[dict]) -> str:
    """渲染暂缓页：列出所有暂缓的来源页和原因。entries 形如 {"file_stem","title","reason"}。"""

    lines: list[str] = []
    lines.append("# 暂缓")
    lines.append("")
    lines.append("以下收藏因内容不足暂缓整理（只保留来源页，不并入主题）：")
    lines.append("")
    if entries:
        for e in entries:
            reason = e.get("reason") or "内容不足"
            lines.append(f"- [[来源/{e['file_stem']}]]：{e['title'] or '无标题'}——{reason}")
    else:
        lines.append("暂无。")
    lines.append("")
    return "\n".join(lines)
