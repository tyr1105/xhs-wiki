# -*- coding: utf-8 -*-
"""project.py —— nashsu/llm_wiki 知识库项目的目录结构与记录文件（只读记录）。

这个文件做什么：集中所有跟"知识库项目目录"打交道的纯文件操作——
来源文件的路径与写入、项目结构检查、两个记录文件（ingest-cache /
ingest-queue）的读取、以及给控制台统计用的页面计数。engine.py 只
调用这里的方法，不自己拼路径，保证路径口径全项目只有一份。

在整体里的位置：engines/llmwiki_app 包的地基（docs/architecture.md
§6 引擎层）。被同包的 engine.py、daemon.py 的知识库统计调用。

术语说明（给非程序员）：
- "来源文件"：xhs-wiki 写进项目 ``raw/sources/xhs/`` 的一条笔记的
  Markdown 文件；LLM Wiki 应用看到新文件后会自动整理它。
- "ingest-cache"（整理缓存）：应用写的 JSON 文件，记录每个来源文件
  整理完成后的内容指纹（sha256）和写入的页面清单。
- "ingest-queue"（整理队列）：应用写的 JSON 数组，记录排队、进行中
  和失败的整理任务；成功的任务会被移出数组。
- "frontmatter"：Markdown 文件开头用 ``---`` 包起来的一段元数据
  （标题、作者等），格式通常用 YAML。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from ..base import Package

# xhs-wiki 在项目里放来源文件的子目录（相对项目根）。
SOURCES_DIR_REL = Path("raw/sources/xhs")
# cache 文件的键是相对 raw/sources/ 的路径，前缀固定是 xhs/。
SOURCE_REL_PREFIX = "xhs"
# 两个记录文件（相对项目根）。
CACHE_REL = Path(".llm-wiki/ingest-cache.json")
QUEUE_REL = Path(".llm-wiki/ingest-queue.json")

# nashsu 布局：一个合法的 LLM Wiki 项目根目录下必须有这三样
#（purpose.md 和 schema.md 是项目的"章程"，.llm-wiki/ 放记录文件）。
PROJECT_REQUIRED = ("purpose.md", "schema.md", ".llm-wiki")

# nashsu 的 wiki 目录布局（skill_context 和页面统计用）。
WIKI_INDEX = "wiki/index.md"
WIKI_SOURCES_DIR = "wiki/sources"
# 主题（概念）分布在这几个目录里，顺序即重要程度。
WIKI_TOPIC_DIRS = (
    "wiki/concepts",
    "wiki/entities",
    "wiki/synthesis",
    "wiki/comparisons",
    "wiki/queries",
)


def sha256_bytes(data: bytes) -> str:
    """一段字节的 SHA-256 十六进制指纹（和应用的 hash 口径一致）。"""
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str | None:
    """一个文件的 SHA-256；文件不存在或读不了返回 None。"""

    try:
        return sha256_bytes(Path(path).read_bytes())
    except OSError:
        return None


def source_rel(note_id: str) -> str:
    """cache 里这条笔记的键：相对 raw/sources/ 的路径。"""

    return f"{SOURCE_REL_PREFIX}/{note_id}.md"


def source_path(kb_root: Path, note_id: str) -> Path:
    """这条笔记的来源文件在项目里的绝对路径。"""

    return Path(kb_root) / SOURCES_DIR_REL / f"{note_id}.md"


def render_source(pkg: Package) -> str:
    """把解析包渲染成来源 Markdown 文件全文。

    结构：YAML frontmatter（title / author / published_at / url /
    note_id / source）+ 一个空行 + package_md 原文。

    字符串值一律用 ``json.dumps`` 转成双引号形式再写——JSON 的双引号
    字符串同时也是合法的 YAML 字符串，标题里的冒号、引号、井号、
    换行都会被正确转义，不会破坏 frontmatter 的结构。这是本引擎最
    容易出错的地方，所以集中在这一个函数里做。
    published_at 为空（None 或空串）时整行不写，避免出现 "published_at:"
    后面跟空值的歧义。
    """

    def q(value) -> str:
        return json.dumps(str(value), ensure_ascii=False)

    lines = ["---"]
    lines.append(f"title: {q(pkg.title)}")
    lines.append(f"author: {q(pkg.author)}")
    if pkg.published_at:
        lines.append(f"published_at: {q(pkg.published_at)}")
    lines.append(f"url: {q(pkg.url)}")
    lines.append(f"note_id: {q(pkg.note_id)}")
    lines.append(f'source: {q("小红书收藏")}')
    lines.append("---")
    return "\n".join(lines) + "\n\n" + pkg.package_md


def write_source_atomic(path: Path, content: str) -> None:
    """原子写来源文件：先写同目录的点开头临时文件再 ``os.replace``。

    为什么要原子写：LLM Wiki 的 Source Watch 在盯着 raw/sources/ 目录，
    直接写正式文件可能让应用读到写了一半的内容（半个 frontmatter），
    整理出错误结果。临时文件名以 ``.`` 开头且后缀不是 .md，应用按
    扩展名过滤来源文件时不会把它收进队列（0.6.13 的样例里来源都是
    .md 文件；这一判断也记录在 .dev/progress/N01.md）。
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent,
                                    prefix="." + path.name + ".",
                                    suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def project_layout_ok(kb_root: Path) -> bool:
    """这个目录是不是一个 LLM Wiki 项目（purpose.md / schema.md /
    .llm-wiki/ 三样齐全）。只看存在性，不读内容。"""

    root = Path(kb_root)
    return all((root / name).exists() for name in PROJECT_REQUIRED)


def read_ingest_cache(kb_root: Path):
    """读 ingest-cache.json。返回 entries 字典；文件不存在返回空字典
   （项目刚建好还没整理过任何东西是正常情况）；JSON 损坏或形状不认识
    （没有 entries 键或 entries 不是字典）返回 None 表示"本轮无法判断"。

    只读：xhs-wiki 绝不写应用的记录文件。"""

    path = Path(kb_root) / CACHE_REL
    if not path.exists():
        return {}  # 项目还没整理过任何东西：正常，等于空记录
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else None


def read_ingest_queue(kb_root: Path):
    """读 ingest-queue.json。返回任务列表（可能是空列表）；文件不存在
    返回空列表；JSON 损坏或不是数组返回 None 表示"本轮无法判断"。

    只读。每项的形状见样例 .dev/fixtures/nashsu-0.6.13/：sourcePath
    相对项目根（如 ``raw/sources/xhs/x.md``）、status（pending /
    processing / done / failed / cancelled）、error、retryCount。"""

    path = Path(kb_root) / QUEUE_REL
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [] if not path.exists() else None
    return data if isinstance(data, list) else None


def cache_entry_complete(kb_root: Path, entry, current_hash: str | None) -> bool:
    """cache 里的一条记录能不能证明"这条来源已整理完成且结果还在"。

    两个条件（PRD §2 已整理内容跳过的判定）：
    1. 记录里的 hash 等于来源文件**当前**内容的指纹——指纹一致且页面
       还在，应用就不会重复整理；
    2. filesWritten 列出的每个页面文件都还存在（页面被用户删掉的
       话，这条来源要留给应用重新整理，不能算完成）。
    """

    if not isinstance(entry, dict) or current_hash is None:
        return False
    if entry.get("hash") != current_hash:
        return False
    files = entry.get("filesWritten")
    if not isinstance(files, list):
        return False
    root = Path(kb_root)
    return all(isinstance(f, str) and (root / f).exists() for f in files)


def page_counts(kb_root: Path) -> dict:
    """数 nashsu 布局下的"主题数"和"来源数"，给控制台统计用。

    口径：来源 = ``wiki/sources/`` 下的 .md 文件数；主题 = 五个主题
    目录（concepts / entities / synthesis / comparisons / queries）
    下的 .md 文件数之和——nashsu 没有"一个主题一页"的概念，页面
    分散在这几个目录里，求和是最接近直觉的粗算。
    """

    root = Path(kb_root)
    sources = len(list((root / WIKI_SOURCES_DIR).glob("*.md"))) \
        if (root / WIKI_SOURCES_DIR).is_dir() else 0
    topics = 0
    for rel in WIKI_TOPIC_DIRS:
        directory = root / rel
        if directory.is_dir():
            topics += len(list(directory.glob("*.md")))
    return {"topics": topics, "sources": sources}
