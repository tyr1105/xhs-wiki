# -*- coding: utf-8 -*-
"""engines/llmwiki_app/ —— 对接 nashsu/llm_wiki 桌面应用的 LLM Wiki 引擎。

这个包做什么：从 v0.4.0 起，「LLM Wiki 引擎」指对接 nashsu/llm_wiki
（GPL-3.0 桌面应用）的引擎。xhs-wiki 和应用之间**只通过文件交接**：
- 写：把解析包写成知识库项目里的 ``raw/sources/xhs/<note_id>.md``，
  应用（在运行、打开这个项目、Source Watch 自动整理开着时）会自动
  把它整理成 ``wiki/`` 下的主题页和来源页；
- 读：只读 ``.llm-wiki/ingest-cache.json``（整理完成记录）和
  ``.llm-wiki/ingest-queue.json``（任务队列）来判断每条整理的结果，
  以及只读 ``~/Library/Application Support/com.llmwiki.app/app-state.json``
  来判断应用当前打开的项目和整理用的提供方。

子模块：
- ``project``：知识库项目目录与两个记录文件的读写（纯文件操作）；
- ``app``：应用状态（安装位置、版本、全局设置文件）与启动；
- ``engine``：实现 engines/base.py 的 Engine 协议（health / ingest /
  distill / skill_context / source_exists），外加运行前准备 prepare()。

红线：绝不写应用的任何配置和记录文件（记录文件只读）；密钥只判断
是否为空，绝不记录、打印或返回它的内容。
"""

from .engine import EngineNeedsUser, LlmWikiAppEngine

__all__ = ["EngineNeedsUser", "LlmWikiAppEngine"]
