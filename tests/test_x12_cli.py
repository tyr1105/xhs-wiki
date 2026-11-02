# -*- coding: utf-8 -*-
"""X12 命令行回归：验证后台安装自动配置和 doctor 的按引擎检查。

所有目录均为临时目录，下载器、命令查找和登录检查都是假实现；不访问
网络、不启动后台或模型、不读写钥匙串。
"""

from types import SimpleNamespace

import pytest

from xhs_wiki import assets, cli
from xhs_wiki.llm import claude_code, codex
from xhs_wiki.store import ServiceStore


@pytest.fixture
def cli_environment(tmp_path, monkeypatch):
    """隔离应用和知识库，收集输出并禁止真实的命令行助手查找。"""
    home = tmp_path / "generated home"
    monkeypatch.setenv("XHS_WIKI_HOME", str(home))
    store = ServiceStore(home)
    store.update_settings({"kb_root": str(tmp_path / "generated kb")})
    lines = []
    monkeypatch.setattr(cli, "_print", lines.append)
    monkeypatch.setattr(cli.importlib, "import_module", lambda name: SimpleNamespace())
    monkeypatch.setattr(claude_code, "find_claude", lambda: None)
    monkeypatch.setattr(codex, "find_codex", lambda: None)
    deps = SimpleNamespace(make_provider=lambda settings: SimpleNamespace(check=lambda: (True, "ok")),
                           key_exists=lambda name: True)
    return SimpleNamespace(home=home, store=store, lines=lines, deps=deps)


def test_install_llmwiki_command_writes_absolute_path(cli_environment, monkeypatch):
    """安装成功才更新本体路径，并保留其他设置。"""
    env = cli_environment
    binary = (env.home / "vendor" / "generated" / "llm-wiki-desktop").resolve()
    calls = []
    def install(home):
        """记录应用目录并返回假安装的绝对路径，不创建或启动程序。"""
        calls.append(home)
        return binary
    monkeypatch.setattr(assets, "install_llmwiki", install)
    assert cli.main(["--home", str(env.home), "assets", "install-llmwiki"]) == 0
    assert calls == [env.home.resolve()]
    settings = env.store.load_settings()
    assert settings["llmwiki_binary"] == str(binary)
    assert settings["engine"] == "markdown"
    assert any("已自动配置" in line for line in env.lines)


def test_failed_install_does_not_change_setting(cli_environment, monkeypatch):
    """校验失败会输出固定代码，设置中仍没有可执行路径。"""
    env = cli_environment
    def fail(home):
        """模拟资源校验失败。"""
        raise ValueError("assets_sha256_mismatch")
    monkeypatch.setattr(assets, "install_llmwiki", fail)
    assert cli.main(["assets", "install-llmwiki"]) == 1
    assert env.store.load_settings()["llmwiki_binary"] is None
    assert env.lines[-1] == "错误：assets_sha256_mismatch"


@pytest.mark.parametrize("provider", ["claude_code", "codex"])
def test_doctor_checks_only_selected_agent(cli_environment, monkeypatch, provider):
    """LLM Wiki 仅要求所选助手可找到，另一助手未装也通过。"""
    env = cli_environment
    env.store.update_settings({"engine": "llmwiki", "llm": {"provider": provider, "model": "generated"}})
    seen = []
    monkeypatch.setattr(assets, "llmwiki_ready", lambda home: seen.append(home) or True)
    selected = claude_code if provider == "claude_code" else codex
    name = "find_claude" if provider == "claude_code" else "find_codex"
    monkeypatch.setattr(selected, name, lambda: "generated-agent")
    assert cli.main(["doctor"], deps_factory=lambda: env.deps) == 0
    assert seen == [env.home.resolve()]
    assert "✓ LLM Wiki 后台程序：文件校验通过" in env.lines
    assert f"✓ LLM Wiki 整理工具：{provider} 已找到" in env.lines


def test_doctor_reports_missing_binary_and_agent(cli_environment, monkeypatch):
    """缺后台与缺助手分别显示，不尝试自行下载或启动。"""
    env = cli_environment
    env.store.update_settings({"engine": "llmwiki"})
    monkeypatch.setattr(assets, "llmwiki_ready", lambda home: False)
    assert cli.main(["doctor"], deps_factory=lambda: env.deps) == 0
    assert any("✗ LLM Wiki 后台程序" in line and "install-llmwiki" in line for line in env.lines)
    assert "✗ LLM Wiki 整理工具：llmwiki_agent_missing" in env.lines


def test_doctor_markdown_does_not_check_llmwiki(cli_environment, monkeypatch):
    """轻量引擎完全不核对可选后台，避免无关提示与额外文件扫描。"""
    env = cli_environment
    monkeypatch.setattr(assets, "llmwiki_ready", lambda home: pytest.fail("unexpected_optional_check"))
    assert cli.main(["doctor"], deps_factory=lambda: env.deps) == 0
    assert not any("LLM Wiki" in line for line in env.lines)
