"""X12 引擎、公共工厂和 CLI 包装脚本的离线回归测试。

测试只执行临时目录中的假 shell 工具；账户、可执行文件发现、帮助
探测、后台及项目连接均使用替身，不调用模型、不触碰真实用户配置。
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from xhs_wiki import engines
from xhs_wiki.engines.llmwiki import agents
from xhs_wiki.engines.llmwiki import engine as engine_module
from xhs_wiki.engines.llmwiki.backend import Backend, EngineUnavailable
from xhs_wiki.llm import claude_code
from xhs_wiki.llm.base import ProcResult


@pytest.fixture(autouse=True)
def isolated_agents(tmp_path, monkeypatch):
    """封住真实 CLI 发现及账户查询，遗漏依赖注入也不会访问真实用户。"""

    account = SimpleNamespace(pw_dir=str(tmp_path / "account ' home"), pw_name="fake'user")
    monkeypatch.setattr(agents.pwd, "getpwuid", lambda uid: account)
    monkeypatch.setattr(agents, "find_claude", lambda: None)
    monkeypatch.setattr(agents, "find_codex", lambda: None)
    monkeypatch.setattr(claude_code, "default_runner", lambda *a, **k: ProcResult(0, "", ""))
    return account


def fake_tool(root: Path, name="claude") -> Path:
    """生成只回显参数及环境的假 CLI，包含危险字符的路径仍必须原样执行。"""

    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(
        '#!/bin/sh\nprintf "arg:%s\\n" "$@"\n'
        'printf "home:%s\\nuser:%s\\nconfig:%s\\n" "$HOME" "$USER" "${CLAUDE_CONFIG_DIR:-unset}"\n'
    )
    path.chmod(0o700)
    return path


def run_wrapper(script, *args):
    """只运行本测试生成的 shell 脚本；用列表保留每一个原始参数边界。"""

    result = subprocess.run([str(script), *args], capture_output=True, text=True,
                            timeout=5, check=True)
    return result.stdout.splitlines()


def test_install_only_claude(tmp_path, monkeypatch):
    """仅安装 Claude 时不要求 Codex。"""

    monkeypatch.setattr(agents, "find_claude", lambda: str(fake_tool(tmp_path / "tools")))
    paths = agents.install_wrappers(provider="claude_code", bin_dir=tmp_path / "bin")
    assert set(paths) == {"claude"}
    assert not (tmp_path / "bin" / "codex").exists()


def test_install_only_codex(tmp_path, monkeypatch):
    """仅安装 Codex 时不要求 Claude。"""

    monkeypatch.setattr(agents, "find_codex", lambda: str(fake_tool(tmp_path / "tools", "codex")))
    paths = agents.install_wrappers(provider="codex", bin_dir=tmp_path / "bin")
    assert set(paths) == {"codex"}


def test_install_both_tools(tmp_path):
    """两个工具都有时为两者生成包装脚本。"""

    paths = agents.install_wrappers(
        provider="codex", claude_path=fake_tool(tmp_path / "tools"),
        codex_path=fake_tool(tmp_path / "tools", "codex"), bin_dir=tmp_path / "bin")
    assert set(paths) == {"claude", "codex"}


@pytest.mark.parametrize("provider, code", [("claude_code", "claude"), ("codex", "codex")])
def test_selected_tool_missing(tmp_path, provider, code):
    """所选 CLI 缺失必须报错，不能静默选另一个提供方。"""

    with pytest.raises(ValueError, match=f"^{code}_executable_not_found$"):
        agents.install_wrappers(provider=provider, bin_dir=tmp_path / "bin")
    assert not (tmp_path / "bin").exists()


def test_non_executable_tool_is_missing(tmp_path):
    """仅有同名文件而不可执行，仍视为工具缺失。"""

    tool = fake_tool(tmp_path / "tools")
    tool.chmod(0o600)
    with pytest.raises(ValueError, match="^claude_executable_not_found$"):
        agents.install_wrappers(claude_path=tool, bin_dir=tmp_path / "bin")


def test_unsupported_provider_does_not_create_files(tmp_path):
    """API 提供方不能误走 CLI 包装链路。"""

    with pytest.raises(ValueError, match="^llmwiki_provider_unsupported$"):
        agents.install_wrappers(provider="anthropic", bin_dir=tmp_path / "bin")
    assert not (tmp_path / "bin").exists()


@pytest.mark.parametrize("provider, name", [("claude_code", "claude"), ("codex", "codex")])
def test_account_values_ignore_environment(tmp_path, monkeypatch, isolated_agents, provider, name):
    """HOME/USER 从 pwd 读取；引号、美元和反引号路径不能改变参数或执行命令。"""

    monkeypatch.setenv("HOME", str(tmp_path / "wrong-home"))
    monkeypatch.setenv("USER", "wrong-user")
    tool = fake_tool(tmp_path / "fake ' $value `uname` tools", name)
    paths = agents.install_wrappers(provider=provider, bin_dir=tmp_path / "bin ' quote",
                                   **{f"{name}_path": tool})
    output = run_wrapper(paths[name], "--version")
    assert f"home:{isolated_agents.pw_dir}" in output
    assert f"user:{isolated_agents.pw_name}" in output
    assert "wrong-user" not in "\n".join(output)


@pytest.mark.parametrize("supported", [True, False])
def test_claude_safe_mode_and_arguments(tmp_path, supported):
    """实际执行特殊路径下的假 CLI，断言 safe-mode 和每个原始参数准确传递。"""

    calls = []

    def help_runner(argv, **kwargs):
        """模拟 --help 的能力声明，不运行真实 Claude。"""
        calls.append((argv, kwargs))
        return ProcResult(0, "--safe-mode" if supported else "--print", "")

    tool = fake_tool(tmp_path / "fake ' $tool `uname` tools")
    paths = agents.install_wrappers(claude_path=tool, bin_dir=tmp_path / "bin ' quote",
                                   runner=help_runner)
    agents.write_model_config("claude-unit-5-5[1m]", tmp_path / "bin ' quote")
    output = run_wrapper(paths["claude"], "-p", "text ' $value `uname`", "--output-format", "json")
    expected = ["arg:--model", "arg:claude-unit-5-5[1m]"]
    if supported:
        expected.append("arg:--safe-mode")
    expected += ["arg:-p", "arg:text ' $value `uname`", "arg:--output-format", "arg:json"]
    assert [line for line in output if line.startswith("arg:")] == expected
    assert len(calls) == 1
    assert calls[0][0] == [str(tool.resolve()), "--help"]
    assert calls[0][1]["timeout_s"] == 15


def test_safe_mode_empty_model_and_non_print(tmp_path):
    """未填模型仍开启支持的安全模式；非打印命令不加该参数。"""

    paths = agents.install_wrappers(claude_path=fake_tool(tmp_path / "tools"),
                                   bin_dir=tmp_path / "bin",
                                   runner=lambda *a, **k: ProcResult(0, "--safe-mode", ""))
    agents.write_model_config("", tmp_path / "bin")
    printed = run_wrapper(paths["claude"], "--print", "text")
    assert "arg:--safe-mode" in printed
    assert "arg:--model" not in printed
    assert "arg:--safe-mode" not in run_wrapper(paths["claude"], "--version")


@pytest.mark.parametrize("model", ["x;y", "x y", "x'y", "x$y", "x`y", "x\ny", "x" * 101, None])
def test_model_validation_rejects_shell_syntax(tmp_path, model):
    """非法模型不能进入可被 shell 读取的文件。"""

    with pytest.raises(ValueError, match="^llm_model_invalid$"):
        agents.write_model_config(model, tmp_path / "bin")
    assert not (tmp_path / "bin").exists()


@pytest.mark.parametrize("model", ["", "claude-unit-5-5[1m]", "A1._:-[]", "x" * 100])
def test_model_config_is_quoted_and_private(tmp_path, model):
    """合法字符及长度边界保留，配置用 shell 引号编码且权限为 0600。"""

    path = agents.write_model_config(model, tmp_path / "bin")
    assert path.read_text() == f"MODEL={shlex.quote(model)}\n"
    assert path.stat().st_mode & 0o777 == 0o600


def test_unchanged_wrappers_and_model_keep_mtime(tmp_path):
    """多次准备同一配置不会改写脚本和模型配置。"""

    tool = fake_tool(tmp_path / "tools")
    directory = tmp_path / "bin"
    paths = agents.install_wrappers(claude_path=tool, bin_dir=directory)
    conf = agents.write_model_config("fake-model", directory)
    old = {path: path.stat().st_mtime_ns for path in [*paths.values(), conf]}
    agents.install_wrappers(claude_path=tool, bin_dir=directory)
    agents.write_model_config("fake-model", directory)
    assert old == {path: path.stat().st_mtime_ns for path in old}
    assert directory.stat().st_mode & 0o777 == 0o700
    assert paths["claude"].stat().st_mode & 0o777 == 0o700
    assert list(directory.glob(".tmp-*")) == []


def test_unavailable_optional_wrapper_removed(tmp_path):
    """另一个 CLI 被卸载后，不让旧包装脚本冒充它仍可用。"""

    directory = tmp_path / "bin"
    claude, codex = fake_tool(tmp_path / "tools"), fake_tool(tmp_path / "tools", "codex")
    agents.install_wrappers(claude_path=claude, codex_path=codex, bin_dir=directory)
    codex.unlink()
    agents.install_wrappers(claude_path=claude, bin_dir=directory)
    assert not (directory / "codex").exists()


class FakeClient:
    """仅回应项目打开请求的内存客户端，无网络或端口。"""

    def request(self, *args, **kwargs):
        """返回一个固定虚构项目标识。"""
        return 200, {"data": {"projectId": "fake-project"}}


class FakeBackend:
    """记录启动状态，在 start 内检查启动前置条件。"""

    def __init__(self, check=lambda: None):
        """保存检查函数与启动计数。"""
        self.check, self.started = check, 0

    def is_running(self):
        """反映是否已被当前测试启动。"""
        return bool(self.started)

    def start(self):
        """执行前置检查，随后标为已启动。"""
        self.check()
        self.started += 1

    def client(self):
        """返回无网络的客户端。"""
        return FakeClient()

    def stop(self):
        """测试结束只更新内存状态。"""
        self.started = 0


def test_engine_generates_wrappers_before_start(tmp_path, monkeypatch):
    """_ensure 必须先落脚本和模型配置，再启动后台；重复连接不再生成。"""

    home, kb = tmp_path / "app", tmp_path / "kb"
    calls = []
    tool = fake_tool(tmp_path / "tools")
    monkeypatch.setattr(agents, "find_claude", lambda: str(tool))
    monkeypatch.setattr(engine_module, "scaffold", lambda root: Path(root).mkdir(exist_ok=True))

    def install(**kwargs):
        """记录安装次数并走真实脚本生成逻辑。"""
        calls.append(kwargs)
        return agents.install_wrappers(**kwargs)

    def before_start():
        """在假后台启动瞬间核对脚本、模型及项目目录。"""
        assert (home / "llmwiki-bin" / "claude").is_file()
        assert "fake-model" in (home / "llmwiki-bin" / "model.conf").read_text()
        assert kb.is_dir()

    backend = FakeBackend(before_start)
    engine = engine_module.LlmWikiEngine(kb, home, {"llm": {"provider": "claude_code", "model": "fake-model"}},
                                         backend=backend, wrapper_installer=install)
    try:
        engine._ensure()
        engine._ensure()
        assert backend.started == 1
        assert len(calls) == 1
        assert calls[0]["provider"] == "claude_code"
    finally:
        engine.close()


@pytest.mark.parametrize("provider", ["claude_code", "codex"])
def test_engine_selected_cli_missing_does_not_start(tmp_path, provider):
    """找不到当前提供方时给稳定错误代码，不启动后台。"""

    backend = FakeBackend()
    engine = engine_module.LlmWikiEngine(tmp_path / "kb", tmp_path / "app",
                                         {"llm": {"provider": provider}}, backend=backend)
    with pytest.raises(EngineUnavailable, match="^llmwiki_agent_missing$"):
        engine._ensure()
    assert backend.started == 0
    assert not (tmp_path / "kb").exists()


def test_model_write_failure_stops_before_backend(tmp_path, monkeypatch):
    """配置写失败必须停，不允许用用户未选的默认模型继续。"""

    def denied(*args, **kwargs):
        """模拟写失败，错误原文不能出现在引擎结果中。"""
        raise OSError("private details")

    monkeypatch.setattr(agents, "write_model_config", denied)
    backend = FakeBackend()
    engine = engine_module.LlmWikiEngine(tmp_path / "kb", tmp_path / "app", {"llm": {}},
                                         backend=backend, wrapper_installer=lambda **k: {})
    assert engine.health() == (False, "llmwiki_wrapper_failed")
    assert backend.started == 0


def test_backend_waiting_function_is_injectable(tmp_path, monkeypatch):
    """后台接受引擎传来的 sleep，首次启动接口不再发生参数不兼容。"""

    pauses = []
    root = tmp_path / "kb"
    root.mkdir()
    import_root = tmp_path / "import"
    import_root.mkdir()
    process = SimpleNamespace(pid=123, poll=lambda: None)
    backend = Backend(tmp_path / "fake-backend", root, import_root, 12345,
                      agent_bin_dir=tmp_path / "bin", spawn=lambda *args: process,
                      clock=lambda: 0, sleep=pauses.append)
    ready = iter([False, True])
    client = SimpleNamespace(request=lambda *a, **k: (200, {"data": {"ready": next(ready), "webviewWindowCount": 0}}))
    monkeypatch.setattr(backend, "client", lambda: client)
    backend.start()
    assert pauses == [0.2]


def test_shared_factory_selects_both_engines(tmp_path, monkeypatch):
    """公共工厂集中两种构造规则，并原样传递提供方和数据目录。"""

    from xhs_wiki.engines.markdown import engine as markdown

    calls = []
    monkeypatch.setattr(markdown, "MarkdownEngine", lambda root, provider: calls.append((root, provider)))
    monkeypatch.setattr(engine_module, "LlmWikiEngine", lambda root, home, settings: calls.append((root, home, settings)))
    provider = object()
    settings = {"engine": "markdown", "kb_root": str(tmp_path / "kb")}
    engines.make_engine(settings, provider, tmp_path / "app")
    assert calls[-1] == (settings["kb_root"], provider)
    settings = {**settings, "engine": "llmwiki"}
    engines.make_engine(settings, None, tmp_path / "app")
    assert calls[-1] == (Path(settings["kb_root"]), tmp_path / "app", settings)


def test_runner_delegates_to_shared_factory(tmp_path, monkeypatch):
    """runner 委托公共函数，不再维护第二份 LLM Wiki 选择分支。"""

    from xhs_wiki import runner

    monkeypatch.setenv("XHS_WIKI_HOME", str(tmp_path / "app"))
    sentinel, calls = object(), []

    def factory(*args):
        """记录共同工厂的入参。"""
        calls.append(args)
        return sentinel

    monkeypatch.setattr(engines, "make_engine", factory)
    deps = runner.default_deps()
    settings = {"engine": "llmwiki", "kb_root": str(tmp_path / "kb")}
    assert deps.make_engine(settings) is sentinel
    assert calls == [(settings, None, tmp_path / "app")]


@pytest.mark.parametrize("error", [OSError("private path"), ValueError("private text / path")])
def test_health_hides_unstructured_exception_details(tmp_path, monkeypatch, error):
    """系统错误原文可能带私有路径，health 只能返回固定代码。"""

    engine = engine_module.LlmWikiEngine(tmp_path / "kb", tmp_path / "app", {})

    def fail():
        """注入不可公开的底层错误。"""
        raise error

    monkeypatch.setattr(engine, "_ensure", fail)
    assert engine.health() == (False, "engine_unavailable")


def test_engine_default_backend_first_start_is_fully_prepared(tmp_path, monkeypatch):
    """真实引擎和后台类组合，替换启动器与 HTTP，覆盖首次连接衔接。"""

    from xhs_wiki.engines.llmwiki import backend as backend_module

    home, kb = tmp_path / "app", tmp_path / "kb"
    tool, binary = fake_tool(tmp_path / "tools"), fake_tool(tmp_path / "tools", "backend")
    monkeypatch.setattr(agents, "find_claude", lambda: str(tool))
    monkeypatch.setattr(engine_module, "scaffold", lambda root: Path(root).mkdir(exist_ok=True))
    state = {"stopped": False, "starts": 0}

    def terminate():
        """只改变测试内存中的进程状态，不向系统发送信号。"""
        state["stopped"] = True

    process = SimpleNamespace(pid=123, poll=lambda: 0 if state["stopped"] else None,
                              terminate=terminate)

    def spawn(argv, env):
        """启动前核对真正传给后台的包装目录和项目目录。"""
        state["starts"] += 1
        assert kb.is_dir()
        assert (home / "llmwiki-bin" / "claude").is_file()
        assert (home / "llmwiki-bin" / "model.conf").is_file()
        assert env["PATH"].split(":")[0] == str(home / "llmwiki-bin")
        assert argv[0] == str(binary)
        return process

    class ReadyClient(FakeClient):
        """同时满足后台健康检查及项目打开的内存替身。"""

        def request(self, method, path, *args, **kwargs):
            """根据请求路径返回固定成功结果。"""
            if path == "/health":
                return 200, {"data": {"ready": True, "webviewWindowCount": 0}}
            return super().request(method, path, *args, **kwargs)

    monkeypatch.setattr(backend_module, "_default_spawn", spawn)
    monkeypatch.setattr(Backend, "client", lambda self: ReadyClient())
    engine = engine_module.LlmWikiEngine(kb, home, {
        "llmwiki_binary": str(binary), "llm": {"provider": "claude_code", "model": "fake-model"}},
        sleep=lambda _: None)
    try:
        assert engine.health() == (True, "ok")
        assert state["starts"] == 1
    finally:
        engine.close()
    assert state["stopped"] is True
