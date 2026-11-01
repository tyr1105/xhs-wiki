# -*- coding: utf-8 -*-
"""test_x91_glue.py —— 模块衔接（Codex 默认模型、引擎共用类型）的补充测试。

覆盖三块：
1. Codex 不选模型时用用户的默认模型（make_provider 不再抛
   model_missing，complete 的命令行里不出现 -m）；
2. 共用的引擎类型（engines/base.py）：package_from_dict 的转换和
   校验、Engine 协议包含 source_exists；
3. 老导入路径兼容：从 markdown 引擎导入 Package 等类型仍然可用。

全部用假的子进程 runner / 虚构数据，不访问网络、不调用真实模型。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from xhs_wiki.engines import base as engines_base
from xhs_wiki.engines.markdown import engine as markdown_engine_module
from xhs_wiki.llm import make_provider
from xhs_wiki.llm.base import ProcResult


# ---------------------------------------------------------------------------
# 假的子进程执行器：记录 argv，并模拟 codex 把最终回复写进 -o 文件
# ---------------------------------------------------------------------------


class FakeRunner:
    """记录调用参数；遇到 codex exec 时把预设文本写进 -o 指定的文件。"""

    def __init__(self, output_text="虚构回复", *, returncode=0):
        self.output_text = output_text
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, env=None, input="", timeout_s=None, cwd=None):
        self.calls.append(list(argv))
        out_path = None
        if "-o" in argv:
            out_path = argv[argv.index("-o") + 1]
            Path(out_path).write_text(self.output_text, encoding="utf-8")
        return ProcResult(self.returncode, "", "", False)


# ---------------------------------------------------------------------------
# Codex 默认模型
# ---------------------------------------------------------------------------


def test_make_provider_codex_without_model():
    """model 为空（None / 缺省）时 codex 不再抛 model_missing。"""

    for settings in ({"provider": "codex", "model": None},
                     {"provider": "codex", "model": ""},
                     {"provider": "codex"}):
        provider = make_provider(settings, key_provider=lambda: "虚构key")
        assert provider is not None


def test_make_provider_other_providers_still_require_model():
    """其他提供方不选模型仍然拒绝（只有 codex 走用户的默认模型）。"""

    with pytest.raises(ValueError, match="model_missing"):
        make_provider({"provider": "claude_code", "model": None},
                      key_provider=lambda: "虚构key")
    with pytest.raises(ValueError, match="model_missing"):
        make_provider({"provider": "anthropic", "model": ""},
                      key_provider=lambda: "虚构key")


def _fake_codex_found(monkeypatch):
    """让 codex 的查找函数固定返回虚构路径。

    make_provider 不读设置里的 codex_path（那是可执行文件路径，
    曾能经控制台接口写入）。测试改为替换查找函数，既不依赖本机是否
    装了 codex，也不会真的执行任何程序（runner 是假的）。
    """
    from xhs_wiki.llm import codex as codex_module
    monkeypatch.setattr(codex_module, "find_codex",
                        lambda explicit_path=None: "/tmp/fake-codex")


def test_codex_complete_omits_model_flag_when_empty(monkeypatch):
    """model 为空：命令行里没有 -m，由 Codex 用用户配置的默认模型。"""

    _fake_codex_found(monkeypatch)
    runner = FakeRunner()
    provider = make_provider(
        {"provider": "codex", "model": None},
        key_provider=lambda: "虚构key", runner=runner)
    text = provider.complete(system="虚构系统提示词", prompt="虚构提示词",
                             max_output_tokens=100)
    assert text == "虚构回复"
    argv = runner.calls[0]
    assert argv[0] == "/tmp/fake-codex"
    assert "-m" not in argv
    # 其余参数保持不变（codex exec 的固定参数集）。
    for flag in ("exec", "--skip-git-repo-check", "--ephemeral", "-o"):
        assert flag in argv
    assert argv[-1] == "-"


def test_codex_complete_still_passes_model_when_set(monkeypatch):
    """选了模型：-m 照常传（原有行为不变）。"""

    _fake_codex_found(monkeypatch)
    runner = FakeRunner()
    provider = make_provider(
        {"provider": "codex", "model": "虚构模型"},
        key_provider=lambda: "虚构key", runner=runner)
    provider.complete(system="", prompt="虚构提示词", max_output_tokens=100)
    argv = runner.calls[0]
    assert argv[argv.index("-m") + 1] == "虚构模型"


# ---------------------------------------------------------------------------
# 共用的引擎类型（engines/base.py）
# ---------------------------------------------------------------------------


def fake_package_dict(i: int) -> dict:
    """load_package（parse/package.py）返回形状的虚构解析包 dict。"""

    return {
        "note_id": f"{i:024x}",
        "title": f"虚构标题{i}",
        "url": f"https://www.example.com/explore/{i:024x}",
        "author": f"虚构作者{i}",
        "published_at": f"2026-01-{i + 1:02d}",
        "package_md": f"# 虚构标题{i}\n\n虚构正文",
        "package_sha256": "0" * 64,
        "coverage": "text_only",
        "media": [],
    }


def test_package_from_dict_full():
    pkg = engines_base.package_from_dict(fake_package_dict(1), 7)
    assert isinstance(pkg, engines_base.Package)
    assert pkg.note_id == f"{1:024x}"
    assert pkg.title == "虚构标题1"
    assert pkg.list_position == 7
    assert pkg.package_md.startswith("# 虚构标题")


def test_package_from_dict_missing_optional_fields():
    """缺 author / published_at 等可选字段时给安全默认值。"""

    data = fake_package_dict(2)
    del data["author"]
    data["published_at"] = None
    pkg = engines_base.package_from_dict(data, 0)
    assert pkg.author == ""
    assert pkg.published_at is None


def test_package_from_dict_rejects_broken_data():
    """缺 note_id 或 package_md 的 dict 直接抛 ValueError，不猜测。"""

    with pytest.raises(ValueError):
        engines_base.package_from_dict({"package_md": "x"}, 0)
    with pytest.raises(ValueError):
        engines_base.package_from_dict({"note_id": f"{1:024x}"}, 0)
    with pytest.raises(ValueError):
        engines_base.package_from_dict(["不是", "dict"], 0)


def test_engine_protocol_has_source_exists():
    """Engine 协议包含 source_exists 方法。"""

    assert hasattr(engines_base.Engine, "source_exists")
    assert hasattr(engines_base.Engine, "health")
    assert hasattr(engines_base.Engine, "ingest")
    assert hasattr(engines_base.Engine, "distill")
    assert hasattr(engines_base.Engine, "skill_context")


def test_distill_result_has_per_note():
    """DistillResult 带 per_note 字段，默认是空 dict。"""

    result = engines_base.DistillResult(status="ok")
    assert result.per_note == {}


# ---------------------------------------------------------------------------
# 老导入路径兼容（类型移到 base 后 markdown 引擎仍然导出它们）
# ---------------------------------------------------------------------------


def test_markdown_engine_reexports_types():
    for name in ("Package", "IngestResult", "DistillResult", "Provider",
                 "package_from_dict"):
        assert getattr(markdown_engine_module, name) is getattr(engines_base, name)
