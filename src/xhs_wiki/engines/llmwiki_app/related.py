# -*- coding: utf-8 -*-
"""related.py —— 收录每条笔记前，把「可能相关的已有页面」写到 wiki/index.md 开头。

为什么需要它（问题本身）：
LLM Wiki（nashsu/llm_wiki）收录一条来源时，模型只能靠 wiki/index.md 这份目录
知道知识库里已经有哪些页面；它往已有页面的路径写，应用才会把新旧内容合并成一页。
但应用每次只把目录的前后共 3.2 万字放进提示词（中间截掉，源码里的
WIKI_INDEX_PROMPT_MAX_CHARS）。知识库有几百页以后，目录一定放不下，模型看不到
大部分已有页面，就会为同一主题另建近义新页面——实测 50 条新收藏写了 139 个新页面、
并入已有页面 0 个。

怎么解决：
xhs-wiki 每写一条来源文件之前，先在本机按关键词给全部主题页打分，挑出和这条笔记
最相关的几十页，写进目录开头一个带标记的小节。目录开头一定在提示词的保留范围内，
所以无论知识库多大，模型都能看到最该合并的那些页面。完整目录照旧放在后面。

打分方法（不调用模型、不联网、不加依赖）：
- 「词」= 中文按相邻两个字切（二元组，bigram，例如「记忆分层」→ 记忆、忆分、分层），
  英文和数字按单词切；
- 每个词按「越少页面出现越有区分度」加权（IDF，逆文档频率）；
- 页面这边看标题（权重 3）、文件名和标签（权重 2）、正文开头 300 字（权重 1）；
- 笔记这边看标题、标签和正文前 3000 字。
这是粗筛，宁可多给几页候选；最终合不合并由模型决定。

在整体里的位置：engines/llmwiki_app 包内部工具，由 engine.py 的 ingest() 调用。
文件末尾的 duplicate_name_groups（同名重复页检查）由 daemon.py 在控制台状态里调用。
"""

from __future__ import annotations

import math
import os
import re
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path

from . import project

# 目录里的标记，xhs-wiki 只改写这一对标记之间的内容，目录其余部分一字不动。
RELATED_START = "<!-- xhs-wiki:related-start -->"
RELATED_END = "<!-- xhs-wiki:related-end -->"
RELATED_HEADING = "## 与本次收录内容相关的已有页面"
RELATED_NOTE = (
    "> 下列已有页面与正在收录的来源主题相近（收录工具按关键词挑选）。"
    "同一主题请直接写入对应页面的完整路径（例如 `wiki/concepts/xxx.md`），"
    "由应用合并新旧内容；不要新建近义页面。确实没有相近页面时才新建。"
)
# 最多列多少页：30 行约 2 千字，占提示词预算很小，又足够覆盖一条笔记的几个主题。
DEFAULT_LIMIT = 30

# 几乎每页都有、没有区分度的词（来源说明类），打分时忽略。
_STOP = {
    "来源", "作者", "笔记", "视频", "配文", "转写", "核验", "主张", "小红",
    "红书", "博主", "本页", "页面", "相关", "内容", "依据", "原文", "未核",
    "一个", "可以", "这个", "我们", "以及", "进行", "通过", "没有",
}
_FRONT_RE = re.compile(r"^---\n(.*?)\n---\n?", re.S)
_CJK_RUN = re.compile(r"[一-鿿]+")
_WORD = re.compile(r"[a-z0-9]+")


def terms(text: str) -> Counter:
    """把一段文字切成「词」并计数：中文二元组 + 英文/数字单词（小写、至少 2 个字符）。"""
    text = text.lower()
    out: Counter = Counter()
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            continue
        for i in range(len(run) - 1):
            gram = run[i:i + 2]
            if gram not in _STOP:
                out[gram] += 1
    for word in _WORD.findall(text):
        if len(word) >= 2 and not word.isdigit():
            out[word] += 1
    return out


def _front_and_body(text: str) -> tuple[str, str]:
    m = _FRONT_RE.match(text)
    return (m.group(1), text[m.end():]) if m else ("", text)


def _front_field(front: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}:\s*(.*)$", front, re.M)
    return m.group(1).strip().strip("\"'") if m else ""


def _clean_body(body: str, limit: int) -> str:
    """正文去掉标题行、引用行和链接语法后取前 limit 个字（只用来打分）。"""
    lines = [ln for ln in body.split("\n")
             if ln.strip() and not ln.lstrip().startswith(("#", ">", "|", "<!--"))]
    text = " ".join(lines)
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)
    return text[:limit]


def load_catalog(kb_root: Path) -> list[dict]:
    """读出全部主题页（五个主题目录，不含用户本人的记录页），每页：target（目录里的链接写法）、title、加权后的词频。

    读不了的单个页面直接跳过（打分是辅助功能，不能因为一页坏了就拦住收录）。"""
    root = Path(kb_root)
    pages = []
    for rel_dir in project.WIKI_TOPIC_DIRS:
        directory = root / rel_dir
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            front, body = _front_and_body(text)
            # 页首 origin: personal 的是用户本人的记录（日记、项目、教训），
            # 不能让收藏内容合并进去，所以不作为候选。
            if _front_field(front, "origin") == "personal":
                continue
            heading = re.search(r"^#\s+(.+?)\s*$", body, re.M)
            title = _front_field(front, "title") or (heading.group(1) if heading else path.stem)
            weighted: Counter = Counter()
            for term in terms(title):
                weighted[term] += 3
            slug_words = path.stem.replace("-", " ") + " " + _front_field(front, "tags")
            for term in terms(slug_words):
                weighted[term] += 2
            for term in terms(_clean_body(body, 300)):
                weighted[term] += 1
            target = path.relative_to(root / "wiki").with_suffix("").as_posix()
            pages.append({"target": target, "title": title, "terms": weighted})
    return pages


def rank(pages: list[dict], note_text: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """按与笔记的相关度给页面排序，返回得分大于 0 的前 limit 页。

    得分 = Σ（笔记和页面共有的词）IDF × 页面里这个词的权重，再除以页面词量的平方根，
    避免词多的长页面靠「什么都沾一点」排到前面。同分按链接字母序，结果稳定。"""
    if not pages:
        return []
    query = terms(note_text)
    if not query:
        return []
    doc_freq: Counter = Counter()
    for page in pages:
        doc_freq.update(page["terms"].keys())
    total = len(pages)
    scored = []
    for page in pages:
        shared = query.keys() & page["terms"].keys()
        if not shared:
            continue
        score = sum(math.log(1 + total / doc_freq[t]) * page["terms"][t] for t in shared)
        score /= math.sqrt(sum(page["terms"].values()))
        scored.append((-score, page["target"], page))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [page for _, _, page in scored[:limit]]


def note_text(title: str, package_md: str) -> str:
    """笔记这边参与打分的文字：标题重复三遍（加权）+ 解析包正文前 3000 字。"""
    return f"{title} {title} {title} {package_md[:3000]}"


def render_block(pages: list[dict]) -> str:
    """生成标记块全文（含首尾标记）。没有候选时只留一句说明，方便看出「这次没找到」。"""
    lines = [RELATED_START, RELATED_HEADING, "", RELATED_NOTE, ""]
    if pages:
        lines += [f"- [[{p['target']}]] — {p['title']}" for p in pages]
    else:
        lines.append("（本次未找到关键词相近的已有页面）")
    lines.append(RELATED_END)
    return "\n".join(lines)


def replace_block(index_text: str, block: str) -> str:
    """把目录里的标记块换成新块；目录里还没有标记块时，插到第一个一级标题之后
    （没有一级标题就放在头部元数据之后），保证它位于目录开头。"""
    start = index_text.find(RELATED_START)
    end = index_text.find(RELATED_END)
    if start >= 0 and end > start:
        return index_text[:start] + block + index_text[end + len(RELATED_END):]
    heading = re.search(r"^#\s+.*$", index_text, re.M)
    if heading:
        pos = heading.end()
    else:
        front = _FRONT_RE.match(index_text)
        pos = front.end() if front else 0
    return index_text[:pos] + "\n\n" + block + "\n" + index_text[pos:]


def atomic_write_text(path: Path, text: str) -> None:
    """先写同目录临时文件再替换：应用和其他读者不会读到写了一半的文件；保留原文件权限。"""
    path = Path(path)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix=".xhs-wiki-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def protected_pages(kb_root: Path) -> dict[str, str]:
    """用户本人的记录页（页首 origin: personal）的当前全文，键是相对知识库根目录的路径。

    为什么需要：LLM Wiki 收录时会合并「同主题的已有页面」，模型分不清哪些页面是用户自己
    写的（项目、教训、日记）。实测一条讲 DSH 的收藏被合并进了用户的 DSH 项目页，还改写了
    原有段落。所以每条收录前存一份原文，收录后这条写到的个人页面一律恢复原样
    （见 engine.py 的 _restore_protected）。读不了的单个页面跳过。"""
    root = Path(kb_root)
    out: dict[str, str] = {}
    wiki = root / "wiki"
    if not wiki.is_dir():
        return out
    for path in sorted(wiki.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        front, _ = _front_and_body(text)
        if _front_field(front, "origin") == "personal":
            out[path.relative_to(root).as_posix()] = text
    return out


def name_key(stem: str) -> str:
    """页面文件名（不含 .md）的「比对用名字」：只保留文字和数字。

    做法：
    - 先做 NFKC 规范化（Unicode 的「兼容等价」统一），全角问号「？」变成半角「?」，
      全角字母、数字也变成半角；
    - 再统一大小写（casefold，比 lower 更彻底）；
    - 最后去掉标点（P）、符号（S）和空白（Z）这三大类字符。
    这样「某问题.md」和「某问题？.md」、「Claude-Code.md」和「claude code.md」
    得到同一个比对名，而真正不同的名字不会被误判成一样。"""
    text = unicodedata.normalize("NFKC", stem).casefold()
    return "".join(ch for ch in text if unicodedata.category(ch)[0] not in "PSZ")


def duplicate_name_groups(kb_root: Path) -> list[list[str]]:
    """找出主题页里「只差标点、空白或大小写」的同名页面，每组是相对知识库根目录的路径列表。

    为什么需要：LLM Wiki 对含中文的文件名原样使用、不查重。模型偶尔在文件名里多写一个
    全角问号之类的标点，应用就会新建一页，而不是并入已有的同主题页面，知识库里出现
    两页内容重叠的同名页面。实测出现过「某问题.md」和「某问题？.md」并存。

    检查范围：五个主题目录合在一起比（不同目录下同名也算：页面之间的 [[名字]] 链接
    不带目录，同名会让链接指向不明确）。只看文件名、不读页面内容，几百页也是毫秒级，
    控制台每次刷新状态都可以调用。只报告、不自动合并：合并要读懂两页内容，交给用户
    （或用户的 AI 助手）处理。
    返回按路径排序，结果稳定；目录不存在或读不了时当作没有页面。"""
    root = Path(kb_root)
    groups: dict[str, list[str]] = {}
    for rel_dir in project.WIKI_TOPIC_DIRS:
        directory = root / rel_dir
        try:
            paths = sorted(directory.glob("*.md")) if directory.is_dir() else []
        except OSError:
            continue
        for path in paths:
            key = name_key(path.stem)
            if not key:
                # 文件名全是标点（极少见）：没法比，跳过，避免把它们都归成一组。
                continue
            groups.setdefault(key, []).append(path.relative_to(root).as_posix())
    found = [sorted(paths) for paths in groups.values() if len(paths) > 1]
    return sorted(found)


def write_hint(kb_root: Path, title: str, package_md: str,
               limit: int = DEFAULT_LIMIT) -> int:
    """为一条即将收录的笔记更新目录开头的候选页面块，返回列出的页数。

    目录文件不存在（新项目）时什么都不做、返回 0——那时也没有已有页面可合并。
    写法：先写同目录临时文件再替换，应用不会读到半个目录；保留原文件权限。"""
    index_path = Path(kb_root) / project.WIKI_INDEX
    if not index_path.is_file():
        return 0
    pages = rank(load_catalog(kb_root), note_text(title, package_md), limit)
    old = index_path.read_text(encoding="utf-8")
    new = replace_block(old, render_block(pages))
    if new != old:
        atomic_write_text(index_path, new)
    return len(pages)
