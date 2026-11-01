"""轻量引擎（markdown）：把解析包整理成 Markdown 知识库。

对外只暴露 MarkdownEngine 和几个数据类型，引擎目录下的其他文件
（store / pages / prompts）是实现细节。
"""

from .engine import (
    ASSIGN_BATCH_SIZE,
    ASSIGN_PROMPT_LIMIT,
    CARD_PROMPT_LIMIT,
    PACKAGE_CHAR_LIMIT,
    TOPIC_CANDIDATE_LIMIT,
    TOPIC_MEMBER_LIMIT,
    TOPIC_PROMPT_LIMIT,
    DistillResult,
    IngestResult,
    MarkdownEngine,
    Package,
    Provider,
)
from .store import Store

__all__ = [
    "MarkdownEngine",
    "Package",
    "Provider",
    "IngestResult",
    "DistillResult",
    "Store",
    "PACKAGE_CHAR_LIMIT",
    "CARD_PROMPT_LIMIT",
    "ASSIGN_PROMPT_LIMIT",
    "TOPIC_PROMPT_LIMIT",
    "ASSIGN_BATCH_SIZE",
    "TOPIC_CANDIDATE_LIMIT",
    "TOPIC_MEMBER_LIMIT",
]
