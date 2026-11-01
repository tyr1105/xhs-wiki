# -*- coding: utf-8 -*-
"""engines/base.py —— 所有引擎共用的类型。

这个文件做什么：把"引擎"对外的接口契约集中到一个地方——
- ``Engine`` 协议：runner（编排器）只认这个形状，不关心具体引擎的实现；
- ``Package`` / ``IngestResult`` / ``DistillResult``：ingest / distill 两个
  环节的输入输出数据类型；
- ``package_from_dict``：把解析包 dict（parse/package.py 产出的
  package.json 形状）转换成引擎需要的 ``Package``。

在整体里的位置：docs/architecture.md §6。这些类型原来声明在轻量引擎
（engines/markdown/engine.py）里，后来移到这里成为共用类型；markdown
引擎改为从本模块导入，因此"从 markdown 引擎导入这些类型"的老路径仍然
可用（见 markdown/engine.py 顶部的 re-export）。

术语说明（给非程序员）：
- "解析包"（package）：一条收藏下载后由解析阶段整理出的结构化数据，
  package.json 是机器读的，package.md 是给模型读的纯文本版本。
- "协议"（Protocol）：Python 的一种接口约定——只要一个类的方法签名
  长成这样，就算实现了协议，不需要显式继承。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class Provider(Protocol):
    """模型提供方协议：给系统提示词和用户提示词，返回模型输出的文本。

    与 docs/architecture.md §8 和 llm/base.py 里的 Provider 一致；这里
    自行声明（而不是 import llm 包）是为了保持引擎层和模型调用层只在
    runner 里汇合的依赖方向（docs/architecture.md §1 的模块规则）。
    """

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_output_tokens: int,
        timeout_s: float = 300,
    ) -> str: ...


@dataclass
class Package:
    """一条收藏的解析包里引擎需要的字段。

    parse/package.py 产出 package.json / package.md；引擎只消费
    这里列的这几项。package_md 是给模型读的纯文本版本（标题、作者、
    正文、标签、转写、OCR）。list_position 是收藏列表位置，越小越新。
    """

    note_id: str
    title: str
    url: str
    author: str
    published_at: str | None
    list_position: int  # 收藏列表位置，越小越新
    package_md: str


@dataclass
class IngestResult:
    """ingest 单条结果：status ∈ ok / deferred / failed。"""

    note_id: str
    status: str
    source_file: str | None = None
    insufficient_reason: str | None = None
    error: str | None = None
    engine_ref: dict = field(default_factory=dict)


@dataclass
class DistillResult:
    """distill 的整体结果。

    - status：ok（全部成功）| partial（部分失败）；
    - errors：每条是 {"code", "detail"}；
    - per_note：逐条说明这次整理的结果，键是 note_id，值是
      {"status": "ok"|"failed", "code": str|None}。runner 优先按它逐条
      推进台账（哪些笔记到 distilled、哪些记失败），没有它的引擎才走
      "按整体状态一刀切"的兜底路径。
    """

    status: str  # ok（全部成功）| partial（部分失败）
    assigned: int = 0
    new_topics: list[str] = field(default_factory=list)
    pages_rewritten: int = 0
    errors: list[dict] = field(default_factory=list)
    per_note: dict[str, dict] = field(default_factory=dict)


class Engine(Protocol):
    """引擎协议（docs/architecture.md §6）。

    runner 和 cleanup 只认这个形状；具体引擎（markdown / 未来的 llmwiki）
    实现这些方法即可，不需要显式继承。
    """

    name: str

    def health(self) -> tuple[bool, str]:
        """引擎是否可用及原因（不调用模型、不花钱）。"""
        ...

    def ingest(self, packages: list[Package]) -> list[IngestResult]:
        """把解析包入库（每条：ok / deferred / failed + engine_ref）。"""
        ...

    def distill(self, note_ids: list[str]) -> DistillResult:
        """把新入库的收藏整合进主题，逐条结果放在返回值的 per_note 里。"""
        ...

    def skill_context(self) -> dict:
        """Claude Skill 需要的路径和说明。"""
        ...

    def source_exists(self, note_id: str, engine_ref: dict) -> bool:
        """这条笔记的来源是否确实已入引擎（清理原件前的第 1 道核对）。

        engine_ref 是 ingest 时写进台账的引擎引用（JSON 解析后的 dict，
        轻量引擎里是 {"source": 来源页文件名}）。
        """
        ...


def package_from_dict(pkg: dict, list_position: int) -> Package:
    """把解析包 dict 转成引擎需要的 Package（输入是 parse/package.py 里 load_package 的返回形状）。

    输入形状：
    {"note_id", "title", "url", "author", "published_at", "package_md",
     "package_sha256", "coverage", "media"}。其中引擎只用前六个字段；
    package_sha256 / coverage / media 是 runner 和清理环节用的，这里不碰。

    list_position 由调用方（runner）从台账传入——解析包本身不记收藏位置。
    缺 note_id 或 package_md 说明数据不完整（编程错误或磁盘损坏），
    直接抛 ValueError 让调用方按失败处理，不猜测。
    """

    if not isinstance(pkg, dict):
        raise ValueError("package_not_dict")
    note_id = pkg.get("note_id")
    package_md = pkg.get("package_md")
    if not isinstance(note_id, str) or not note_id:
        raise ValueError("package_missing_note_id")
    if not isinstance(package_md, str):
        raise ValueError("package_missing_md")
    published = pkg.get("published_at")
    return Package(
        note_id=note_id,
        title=str(pkg.get("title") or ""),
        url=str(pkg.get("url") or ""),
        author=str(pkg.get("author") or ""),
        published_at=str(published) if published else None,
        list_position=int(list_position),
        package_md=package_md,
    )
