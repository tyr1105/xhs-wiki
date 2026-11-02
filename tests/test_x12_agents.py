"""X12 第二轮：离线验证 Claude 包装脚本的能力检测与参数去重。

仅执行临时目录生成的假 CLI；帮助响应、账户信息和命令发现均为替身，
不读取真实模型配置或调用模型。后台原有的参数必须按原边界保留。
"""

from __future__ import annotations

import shlex
import subprocess
from types import SimpleNamespace

import pytest

from xhs_wiki.engines.llmwiki import agents
from xhs_wiki.llm import claude_code
from xhs_wiki.llm.base import ProcResult


@pytest.fixture
def wrapper_factory(tmp_path, monkeypatch):
    """制造假 CLI，并封住真实工具发现和系统账户读取。"""

    tool_dir = tmp_path / "fake ' $value `uname` tools"
    tool_dir.mkdir()
    tool = tool_dir / "claude"
    tool.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    tool.chmod(0o700)
    monkeypatch.setattr(agents, "find_claude", lambda: str(tool))
    monkeypatch.setattr(agents, "find_codex", lambda: None)
    monkeypatch.setattr(agents.pwd, "getpwuid", lambda uid: SimpleNamespace(
        pw_dir=str(tmp_path / "fake home"), pw_name="fake-user"))
    calls = []

    def factory(help_text):
        """每次安装只允许一次帮助探测，并返回可实际运行的包装脚本。"""

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return ProcResult(0, help_text, "")

        paths = agents.install_wrappers(bin_dir=tmp_path / "bin ' quoted", runner=runner)
        assert len(calls) == 1
        assert calls[0][0] == [str(tool), "--help"]
        return paths["claude"]

    return factory


def _arguments(script, *args):
    """实际运行假 CLI，回读参数，避免只断言脚本文本而遗漏 shell 行为。"""

    result = subprocess.run([str(script), *args], capture_output=True, text=True,
                            check=True, timeout=5)
    return result.stdout.splitlines()


@pytest.mark.parametrize("print_flag", ["--print", "-p"])
@pytest.mark.parametrize("existing", [(), ("--safe-mode",), ("--no-session-persistence",),
                                      ("--safe-mode", "--no-session-persistence")])
def test_supported_isolation_flags_are_present_once(wrapper_factory, print_flag, existing):
    """后台已传任一或两个隔离参数时只补缺失项，两种打印形式都不重复。"""

    script = wrapper_factory("--safe-mode\n--no-session-persistence\n")
    incoming = [print_flag, *existing, "--strict-mcp-config", "text ' $value `uname`"]
    actual = _arguments(script, *incoming)
    assert actual.count("--safe-mode") == 1
    assert actual.count("--no-session-persistence") == 1
    assert actual.count("--strict-mcp-config") == 1
    assert actual[-len(incoming):] == incoming


@pytest.mark.parametrize("supported", [(), ("--safe-mode",), ("--no-session-persistence",),
                                       ("--safe-mode", "--no-session-persistence")])
def test_each_isolation_flag_requires_its_own_support(wrapper_factory, supported):
    """不能用 safe-mode 的检测结果推断另一个参数也受支持。"""

    script = wrapper_factory("\n".join(supported))
    actual = _arguments(script, "--print", "fake prompt")
    for flag in ("--safe-mode", "--no-session-persistence"):
        assert (flag in actual) == (flag in supported)


def test_unsupported_existing_flags_are_preserved(wrapper_factory):
    """不支持时不追加，也不篡改后台自己传入的参数。"""

    script = wrapper_factory("--print")
    incoming = ["-p", "--safe-mode", "--no-session-persistence", "fake prompt"]
    assert _arguments(script, *incoming) == incoming


def test_non_print_calls_do_not_gain_isolation_flags(wrapper_factory):
    """版本检查等非打印调用原样转发，不添加仅供整理使用的参数。"""

    script = wrapper_factory("--safe-mode\n--no-session-persistence")
    assert _arguments(script, "--version") == ["--version"]


@pytest.mark.parametrize("result", [
    ProcResult(0, "--safe-mode --no-session-persistence", "", timed_out=True),
    ProcResult(1, "--safe-mode --no-session-persistence", ""),
    ProcResult(0, "--safe-mode-extra --no-session-persistence-extra", ""),
])
def test_failed_or_partial_help_does_not_enable_flags(tmp_path, result):
    """超时、失败或相似名称都不能当成明确支持所需参数。"""

    assert claude_code.supported_print_flags(
        "/fake/claude", runner=lambda *a, **k: result,
        env={}, cwd=tmp_path) == frozenset()


def test_help_exception_is_conservatively_unsupported(tmp_path):
    """帮助探测失败没有业务调用，不泄漏底层异常文本。"""

    def failed(*args, **kwargs):
        raise OSError("fake private error")

    assert claude_code.supported_print_flags(
        "/fake/claude", runner=failed, env={}, cwd=tmp_path) == frozenset()


def test_missing_cli_does_not_probe_help(tmp_path):
    """未发现程序时无需执行任何命令。"""

    def unexpected(*args, **kwargs):
        pytest.fail("不应调用帮助探测")

    assert claude_code.supported_print_flags(
        None, runner=unexpected, env={}, cwd=tmp_path) == frozenset()


def test_symlinked_cli_keeps_link_path_for_upgrades(tmp_path, monkeypatch):
    """claude 的入口是指向版本文件的链接时，包装脚本必须写链接本身。

    Claude Code 升级会把链接改指向新版本、再清理旧版本文件；如果脚本里
    写死解析后的旧版本路径，升级后后台就调用不到 claude 了。
    """

    versions = tmp_path / "share" / "claude" / "versions"
    versions.mkdir(parents=True)
    real = versions / "2.1.259"
    real.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    real.chmod(0o700)
    link_dir = tmp_path / "local bin"
    link_dir.mkdir()
    link = link_dir / "claude"
    link.symlink_to(real)
    monkeypatch.setattr(agents, "find_claude", lambda: str(link))
    monkeypatch.setattr(agents, "find_codex", lambda: None)
    monkeypatch.setattr(agents.pwd, "getpwuid", lambda uid: SimpleNamespace(
        pw_dir=str(tmp_path / "home"), pw_name="fake-user"))

    def runner(argv, **kwargs):
        return ProcResult(0, "--safe-mode --no-session-persistence", "")

    paths = agents.install_wrappers(bin_dir=tmp_path / "bin", runner=runner)
    script = paths["claude"].read_text(encoding="utf-8")
    assert f"REAL={shlex.quote(str(link))}" in script
    assert str(real) not in script
