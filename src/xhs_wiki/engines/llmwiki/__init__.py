# -*- coding: utf-8 -*-
"""engines/llmwiki/ —— LLM Wiki 引擎（地基 + 导入/整理）。

这个文件做什么：LLM Wiki 引擎的包入口，把各子模块里对外要用的名字
集中 re-export，方便上层（runner、cleanup、测试）一行 import。

在整体里的位置：docs/architecture.md §6.2。LLM Wiki 是"进阶引擎"——
用户装了 LLM Wiki Desktop 之后，xhs-wiki 把解析包导入它、用它调
Claude Code / Codex 整理成互联的知识库。地基是 backend / client /
journal / project / agents，引擎本体是 importer 导入、compiler 整理、
review 轻量检查和 engine 协议实现。

术语说明（给非程序员）：
- "无窗口后台"：LLM Wiki Desktop 用 --serve-api --headless 参数启动后，
  不显示任何界面，只在 127.0.0.1 上开一个需要令牌的 HTTP 接口。
- "幂等键"（Idempotency-Key）：每个写请求带的唯一编号；同一个键重发，
  后台只会执行一次并返回同一次的结果，防止网络重试导致重复写入。
"""

from .agents import install_wrappers, write_model_config
from .backend import Backend, EngineUnavailable
from .client import ApiError, Client
from .compiler import Compiler, GroupMember, ReviewRequired
from .engine import LlmWikiEngine
from .importer import ApiContext, import_package
from .journal import Journal
from .project import open_project, scaffold
from .review import light_review

__all__ = [
    "ApiContext",
    "ApiError",
    "Backend",
    "Client",
    "Compiler",
    "EngineUnavailable",
    "GroupMember",
    "Journal",
    "LlmWikiEngine",
    "ReviewRequired",
    "import_package",
    "install_wrappers",
    "light_review",
    "open_project",
    "scaffold",
    "write_model_config",
]
