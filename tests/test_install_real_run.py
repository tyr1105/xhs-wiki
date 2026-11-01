# -*- coding: utf-8 -*-
"""安装和卸载脚本的非 dry-run 回归测试。

这个文件验证实际分支及删除边界。HOME、应用、日志和 Skill 路径均隔离到
临时目录；uv、网络、浏览器、服务管理和信号均由 PATH 替身记录。
只有本地 Python、文件操作及系统 plutil 真正执行，不安装任何依赖或服务。
"""

import json
import os
import plistlib
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
UNINSTALL_SH = REPO_ROOT / "uninstall.sh"

# 替身集中实现，程序名及参数写成 JSON，便于检查参数边界和调用次序。
FAKE_TOOL = r'''
import json
import os
import sys
from pathlib import Path

tool, *args = sys.argv[1:]
root = Path(os.environ["Y1_TEST_ROOT"])
with (root / "calls.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps([tool, *args]) + "\n")
# 只记录本卡需要的镜像变量，验证子进程继承，避免采集无关环境信息。
if tool in {"uv", "nohup"}:
    with (root / "mirror_environment.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"tool": tool, "environment": {
            name: os.environ[name] for name in (
                "UV_DEFAULT_INDEX", "UV_PYTHON_INSTALL_MIRROR", "HF_ENDPOINT"
            ) if name in os.environ
        }}) + "\n")
state_path = root / "process.json"
state = json.loads(state_path.read_text()) if state_path.exists() else {}
if tool == "id":
    print(os.environ.get("Y1_TEST_UID", "501") if "-u" in args else "generated-user")
elif tool == "sw_vers":
    print("13.0")
elif tool == "uname":
    print("arm64")
elif tool == "df":
    print("Filesystem 1024-blocks Used Available Capacity Mounted")
    print("generated 999999999 1 999999998 1% /")
elif tool == "lsof":
    if state.get("listening"):
        print("4242")
    else:
        sys.exit(1)
elif tool == "ps":
    print(state.get("command", ""))
elif tool == "kill":
    if not os.environ.get("Y1_TEST_KEEP_PROCESS"):
        state["listening"] = False
        state_path.write_text(json.dumps(state))
elif tool == "launchctl":
    if args[0] == "print":
        sys.exit(0 if state.get("launch_agent") else 1)
    if args[0] == "bootout" and not os.environ.get("Y1_TEST_KEEP_PROCESS"):
        state.update(listening=False, launch_agent=False)
        state_path.write_text(json.dumps(state))
    if args[0] == "bootstrap" and os.environ.get("Y1_TEST_BOOTSTRAP_FAIL"):
        sys.exit(5)
elif tool == "curl":
    # 此用例要验证后台继承：假服务必须先真正启动，健康检查才能说就绪。
    # 否则 curl 立即成功，父脚本退出时后台替身可能还没被操作系统调度。
    if os.environ.get("Y4_TEST_WAIT_NOHUP") and args[-1].endswith("/healthz"):
        environments = [json.loads(line) for line in
                        (root / "mirror_environment.jsonl").read_text().splitlines()]
        if not any(record["tool"] == "nohup" for record in environments):
            sys.exit(7)
    sys.exit(int(os.environ.get("Y1_TEST_CURL_STATUS", "0")))
elif tool == "uv":
    if args[0] == "--version":
        print("uv 0.12.13")
    elif args[0] == "venv":
        bindir = Path(args[-1]) / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        python = bindir / "python"
        python.write_text('#!/bin/sh\n'
                          'case "$*" in *print-launch-agent*) '
                          'if [ "${Y1_TEST_BAD_PLIST:-}" = yes ]; then printf "invalid plist\\n"; exit 0; fi;; esac\n'
                          'exec "$Y1_TEST_PYTHON" "$@"\n')
        python.chmod(0o700)
        cli = bindir / "xhs-wiki"
        cli.write_text('#!/bin/sh\nexec "$Y1_TEST_PYTHON" "$Y1_TEST_DRIVER" xhs-wiki "$@"\n')
        cli.chmod(0o700)
    elif args[:2] == ["pip", "install"]:
        sys.exit(int(os.environ.get("Y1_TEST_INSTALL_STATUS", "0")))
elif tool == "xhs-wiki":
    assert args == ["open"], "测试禁止启动真实服务"
    sys.exit(int(os.environ.get("Y1_TEST_OPEN_STATUS", "0")))
elif tool in {"open", "nohup", "sleep", "claude"}:
    pass
else:
    raise AssertionError("unexpected_fake_tool")
'''


class ScriptSandbox:
    """给 shell 子进程提供完全独立的文件与外部命令环境。"""

    def __init__(self, root):
        """先准备替身，再设置最小 PATH，防止落回本机 uv 或服务命令。"""
        self.root = root.resolve()
        self.repo = REPO_ROOT
        self.home = self.root / "home"
        self.app = self.home / "app data"
        self.logs = self.home / "logs"
        self.skills = self.home / "skills"
        self.bin = self.root / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        chrome = self.root / "Generated Chrome.app"
        chrome.mkdir()
        self.driver = self.root / "fake_tool.py"
        self.driver.write_text(FAKE_TOOL, encoding="utf-8")
        self.env = {
            "HOME": str(self.home), "USER": "generated-user", "LANG": "en_US.UTF-8",
            "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": str(self.root), "PYTHONPATH": str(REPO_ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1", "XHS_WIKI_HOME": str(self.app),
            "XHS_WIKI_LOG_DIR": str(self.logs), "XHS_WIKI_SKILLS_ROOT": str(self.skills),
            "XHS_WIKI_CHROME_APP": str(chrome), "XHS_WIKI_REPO": "",
            "Y1_TEST_ROOT": str(self.root), "Y1_TEST_DRIVER": str(self.driver),
            "Y1_TEST_PYTHON": str(REPO_ROOT / ".venv/bin/python"),
        }
        for name in ("id", "sw_vers", "uname", "df", "lsof", "ps", "kill", "launchctl",
                     "curl", "uv", "open", "nohup", "sleep", "claude"):
            self.write_tool(name)

    def write_tool(self, name):
        """参数列表原样转发，路径含空格也不会被 shell 再次拆开。"""
        target = self.bin / name
        target.write_text('#!/bin/sh\nexec "$Y1_TEST_PYTHON" "$Y1_TEST_DRIVER" '
                          + shlex.quote(name) + ' "$@"\n', encoding="utf-8")
        target.chmod(0o700)

    def run(self, script, *args, local=True, piped=False, source=None):
        """执行实际 shell 分支；输入与所有用户目录均在本测试隔离区。"""
        parameters = ["--app-home", str(self.app), *args]
        if script == INSTALL_SH and local:
            parameters += ["--from-local", "."]
        if piped or source is not None:
            command = ["/bin/bash", "-s", "--", *parameters]
            input_text = script.read_text(encoding="utf-8") if source is None else source
        else:
            command = ["/bin/bash", str(script), *parameters]
            input_text = ""
        return subprocess.run(command, input=input_text, capture_output=True, text=True,
                              cwd=REPO_ROOT, env=self.env, timeout=120, start_new_session=True)

    def calls(self, tool=None):
        """按需过滤替身记录，避免测试依赖不相关的环境探测次数。"""
        path = self.root / "calls.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [record for record in records if tool is None or record[0] == tool]

    def snapshot(self):
        """只扫描临时 HOME，记录内容与链接目标以捕获预览意外写入。"""
        result = {}
        for path in self.home.rglob("*"):
            result[str(path.relative_to(self.home))] = (
                ("link", os.readlink(path)) if path.is_symlink()
                else ("dir",) if path.is_dir() else ("file", path.read_bytes()))
        return result

    def mark_app(self, **settings):
        """构造虚构已有安装，标记和设置只能位于当前临时主目录。"""
        self.app.mkdir(parents=True, exist_ok=True)
        (self.app / ".xhs-wiki-install").write_text("xhs-wiki\n", encoding="utf-8")
        if settings:
            (self.app / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    def process(self, command=None, launch_agent=False):
        """假的信号只改变 JSON 状态，不向真实 PID 发送信号。"""
        (self.root / "process.json").write_text(json.dumps({
            "listening": True, "launch_agent": launch_agent,
            "command": command or f"{self.app}/venv/bin/xhs-wiki serve",
        }), encoding="utf-8")

    def knowledge_base(self, user_file=False):
        """生成白名单内容，可加用户文件验证清理范围不扩大。"""
        kb = self.home / "generated knowledge"
        kb.mkdir()
        for name in ("主题", "来源", ".xhs-wiki"):
            (kb / name).mkdir()
            (kb / name / "generated.txt").write_text("generated", encoding="utf-8")
        for name in ("索引.md", "暂缓.md"):
            (kb / name).write_text("generated", encoding="utf-8")
        if user_file:
            (kb / "user-file.txt").write_text("keep", encoding="utf-8")
        self.mark_app(kb_root=str(kb))
        return kb


@pytest.fixture
def sandbox(tmp_path):
    """每个测试使用新的临时目录，失败后也不影响真实用户文件。"""
    return ScriptSandbox(tmp_path)


def test_real_install_generates_valid_private_plist_and_opens_cli(sandbox):
    """实际运行第五步，再用系统 plutil 检查 plist 没有命令回显污染。"""
    result = sandbox.run(INSTALL_SH, "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    plist = sandbox.home / "Library/LaunchAgents/io.github.xhs-wiki.service.plist"
    checked = subprocess.run(["/usr/bin/plutil", "-lint", str(plist)], env=sandbox.env,
                             capture_output=True, text=True, check=False)
    assert checked.returncode == 0, checked.stderr + checked.stdout
    payload = plistlib.loads(plist.read_bytes())
    for key, expected in (("XHS_WIKI_HOME", sandbox.app), ("XHS_WIKI_LOG_DIR", sandbox.logs),
                          ("XHS_WIKI_SKILLS_ROOT", sandbox.skills)):
        assert payload["EnvironmentVariables"][key] == str(expected)
    assert Path(payload["StandardOutPath"]).parent == sandbox.logs
    assert Path(payload["StandardErrorPath"]).parent == sandbox.logs
    assert plist.stat().st_mode & 0o777 == 0o600
    assert sandbox.app.stat().st_mode & 0o777 == 0o700
    assert (sandbox.app / ".xhs-wiki-install").is_file()
    assert sorted(path.name for path in plist.parent.iterdir()) == [plist.name]
    assert sandbox.calls("xhs-wiki") == [["xhs-wiki", "open"]]
    assert sandbox.calls("open") == []
    assert any(record[1] == "bootstrap" for record in sandbox.calls("launchctl"))
    assert sandbox.calls("curl")
    assert all("--noproxy" in record and "*" in record for record in sandbox.calls("curl"))
    assert all(record[-1] == "http://127.0.0.1:18765/healthz" for record in sandbox.calls("curl"))
    assert all(0 < int(record[record.index("--max-time") + 1]) <= 60
               for record in sandbox.calls("curl"))


def test_remote_install_defaults_to_release_archive(sandbox):
    """未设置发布地址时使用正式源码包；替身确保实际分支也不下载。"""
    result = sandbox.run(INSTALL_SH, "--yes", local=False)
    assert result.returncode == 0, result.stdout + result.stderr
    install = next(call for call in sandbox.calls("uv") if call[1:3] == ["pip", "install"])
    assert install[-1] == "https://github.com/tyr1105/xhs-wiki/archive/refs/heads/main.tar.gz"
    assert "--only-binary" in install
    assert not any(call[0] in {"ffmpeg", "brew", "git"} for call in sandbox.calls())


@pytest.mark.parametrize("launch_agent", [True, False])
def test_cn_mirror_reaches_install_and_background_service(launch_agent, sandbox):
    """实际分支让安装组件与两种后台启动方式使用同一组镜像配置。"""
    expected = {
        "UV_DEFAULT_INDEX": "https://mirrors.aliyun.com/pypi/simple/",
        "UV_PYTHON_INSTALL_MIRROR": "https://registry.npmmirror.com/-/binary/python-build-standalone",
        "HF_ENDPOINT": "https://hf-mirror.com",
    }
    if not launch_agent:
        sandbox.env["Y4_TEST_WAIT_NOHUP"] = "yes"
    result = sandbox.run(INSTALL_SH, "--yes", "--mirror", "cn",
                         *([] if launch_agent else ["--no-launch-agent"]), local=False)
    assert result.returncode == 0, result.stdout + result.stderr
    environments = [json.loads(line) for line in
                    (sandbox.root / "mirror_environment.jsonl").read_text().splitlines()]
    assert any(record["tool"] == "uv" for record in environments)
    assert all(record["environment"] == expected for record in environments)
    if launch_agent:
        plist = sandbox.home / "Library/LaunchAgents/io.github.xhs-wiki.service.plist"
        payload = plistlib.loads(plist.read_bytes())
        assert {key: payload["EnvironmentVariables"][key] for key in expected} == expected
    else:
        assert any(record["tool"] == "nohup" for record in environments)


def test_invalid_plist_never_bootstraps_or_replaces_previous_file(sandbox):
    """生成器返回坏内容时，只丢弃临时文件，保留此前有效的启动配置。"""
    plist = sandbox.home / "Library/LaunchAgents/io.github.xhs-wiki.service.plist"
    plist.parent.mkdir(parents=True)
    original = plistlib.dumps({"Label": "generated previous configuration"})
    plist.write_bytes(original)
    sandbox.env["Y1_TEST_BAD_PLIST"] = "yes"
    result = sandbox.run(INSTALL_SH, "--yes")
    assert result.returncode != 0
    assert plist.read_bytes() == original
    assert not any(call[1] == "bootstrap" for call in sandbox.calls("launchctl"))
    assert sorted(path.name for path in plist.parent.iterdir()) == [plist.name]


def test_uv_download_failure_is_not_reported_as_path_problem(sandbox):
    """模拟 uv 缺失及 curl 失败，不能继续执行安装器或误导用户重开终端。"""
    # 只替换“是否找到 uv”的查询；其他命令仍由已注入的 PATH 替身负责。
    bash_env = sandbox.root / "bash-env"
    bash_env.write_text(
        'command() { if [ "$#" -eq 2 ] && [ "$1" = -v ] && [ "$2" = uv ]; '
        'then return 1; fi; builtin command "$@"; }\n', encoding="utf-8")
    sandbox.env["BASH_ENV"] = str(bash_env)
    sandbox.env["Y1_TEST_CURL_STATUS"] = "7"
    before = sandbox.snapshot()
    result = sandbox.run(INSTALL_SH, "--yes")
    assert result.returncode != 0
    assert "下载 uv 安装脚本失败" in result.stderr
    assert "重新打开终端" not in result.stderr
    assert sandbox.calls("uv") == [] and sandbox.snapshot() == before
    assert len(sandbox.calls("curl")) == 1
    assert not list(sandbox.root.glob("xhs-wiki-uv.*"))


def test_uv_install_failure_keeps_actual_error_context(sandbox):
    """安装依赖失败必须让用户查看 uv 原因，不能统一误报仓库不完整。"""
    sandbox.env["Y1_TEST_INSTALL_STATUS"] = "1"
    result = sandbox.run(INSTALL_SH, "--yes")
    assert result.returncode != 0
    assert "uv" in result.stderr and "错误" in result.stderr
    assert not any(call[1] == "bootstrap" for call in sandbox.calls("launchctl"))


@pytest.mark.parametrize("explicit", [False, True])
def test_reinstall_preserves_asr_unless_explicit(explicit, sandbox):
    """重复安装保留旧选择，显式切换时只改 ASR 字段。"""
    sandbox.mark_app(asr_model="sensevoice", daily_time="04:21")
    result = sandbox.run(INSTALL_SH, "--yes", *(["--asr", "whisper"] if explicit else []))
    assert result.returncode == 0, result.stdout + result.stderr
    settings = json.loads((sandbox.app / "settings.json").read_text())
    assert settings["asr_model"] == ("whisper" if explicit else "sensevoice")
    assert settings["daily_time"] == "04:21"


def test_local_source_passed_to_uv_is_absolute(sandbox):
    """本地来源按单个绝对路径参数交给 uv，不会被当作 PyPI 包名。"""
    result = sandbox.run(INSTALL_SH, "--yes")
    assert result.returncode == 0, result.stderr
    install = next(call for call in sandbox.calls("uv") if call[1:3] == ["pip", "install"])
    assert install[-1] == str(REPO_ROOT)
    assert "--only-binary" in install


@pytest.mark.parametrize("script", [INSTALL_SH, UNINSTALL_SH])
@pytest.mark.parametrize("launch_agent", [False, True])
def test_own_listener_stopped_before_install_or_delete(script, launch_agent, sandbox):
    """两种服务只处理当前安装的假进程，停止动作先于依赖更新或删除。"""
    sandbox.mark_app()
    sandbox.process(launch_agent=launch_agent)
    if launch_agent:
        plist = sandbox.home / "Library/LaunchAgents/io.github.xhs-wiki.service.plist"
        plist.parent.mkdir(parents=True)
        plist.write_bytes(plistlib.dumps({
            "Label": "io.github.xhs-wiki.service",
            "EnvironmentVariables": {"XHS_WIKI_HOME": str(sandbox.app)},
            "ProgramArguments": [str(sandbox.app / "venv/bin/xhs-wiki"), "serve"],
        }))
    result = sandbox.run(script, "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    records = sandbox.calls()
    stop = [call for call in records if call[0] == "kill" or call[:2] == ["launchctl", "bootout"]]
    assert stop and stop[0][0] == ("launchctl" if launch_agent else "kill")
    if script == INSTALL_SH:
        assert records.index(stop[0]) < next(i for i, call in enumerate(records)
                                             if call[:3] == ["uv", "pip", "install"])
    else:
        assert not sandbox.app.exists()


@pytest.mark.parametrize("script", [INSTALL_SH, UNINSTALL_SH])
def test_foreign_listener_never_killed_or_modified(script, sandbox):
    """别的程序占端口时不发送信号，也不开始安装或删除。"""
    sandbox.mark_app()
    sandbox.process(command="/generated/other/bin/server serve")
    before = sandbox.snapshot()
    result = sandbox.run(script, "--yes")
    assert result.returncode != 0 and "18765" in result.stderr
    assert sandbox.calls("kill") == [] and sandbox.calls("uv") == []
    assert not any(call[1] == "bootout" for call in sandbox.calls("launchctl"))
    assert sandbox.snapshot() == before


@pytest.mark.parametrize("script", [INSTALL_SH, UNINSTALL_SH])
def test_listener_with_physical_path_alias_is_still_owned(script, sandbox):
    """同一目录的符号链接别名不应被误判为另一个应用。"""
    sandbox.mark_app()
    alias = sandbox.root / "app-alias"
    alias.symlink_to(sandbox.app, target_is_directory=True)
    sandbox.process(command=f"{alias}/venv/bin/xhs-wiki serve")
    result = sandbox.run(script, "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert sandbox.calls("kill")


@pytest.mark.parametrize("script", [INSTALL_SH, UNINSTALL_SH])
def test_loaded_launch_agent_from_other_home_is_never_stopped(script, sandbox):
    """即使没有监听端口，也不能覆盖或停止属于另一安装目录的服务。"""
    sandbox.mark_app()
    foreign = sandbox.root / "foreign app"
    plist = sandbox.home / "Library/LaunchAgents/io.github.xhs-wiki.service.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({
        "Label": "io.github.xhs-wiki.service",
        "EnvironmentVariables": {"XHS_WIKI_HOME": str(foreign)},
        "ProgramArguments": [str(foreign / "venv/bin/xhs-wiki"), "serve"],
    }))
    (sandbox.root / "process.json").write_text(json.dumps({"listening": False, "launch_agent": True}))
    before = sandbox.snapshot()
    result = sandbox.run(script, "--yes")
    assert result.returncode != 0
    assert sandbox.snapshot() == before
    assert sandbox.calls("kill") == []
    assert not any(call[1] == "bootout" for call in sandbox.calls("launchctl"))


def test_open_failure_prints_recovery_command(sandbox):
    """浏览器入口失败时，提示可手动执行的新控制台命令。"""
    sandbox.env["Y1_TEST_OPEN_STATUS"] = "1"
    result = sandbox.run(INSTALL_SH, "--yes")
    assert "运行 xhs-wiki open 打开控制台" in result.stdout + result.stderr
    assert sandbox.calls("open") == []


def test_broken_venv_reports_rebuild_command(sandbox):
    """损坏环境不能被误报为网络故障，也不自动删除用户目录。

    install.sh 拒绝"非空且没有安装标记"的程序目录，所以
    这里先放好标记，模拟"之前装过、venv 后来坏了"的真实情形。
    """
    sandbox.mark_app()
    (sandbox.app / "venv/bin").mkdir(parents=True)
    result = sandbox.run(INSTALL_SH, "--yes")
    assert result.returncode != 0
    assert "venv" in result.stderr and "删除" in result.stderr and "重新安装" in result.stderr
    assert sandbox.calls("uv") == []


@pytest.mark.parametrize("script", [INSTALL_SH, UNINSTALL_SH])
def test_root_is_rejected_without_mutations(script, sandbox):
    """uid 零必须在所有安装和删除前拒绝。"""
    sandbox.env["Y1_TEST_UID"] = "0"
    before = sandbox.snapshot()
    result = sandbox.run(script, "--yes")
    assert result.returncode != 0
    assert "sudo" in result.stderr or "root" in result.stderr
    assert sandbox.snapshot() == before and sandbox.calls("launchctl") == []


@pytest.mark.parametrize("script", [INSTALL_SH, UNINSTALL_SH])
def test_no_tty_has_actionable_message(script, sandbox):
    """无控制终端时解释 --yes，不暴露 /dev/tty 原始设备错误。"""
    sandbox.mark_app()
    result = sandbox.run(script)
    output = result.stdout + result.stderr
    assert ("非交互" in output or "无法交互" in output) and "--yes" in output
    assert "Device not configured" not in output


def test_purge_removes_only_generated_entries_and_preserves_user_file(sandbox):
    """清库确认也只允许删除白名单，保留原有用户文件及根目录。"""
    kb = sandbox.knowledge_base(user_file=True)
    result = sandbox.run(UNINSTALL_SH, "--yes", "--purge-kb")
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in kb.iterdir()) == ["user-file.txt"]
    assert (kb / "user-file.txt").read_text() == "keep"


def test_purge_removes_empty_root_after_whitelist_cleanup(sandbox):
    """白名单清理后才允许删除空根目录。"""
    kb = sandbox.knowledge_base()
    result = sandbox.run(UNINSTALL_SH, "--yes", "--purge-kb")
    assert result.returncode == 0, result.stderr
    assert not kb.exists()


@pytest.mark.parametrize("missing", ["yes", "marker"])
def test_purge_requires_yes_and_marker(missing, sandbox):
    """独立清库意图和归属标记缺一不可，失败时保留整个安装。"""
    kb = sandbox.knowledge_base()
    if missing == "marker":
        (kb / ".xhs-wiki/generated.txt").unlink()
        (kb / ".xhs-wiki").rmdir()
    before = sandbox.snapshot()
    result = sandbox.run(UNINSTALL_SH, "--purge-kb", *([] if missing == "yes" else ["--yes"]))
    assert result.returncode != 0 and sandbox.snapshot() == before


def test_yes_without_purge_preserves_knowledge_base(sandbox):
    """一般卸载确认不会隐含授权删除知识库。"""
    kb = sandbox.knowledge_base()
    before = {str(p.relative_to(kb)) for p in kb.rglob("*")}
    result = sandbox.run(UNINSTALL_SH, "--yes")
    assert result.returncode == 0, result.stderr
    assert {str(p.relative_to(kb)) for p in kb.rglob("*")} == before


@pytest.mark.parametrize("location", ["home", "parent", "documents", "desktop", "library", "root", "users"])
def test_purge_refuses_protected_roots(location, sandbox):
    """保护路径即使有标记也不能清理；系统路径仅提供字符串不写入。"""
    choices = {"home": sandbox.home, "parent": sandbox.root,
               "documents": sandbox.home / "Documents", "desktop": sandbox.home / "Desktop",
               "library": sandbox.home / "Library", "root": Path("/"), "users": Path("/Users")}
    kb = choices[location]
    if location not in {"root", "users"}:
        (kb / ".xhs-wiki").mkdir(parents=True, exist_ok=True)
        (kb / "索引.md").write_text("keep", encoding="utf-8")
    sandbox.mark_app(kb_root=str(kb))
    before = sandbox.snapshot()
    result = sandbox.run(UNINSTALL_SH, "--yes", "--purge-kb")
    assert result.returncode != 0 and sandbox.snapshot() == before


def test_purge_expands_tilde_using_temporary_home(sandbox):
    """plutil 读出波浪线路径后，必须展开为隔离主目录。"""
    kb = sandbox.knowledge_base()
    sandbox.mark_app(kb_root="~/" + kb.name)
    result = sandbox.run(UNINSTALL_SH, "--yes", "--purge-kb")
    assert result.returncode == 0, result.stderr
    assert not kb.exists()


@pytest.mark.parametrize("symlink", ["root", "marker"])
def test_purge_does_not_follow_symlink_ownership(symlink, sandbox):
    """符号链接不能绕过受保护根目录及标记的归属检查。"""
    kb = sandbox.knowledge_base(user_file=True)
    if symlink == "root":
        alias = sandbox.home / "alias"
        (sandbox.home / ".xhs-wiki").mkdir()
        alias.symlink_to(sandbox.home, target_is_directory=True)
        sandbox.mark_app(kb_root=str(alias))
    else:
        marker = kb / ".xhs-wiki"
        marker.rename(sandbox.home / "external-marker")
        marker.symlink_to(sandbox.home / "external-marker", target_is_directory=True)
    before = sandbox.snapshot()
    result = sandbox.run(UNINSTALL_SH, "--yes", "--purge-kb")
    assert result.returncode != 0 and sandbox.snapshot() == before


def test_uninstall_refuses_unmarked_app_home(sandbox):
    """任意目录不能因为 --app-home 与 --yes 就被递归删除。"""
    sandbox.app.mkdir()
    (sandbox.app / "keep.txt").write_text("keep", encoding="utf-8")
    before = sandbox.snapshot()
    result = sandbox.run(UNINSTALL_SH, "--yes")
    assert result.returncode != 0 and sandbox.snapshot() == before
    assert sandbox.calls("kill") == []


def test_uninstall_does_not_need_python3_for_settings(sandbox):
    """系统 Python 占位程序不可用时，plutil 仍可找到并保留知识库。"""
    kb = sandbox.knowledge_base()
    python = sandbox.bin / "python3"
    python.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    python.chmod(0o700)
    result = sandbox.run(UNINSTALL_SH, "--yes")
    assert result.returncode == 0, result.stderr
    assert str(kb) in result.stdout and kb.exists()


@pytest.mark.parametrize("location", ["logs", "skill"])
def test_uninstall_refuses_symlink_auxiliary_directory(location, sandbox):
    """日志或 Skill 的链接目标可能是用户目录，卸载不能沿链接扩大删除。"""
    sandbox.mark_app()
    target = sandbox.home / "unrelated files"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    link = sandbox.logs if location == "logs" else sandbox.skills / "xhs-wiki"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    before = sandbox.snapshot()
    result = sandbox.run(UNINSTALL_SH, "--yes")
    assert result.returncode != 0
    assert sandbox.snapshot() == before


@pytest.mark.parametrize("location", ["app", "logs", "skill"])
def test_default_uninstall_preserves_nested_knowledge_base(location, sandbox):
    """知识库位于待删目录内时，宁可拒绝卸载，也不能违反默认保留承诺。"""
    parent = {"app": sandbox.app, "logs": sandbox.logs,
              "skill": sandbox.skills / "xhs-wiki"}[location]
    kb = parent / "generated knowledge"
    kb.mkdir(parents=True)
    (kb / "keep.txt").write_text("keep", encoding="utf-8")
    sandbox.mark_app(kb_root=str(kb))
    before = sandbox.snapshot()
    result = sandbox.run(UNINSTALL_SH, "--yes")
    assert result.returncode != 0
    assert sandbox.snapshot() == before
