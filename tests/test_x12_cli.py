# -*- coding: utf-8 -*-
"""X12 命令行回归：doctor 的按引擎检查（v0.4.0 的 LLM Wiki 引擎）。

所有目录均为临时目录；doctor 里的 LLM Wiki 引擎用假的替身注入，
不读真实的 /Applications 或 ~/Library，不启动应用、不调用模型。

v0.4.0 起 ``assets install-llmwiki`` 子命令已删除（LLM Wiki 引擎直接
对接桌面应用，不再下载后台程序），相关测试随之删除。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from xhs_wiki import cli
from xhs_wiki.store import ServiceStore


@pytest.fixture
def cli_environment(tmp_path, monkeypatch):
    """隔离应用和知识库，收集输出并禁止真实的命令查找。"""
    home = tmp_path / "generated home"
    monkeypatch.setenv("XHS_WIKI_HOME", str(home))
    store = ServiceStore(home)
    store.update_settings({"kb_root": str(tmp_path / "generated kb")})
    lines = []
    monkeypatch.setattr(cli, "_print", lines.append)
    # 把 cli 模块里的 importlib 引用整个换成替身（而不是改真实的
    # importlib 模块——monkeypatch 的字符串导入也用它，改了会连自己都坏）。
    monkeypatch.setattr(cli, "importlib",
                        SimpleNamespace(import_module=lambda name: SimpleNamespace()))
    deps = SimpleNamespace(
        make_provider=lambda settings: SimpleNamespace(
            check=lambda: (True, "ok")),
        key_exists=lambda name: True)
    return SimpleNamespace(home=home, store=store, lines=lines, deps=deps)


class _FakeApp:
    """替身：state_path 指到临时文件，供 doctor 的权限检查用。"""

    def __init__(self, tmp_path):
        self.state_path = tmp_path / "app-state.json"


class _FakeEngine:
    """替身引擎：health_checks 的结果由测试给定。"""

    def __init__(self, checks, tmp_path):
        self._checks = checks
        self.app = _FakeApp(tmp_path)

    def health_checks(self):
        return self._checks


@pytest.fixture
def fake_engine(monkeypatch, tmp_path):
    """把 doctor 里的引擎类换成替身；测试按需调整 checks 列表。"""
    holder = SimpleNamespace(checks=[
        ("llmwiki_app", True, "/synthetic/Applications/LLM Wiki.app"),
        ("llmwiki_version", True, "0.6.13"),
        ("llmwiki_project", True, ""),
        ("llmwiki_model", True, ""),
    ], tmp_path=tmp_path)

    def factory(kb_root):
        return _FakeEngine(holder.checks, holder.tmp_path)

    monkeypatch.setattr("xhs_wiki.engines.llmwiki_app.engine.LlmWikiAppEngine",
                        factory)
    return holder


def test_doctor_shows_llmwiki_checks_line_by_line(cli_environment, fake_engine):
    """选 LLM Wiki 时逐项显示应用、版本、项目结构、整理模型。"""
    env = cli_environment
    env.store.update_settings({"engine": "llmwiki"})
    assert cli.main(["doctor"]) == 0
    assert any("LLM Wiki 应用" in line and "✓" in line for line in env.lines)
    assert any("LLM Wiki 版本" in line and "0.6.13" in line
               for line in env.lines)
    assert any("LLM Wiki 项目结构" in line and "正常" in line
               for line in env.lines)
    assert any("LLM Wiki 整理模型" in line and "正常" in line
               for line in env.lines)


def test_doctor_reports_each_llmwiki_failure(cli_environment, fake_engine):
    """不满足的项逐项打 ✗ 和原因，不再提示下载后台程序。"""
    env = cli_environment
    fake_engine.checks = [
        ("llmwiki_app", True, "/synthetic/Applications/LLM Wiki.app"),
        ("llmwiki_version", False, "0.5.0"),
        ("llmwiki_project", False, "llmwiki_project_invalid"),
        ("llmwiki_model", False, "llmwiki_model_missing"),
    ]
    env.store.update_settings({"engine": "llmwiki"})
    assert cli.main(["doctor"]) == 0
    assert any("✗ LLM Wiki 版本" in line and "0.6.13" in line
               for line in env.lines)
    assert "✗ LLM Wiki 项目结构：llmwiki_project_invalid" in env.lines
    assert "✗ LLM Wiki 整理模型：llmwiki_model_missing" in env.lines
    assert not any("install-llmwiki" in line for line in env.lines)


def test_doctor_warns_about_broad_state_permissions(cli_environment,
                                                    fake_engine):
    """app-state.json 权限比 600 宽时提示收紧，不自动修改。"""
    env = cli_environment
    env.store.update_settings({"engine": "llmwiki"})
    state = fake_engine.tmp_path / "app-state.json"
    state.write_text("{}", encoding="utf-8")
    state.chmod(0o644)
    assert cli.main(["doctor"]) == 0
    assert any("LLM Wiki 设置文件权限" in line and "建议收紧" in line
               for line in env.lines)
    # 只提示：权限保持原样。
    assert state.stat().st_mode & 0o077


def test_doctor_accepts_private_state_permissions(cli_environment,
                                                  fake_engine):
    """权限是 600 时不提示。"""
    env = cli_environment
    env.store.update_settings({"engine": "llmwiki"})
    state = fake_engine.tmp_path / "app-state.json"
    state.write_text("{}", encoding="utf-8")
    state.chmod(0o600)
    assert cli.main(["doctor"]) == 0
    assert not any("建议收紧" in line for line in env.lines)


def test_doctor_markdown_does_not_check_llmwiki(cli_environment,
                                                fake_engine):
    """轻量引擎用户不看到 LLM Wiki 的检查项。"""
    env = cli_environment
    # 故意让替身所有项都不通过：没选 LLM Wiki 时这些检查不该被调到。
    fake_engine.checks = [("llmwiki_app", False, "llmwiki_app_missing")]
    assert cli.main(["doctor"]) == 0
    assert not any("LLM Wiki" in line for line in env.lines)


def test_assets_has_no_llmwiki_installer_subcommand(cli_environment):
    """v0.4.0 起不再提供 install-llmwiki 子命令。"""
    env = cli_environment
    with pytest.raises(SystemExit) as exc:
        cli.main(["assets", "install-llmwiki"])
    assert exc.value.code != 0
    assert not any("LLM Wiki" in line for line in env.lines)
