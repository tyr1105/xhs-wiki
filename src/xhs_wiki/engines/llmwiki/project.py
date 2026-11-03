# -*- coding: utf-8 -*-
"""engines/llmwiki/project.py —— 知识库脚手架与打开项目。

这个文件做什么：
- scaffold(project_root)：在空目录（或不存在的目录）里搭出 LLM Wiki
  认得的最小知识库结构，然后 git init + 首次提交；
- open_project(client, project_root)：通过接口打开知识库，拿到 projectId。

默认内容与 LLM Wiki 后端创建项目时一致：
- purpose.md / schema.md：通用模板中的知识库用途和页面结构说明；
- wiki/index.md / wiki/log.md / wiki/overview.md：初始索引、日志和概览；
- .app/project.json：项目标识文件，projectId 使用新生成的 uuid。

目录结构取最小集合，只创建 raw/sources、wiki、wiki/sources、
.app/tasks、exports、skills；其余子目录留给后台第一次整理时自建。

在整体里的位置：用户第一次选择 LLM Wiki 引擎时由向导调 scaffold；
之后每次连接（后台重启后 projectId 可能变化）都要 open_project。
知识库目录里只放 LLM Wiki 自己的东西——我们的请求日志、状态、导入包
全在 APP_HOME（见 journal.py / config.py），绝不往知识库里塞私货。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

# ---- 保留后台创建项目所需的通用默认内容（用途见文件头说明）------------
#
# 第三方内容声明：下面的 PURPOSE_MD、SCHEMA_MD，以及 _starter_index、
# _starter_log、_starter_overview 生成的文字，逐字取自上游项目
# StoneLL1/llm-wiki-desktop（提交 749e213）的
# src-tauri/templates/projects/general/{purpose,schema}.md 和
# src-tauri/src/services/project_service.rs 里的 starter 文本。
# Copyright 2026 StoneLL1 and the LLM Wiki Desktop contributors，
# 按 Apache License 2.0 使用，许可证全文见仓库的 LICENSES/Apache-2.0.txt，
# 说明见 THIRD_PARTY_NOTICES.md。为了让 LLM Wiki 后端识别，这些文字未做修改。

# 通用用途模板全文：写入 purpose.md，供用户说明知识库目标与范围。
PURPOSE_MD = """# Purpose

> Describe why this knowledge base exists and what questions it should answer.
> Edit this file freely — it guides how the compiler organizes your wiki.

## Goal

A general-purpose local knowledge base that turns scattered sources into a linked, explorable Markdown wiki.

## Key Questions

- What are the core concepts I keep returning to?
- Which sources inform each idea?
- How do topics connect to each other?

## Scope

Capture anything worth remembering: articles, notes, documents, and links. Keep pages short and cross-linked rather than long and isolated.

## Direction

Prioritize clarity and discoverability. Use the graph to surface relationships and the index to navigate.
"""

# 通用结构模板全文：写入 schema.md，说明页面类型与组织方式。
SCHEMA_MD = """# Schema

> Defines the page types and structure rules for this wiki.
> Templates only affect `purpose.md` and `schema.md` — not the folder layout.

## Page Types

| Type | Folder | Purpose |
|---|---|---|
| entity | `wiki/entities/` | Named people, organizations, products, or things |
| concept | `wiki/concepts/` | Ideas, definitions, and mental models |
| source | `wiki/sources/` | A single imported reference (article, doc, page) — the verbatim imported original (import-owned) |
| query | `wiki/queries/` | Saved questions and their cited answers |
| synthesis | `wiki/synthesis/` | Multi-source summaries around a theme |
| comparison | `wiki/comparisons/` | Side-by-side analysis of two or more subjects |

## Page Rules

- Each page is a Markdown file with optional YAML frontmatter (`type`, `tags`, `source`).
- Link related pages with `[[wikilinks]]`.
- Keep one topic per page.
- `wiki/index.md` lists the most important entry points.
- `wiki/overview.md` summarizes the whole knowledge base.
- `wiki/sources/` holds verbatim imported originals and is import-owned: derived pages (`entities`, `concepts`, `synthesis`, `comparisons`) cite these sources via frontmatter `sources: ["<filename>"]` and a `> Sources:` line, but never create, modify, or summarize a page under `wiki/sources/`.
"""


def _starter_index(name: str) -> str:
    """生成初始索引，作为后台持续更新的知识库导航入口。"""

    return (
        f"# {name}\n\n"
        "> Wiki index. This is the navigation entry point the compiler keeps up to date.\n\n"
        "No pages yet. Import sources and run a compile to populate the wiki.\n"
    )


def _starter_log(name: str) -> str:
    """生成初始日志，记录项目创建并预留后续追加的操作历史。"""

    return f"# Log\n\n> Append-only operation history for {name}.\n\n- Project initialized.\n"


def _starter_overview(name: str) -> str:
    """生成初始概览，明确项目尚无整理内容，之后由后台更新。"""

    return (
        f"# Overview\n\n"
        f"> Global summary of the {name} knowledge base. The compiler refreshes this as the wiki grows.\n\n"
        "This project has no compiled content yet.\n"
    )


# 最小目录集合（Rust ensure_skeleton 的子集）。
# wiki/sources 是后台导入来源页的位置，查询 Skill 的关键词搜索指向它
# （见 engine.skill_context）。先建好，第一次导入之前 Skill 指的目录也
# 存在；后台自己新建项目时同样会建这个目录。空目录不进 git，首次提交
# 的内容不变。
_SCAFFOLD_DIRS = (
    "raw/sources",
    "wiki",
    "wiki/sources",
    ".app/tasks",
    "exports",
    "skills",
)

# git 首次提交的作者：固定的虚构值，只通过 -c 传给这一次 commit，
# 不写任何全局/仓库级配置。
_GIT_AUTHOR_NAME = "xhs-wiki"
_GIT_AUTHOR_EMAIL = "xhs-wiki@localhost"
# 首次提交说明与后台创建项目时保持一致，便于识别初始化记录。
_GIT_INIT_MESSAGE = "Initial wiki project"


def _atomic_write(path: Path, content: str, *, mode: int = 0o644) -> None:
    """先写临时文件再 os.replace 的原子写（CONTRIBUTING.md 的代码规范）。"""

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        # 中途失败要清掉临时文件，不留垃圾。
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """在这个仓库里跑一次 git（参数列表方式，禁止 shell=True）。"""

    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _is_our_scaffold(root: Path) -> bool:
    """判断目录是不是我们 scaffold 搭出来的：以 .app/project.json 为记号
    （后台也从这个文件读取稳定的 projectId，避免重复初始化）。"""

    return (root / ".app" / "project.json").is_file()


def scaffold(project_root) -> str:
    """在空目录（或不存在的目录）里搭出最小知识库，返回 projectId。

    - 目录非空且没有 .app/project.json 记号 → ValueError
      ("project_root_not_empty")，绝不覆盖用户数据；
    - 已是我们搭的知识库 → 幂等返回，补齐缺失文件，不动 git 历史；
    - git init + 首次提交用虚构作者，只作用于这个仓库
      （-c user.name/-c user.email 优先级高于全局配置，且不落盘）。
    """

    root = Path(project_root)
    if root.exists():
        if not root.is_dir():
            raise ValueError("project_root_not_directory")
        if any(root.iterdir()) and not _is_our_scaffold(root):
            raise ValueError("project_root_not_empty")
    else:
        root.mkdir(parents=True)
    os.chmod(root, 0o700)

    if _is_our_scaffold(root):
        # 我们自己搭的：直接复用原 projectId（后台按它稳定识别项目）。
        identity = json.loads((root / ".app" / "project.json").read_text())
        return identity["projectId"]

    name = root.name or "Project"  # 与 Rust open_project 取目录名的做法一致
    project_id = str(uuid.uuid4())

    for directory in _SCAFFOLD_DIRS:
        (root / directory).mkdir(parents=True, exist_ok=True)

    # 文件内容照抄 Rust create_project（出处见各常量的注释）。
    _atomic_write(root / "purpose.md", PURPOSE_MD)
    _atomic_write(root / "schema.md", SCHEMA_MD)
    _atomic_write(root / "wiki" / "index.md", _starter_index(name))
    _atomic_write(root / "wiki" / "log.md", _starter_log(name))
    _atomic_write(root / "wiki" / "overview.md", _starter_overview(name))
    _atomic_write(
        root / ".app" / "project.json",
        json.dumps({"projectId": project_id}, indent=2) + "\n",
        mode=0o600,  # .app 下是后台的私有状态，收紧权限
    )

    # git init + 首次提交（整理前工作区必须干净，起点就是一个干净提交）。
    _git(root, "init", "--quiet")
    _git(root, "add", "-A")
    _git(
        root,
        "-c", f"user.name={_GIT_AUTHOR_NAME}",
        "-c", f"user.email={_GIT_AUTHOR_EMAIL}",
        "-c", "commit.gpgsign=false",  # 用户全局可能开了签名，这里不需要
        "commit", "--quiet", "-m", _GIT_INIT_MESSAGE,
    )
    return project_id


def open_project(client, project_root, *, timeout: float = 900) -> str:
    """通过接口打开知识库，返回 projectId。

    每次连接都要重新打开（后台重启后 projectId 可能变化）；
    第一次打开可能要重建索引，超时放宽到 15 分钟（900 秒）。
    POST /projects/open 带一个
    新的 uuid 幂等键。
    """

    root = Path(project_root)
    status, payload = client.request(
        "POST",
        "/projects/open",
        {"path": str(root.resolve())},
        str(uuid.uuid4()),
        timeout=timeout,
    )
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    project_id = data.get("projectId")
    if status != 200 or not project_id:
        raise ValueError("project_open_failed")
    return project_id
