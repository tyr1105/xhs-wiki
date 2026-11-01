# -*- coding: utf-8 -*-
"""安装 / 卸载脚本安全与健壮性修复的回归测试。

每个测试对应一条已确认的问题（测试名和说明里写了问题要点）：
- 没有终端又没加 --yes：任何副作用之前就停下；停掉旧服务后再失败要给恢复办法；
- 受保护目录按"磁盘身份"比较：大小写不同、/System/Volumes/Data 别名都绕不过去；
  install.sh 校验 --app-home，不往主目录或别人的目录里写东西；
- 卸载：--app-home 写错时什么都不删；日志和 Skill 目录只删本工具的文件；
  确认前列出将要删除的内容；缺安装标记时给出出路；
- 重装时选择不装 LaunchAgent 会删掉旧 plist；别的安装目录的 plist 没加载也不覆盖；
- bash 3.2 下带单引号的路径能正确转义；导出 CDPATH 不影响 --from-local；
  安装时当前目录里与标准库同名的 .py 文件不会被执行。

沿用 test_install_real_run.py 的 ScriptSandbox：HOME、程序目录、日志、Skill
都在 pytest 的临时目录里；uv、launchctl、curl、kill 等全是记录参数的替身；
不访问网络、不执行真实 launchctl、不碰真实主目录。
"""

import json
import os
import plistlib
import re
import subprocess
from pathlib import Path

import pytest

from test_install_real_run import INSTALL_SH, REPO_ROOT, UNINSTALL_SH, ScriptSandbox

PLIST_NAME = "io.github.xhs-wiki.service.plist"


@pytest.fixture
def sandbox(tmp_path):
    """每个测试一个全新的隔离环境。"""
    return ScriptSandbox(tmp_path)


def case_variant(path: Path) -> Path:
    """把路径最后一级换成大小写不同的写法（Documents → dOCUMENTS）。"""
    return path.parent / path.name.swapcase()


def case_insensitive(path: Path) -> bool:
    """当前文件系统是否不区分大小写（macOS 默认的 APFS 不区分）。"""
    variant = case_variant(path)
    return variant != path and variant.exists() and os.path.samefile(variant, path)


def data_volume_alias(path: Path) -> Path:
    """/System/Volumes/Data 前缀的别名写法（macOS 的固件链接）。"""
    return Path("/System/Volumes/Data" + str(path.resolve()))


def alias_works(alias: Path, real: Path) -> bool:
    """别名写法确实指向同一个目录时才跑相关测试，否则跳过。"""
    try:
        return os.path.samefile(alias, real)
    except OSError:
        return False


def write_plist(sandbox, *, home, program=None):
    """写一份 LaunchAgent 配置（虚构内容），返回它的路径。"""
    plist = sandbox.home / "Library/LaunchAgents" / PLIST_NAME
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({
        "Label": "io.github.xhs-wiki.service",
        "EnvironmentVariables": {"XHS_WIKI_HOME": str(home)},
        "ProgramArguments": [program or str(Path(home) / "venv/bin/xhs-wiki"), "serve"],
        "RunAtLoad": True,
    }))
    return plist


# ---------------------------------------------------------------------------
# 无终端且未加 --yes
# ---------------------------------------------------------------------------

def test_no_tty_without_yes_stops_before_any_side_effect(sandbox):
    """无终端、无 --yes：不 bootout、不 kill、不调用 uv、不建任何目录。"""
    sandbox.mark_app()
    sandbox.process(launch_agent=True)
    write_plist(sandbox, home=sandbox.app)
    before = sandbox.snapshot()
    result = sandbox.run(INSTALL_SH)
    assert result.returncode != 0
    assert "non_interactive" in result.stderr and "--yes" in result.stderr
    assert not any(call[1] == "bootout" for call in sandbox.calls("launchctl"))
    assert sandbox.calls("kill") == [] and sandbox.calls("uv") == []
    assert sandbox.snapshot() == before


def test_no_tty_piped_install_creates_nothing(sandbox):
    """curl | bash 方式（bash -s）同样在建目录、建 venv 之前就停下。"""
    result = sandbox.run(INSTALL_SH, piped=True)
    assert result.returncode != 0
    assert not sandbox.app.exists()
    assert sandbox.calls("uv") == []


@pytest.mark.parametrize("launch_agent", [True, False])
def test_failure_after_stopping_old_service_explains_recovery(launch_agent, sandbox):
    """停掉旧服务之后依赖安装失败：提示里说清旧服务已停、怎么恢复。"""
    sandbox.mark_app()
    sandbox.process(launch_agent=launch_agent)
    if launch_agent:
        write_plist(sandbox, home=sandbox.app)
    sandbox.env["Y1_TEST_INSTALL_STATUS"] = "1"
    result = sandbox.run(INSTALL_SH, "--yes")
    assert result.returncode != 0
    assert "旧的" in result.stderr and "已经停止" in result.stderr
    assert ("launchctl bootstrap" if launch_agent else "serve") in result.stderr


# ---------------------------------------------------------------------------
# 受保护目录按磁盘身份比较；install.sh 校验 --app-home
# ---------------------------------------------------------------------------

def test_purge_refuses_case_variant_of_documents(sandbox):
    """kb_root 写成 ~/dOCUMENTS（大小写不同）也必须拒绝清理。"""
    documents = sandbox.home / "Documents"
    (documents / ".xhs-wiki").mkdir(parents=True)
    (documents / "主题" / "我自己的旧笔记").mkdir(parents=True)
    (documents / "主题" / "我自己的旧笔记" / "note.md").write_text("keep", encoding="utf-8")
    variant = case_variant(documents)
    if not case_insensitive(documents):
        pytest.skip("文件系统区分大小写，这种写法本来就不是同一个目录")
    sandbox.mark_app(kb_root=str(variant))
    before = sandbox.snapshot()
    result = sandbox.run(UNINSTALL_SH, "--yes", "--purge-kb")
    assert result.returncode != 0
    assert "kb_root_unsafe" in result.stderr
    assert sandbox.snapshot() == before


def test_purge_refuses_data_volume_alias_of_documents(sandbox):
    """kb_root 写成 /System/Volumes/Data/... 别名也必须拒绝清理。"""
    documents = sandbox.home / "Documents"
    (documents / ".xhs-wiki").mkdir(parents=True)
    (documents / "索引.md").write_text("keep", encoding="utf-8")
    alias = data_volume_alias(documents)
    if not alias_works(alias, documents):
        pytest.skip("这台机器没有 /System/Volumes/Data 别名")
    sandbox.mark_app(kb_root=str(alias))
    before = sandbox.snapshot()
    result = sandbox.run(UNINSTALL_SH, "--yes", "--purge-kb")
    assert result.returncode != 0
    assert sandbox.snapshot() == before


def test_install_refuses_home_as_app_home(sandbox):
    """--app-home 指向主目录：拒绝，主目录权限不变、不写标记和 venv。"""
    mode = sandbox.home.stat().st_mode & 0o777
    sandbox.app = sandbox.home
    result = sandbox.run(INSTALL_SH, "--yes")
    assert result.returncode != 0
    assert "app_home_unsafe" in result.stderr
    assert sandbox.home.stat().st_mode & 0o777 == mode
    assert not (sandbox.home / ".xhs-wiki-install").exists()
    assert not (sandbox.home / "venv").exists()
    assert sandbox.calls("uv") == []


def test_install_refuses_nonempty_unmarked_app_home(sandbox):
    """已有内容、又没有安装标记的目录不能当程序目录（不往别人的目录里写）。"""
    sandbox.app.mkdir(parents=True)
    (sandbox.app / "我的文件.txt").write_text("keep", encoding="utf-8")
    before = sandbox.snapshot()
    result = sandbox.run(INSTALL_SH, "--yes")
    assert result.returncode != 0
    assert "app_home_not_empty" in result.stderr and "rm -rf" in result.stderr
    assert sandbox.snapshot() == before


def test_install_accepts_pre_marker_install_and_adds_marker(sandbox):
    """加标记之前的旧版安装（有 settings.json）可以重装，重装后补上标记。"""
    sandbox.app.mkdir(parents=True)
    (sandbox.app / "settings.json").write_text(json.dumps({"kb_root": "~/XHS-Wiki"}),
                                              encoding="utf-8")
    result = sandbox.run(INSTALL_SH, "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (sandbox.app / ".xhs-wiki-install").is_file()


def test_uninstall_refuses_case_variant_of_home(sandbox):
    """主目录里被（错误地）写了安装标记，大写写法的 --app-home 也不能删主目录。"""
    (sandbox.home / ".xhs-wiki-install").write_text("xhs-wiki\n", encoding="utf-8")
    (sandbox.home / "Documents").mkdir()
    (sandbox.home / "Documents" / "thesis.txt").write_text("keep", encoding="utf-8")
    variant = case_variant(sandbox.home)
    if not case_insensitive(sandbox.home):
        pytest.skip("文件系统区分大小写")
    sandbox.app = variant
    before = sandbox.snapshot()
    result = sandbox.run(UNINSTALL_SH, "--yes")
    assert result.returncode != 0
    assert "app_home_unsafe" in result.stderr
    assert sandbox.snapshot() == before


# ---------------------------------------------------------------------------
# 卸载只删本工具的东西，--app-home 写错时什么都不删
# ---------------------------------------------------------------------------

def _install_shared_files(sandbox):
    """正式安装在用的日志和 Skill（虚构内容）。"""
    sandbox.logs.mkdir(parents=True)
    (sandbox.logs / "daemon.log").write_text("generated log", encoding="utf-8")
    skill = sandbox.skills / "xhs-wiki"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("generated skill", encoding="utf-8")


def test_uninstall_with_mistyped_app_home_deletes_nothing(sandbox):
    """--app-home 拼错（目录不存在）：报错退出，日志和 Skill 原样保留。"""
    _install_shared_files(sandbox)
    sandbox.app = sandbox.home / "app dat"   # 少了一个字母
    before = sandbox.snapshot()
    result = sandbox.run(UNINSTALL_SH, "--yes")
    assert result.returncode != 0
    assert "app_home_missing" in result.stderr
    assert sandbox.snapshot() == before


def test_uninstall_log_dir_keeps_other_programs_logs(sandbox):
    """日志目录指到公共目录时，只删本工具的日志文件，别的程序的日志保留。"""
    sandbox.mark_app()
    shared = sandbox.home / "Library" / "Logs"
    (shared / "OtherApp").mkdir(parents=True)
    (shared / "OtherApp" / "important.log").write_text("keep", encoding="utf-8")
    (shared / "daemon.log").write_text("ours", encoding="utf-8")
    sandbox.env["XHS_WIKI_LOG_DIR"] = str(shared)
    result = sandbox.run(UNINSTALL_SH, "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (shared / "OtherApp" / "important.log").read_text(encoding="utf-8") == "keep"
    assert not (shared / "daemon.log").exists()


def test_uninstall_skill_dir_keeps_user_files(sandbox):
    """Skill 目录里只删 SKILL.md；用户放的其他文件和目录本身保留。"""
    sandbox.mark_app()
    _install_shared_files(sandbox)
    (sandbox.skills / "xhs-wiki" / "我的笔记.md").write_text("keep", encoding="utf-8")
    result = sandbox.run(UNINSTALL_SH, "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (sandbox.skills / "xhs-wiki" / "SKILL.md").exists()
    assert (sandbox.skills / "xhs-wiki" / "我的笔记.md").exists()
    # 日志目录只剩本工具的日志，删完后目录本身也删掉。
    assert not sandbox.logs.exists()


def test_uninstall_lists_everything_and_asks_even_without_app_home(sandbox):
    """程序目录不存在时也要确认；无终端时取消，什么都不删。"""
    _install_shared_files(sandbox)
    sandbox.env["XHS_WIKI_HOME"] = str(sandbox.app)

    before = sandbox.snapshot()
    command = ["/bin/bash", str(UNINSTALL_SH)]   # 不显式传 --app-home
    result = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT,
                            env=sandbox.env, timeout=120, start_new_session=True)
    output = result.stdout + result.stderr
    assert "将要删除" in output and "SKILL.md" in output and "daemon.log" in output
    assert "已取消卸载" in output
    assert sandbox.snapshot() == before


def test_uninstall_dry_run_lists_paths(sandbox):
    """预览里列出将要删除的程序目录、日志和 Skill。"""
    sandbox.mark_app()
    _install_shared_files(sandbox)
    result = sandbox.run(UNINSTALL_SH, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "将要删除" in result.stdout
    for text in (str(sandbox.app), "daemon.log", "SKILL.md"):
        assert text in result.stdout


def test_uninstall_unmarked_app_home_explains_way_out(sandbox):
    """缺安装标记时给出出路：重跑 install.sh 补标记，或手动删除的完整命令。"""
    (sandbox.app / "venv").mkdir(parents=True)
    result = sandbox.run(UNINSTALL_SH, "--yes")
    assert result.returncode != 0
    assert "app_home_marker_missing" in result.stderr
    assert "install.sh" in result.stderr and "rm -rf --" in result.stderr


# ---------------------------------------------------------------------------
# LaunchAgent 配置的去留与归属
# ---------------------------------------------------------------------------

def test_reinstall_without_launch_agent_removes_own_plist(sandbox):
    """重装时选择不装 LaunchAgent：旧的、属于本目录的 plist 被删掉。"""
    sandbox.mark_app()
    plist = write_plist(sandbox, home=sandbox.app)
    (sandbox.root / "process.json").write_text(json.dumps({"listening": False,
                                                           "launch_agent": True}))
    result = sandbox.run(INSTALL_SH, "--yes", "--no-launch-agent")
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(call[1] == "bootout" for call in sandbox.calls("launchctl"))
    assert not plist.exists()


def test_unloaded_plist_of_other_home_is_never_overwritten(sandbox):
    """另一安装目录的 plist 即使没加载，也不能被覆盖。"""
    other = sandbox.home / "Library/Application Support/xhs-wiki"
    plist = write_plist(sandbox, home=other)
    original = plist.read_bytes()
    result = sandbox.run(INSTALL_SH, "--yes")
    assert result.returncode != 0
    assert "launch_agent_home_mismatch" in result.stderr
    assert plist.read_bytes() == original
    assert not any(call[1] == "bootstrap" for call in sandbox.calls("launchctl"))


# ---------------------------------------------------------------------------
# 引号、CDPATH、当前目录里的同名 .py 文件
# ---------------------------------------------------------------------------

def _command_after(marker: str, text: str) -> str:
    """从输出里取出某个提示后面的整条命令。"""
    line = next(line for line in text.splitlines() if marker in line)
    return line.split(marker, 1)[1].strip()


def test_single_quote_path_is_quoted_for_bash_32(sandbox):
    """程序目录带单引号：打印的恢复命令在 macOS 自带的 bash 3.2 下能原样还原。"""
    sandbox.app = sandbox.home / "Bob's app"
    sandbox.env["Y1_TEST_OPEN_STATUS"] = "1"
    result = sandbox.run(INSTALL_SH, "--yes")
    command = _command_after("完整命令：", result.stdout + result.stderr)
    checked = subprocess.run(["/bin/bash", "-n", "-c", command], capture_output=True,
                             text=True, env=sandbox.env)
    assert checked.returncode == 0, checked.stderr
    # 把命令里的每个参数逐个打印出来，第二个参数（XHS_WIKI_HOME=…）要原样还原。
    echoed = subprocess.run(["/bin/bash", "-c", "printf '%s\\n' " + command],
                            capture_output=True, text=True, env=sandbox.env)
    assert f"XHS_WIKI_HOME={sandbox.app}" in echoed.stdout.splitlines()


def test_uninstall_echo_quotes_single_quote_path(sandbox):
    """卸载的命令回显（stderr）里，带单引号的路径同样能被 bash 3.2 还原。"""
    sandbox.app = sandbox.home / "Bob's app"
    sandbox.mark_app()
    result = sandbox.run(UNINSTALL_SH, "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    line = next(line for line in result.stderr.splitlines() if "rm" in line and "Bob" in line)
    command = line.strip().lstrip("$").strip()
    echoed = subprocess.run(["/bin/bash", "-c", "printf '%s\\n' " + command],
                            capture_output=True, text=True, env=sandbox.env)
    assert str(sandbox.app) in echoed.stdout.splitlines()


def test_cdpath_does_not_redirect_from_local(sandbox):
    """导出了 CDPATH 时，--from-local 的相对路径仍指向当前目录下的仓库。"""
    decoy = sandbox.root / "cdpath" / REPO_ROOT.name
    decoy.mkdir(parents=True)
    env = dict(sandbox.env, CDPATH=str(decoy.parent))
    command = ["/bin/bash", str(INSTALL_SH), "--app-home", str(sandbox.app), "--yes",
               "--from-local", REPO_ROOT.name]
    result = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT.parent,
                            env=env, timeout=120, start_new_session=True)
    assert result.returncode == 0, result.stdout + result.stderr
    install = next(call for call in sandbox.calls("uv") if call[1:3] == ["pip", "install"])
    assert install[-1] == str(REPO_ROOT)


def test_shadow_stdlib_module_in_cwd_is_not_executed(sandbox):
    """当前目录里有 plistlib.py：安装时不会被导入执行（python 调用都带 -P）。"""
    workdir = sandbox.root / "cwd with shadow"
    workdir.mkdir()
    (workdir / "plistlib.py").write_text("print('SHADOWED: cwd plistlib.py imported')\n",
                                         encoding="utf-8")
    command = ["/bin/bash", str(INSTALL_SH), "--app-home", str(sandbox.app), "--yes",
               "--from-local", str(REPO_ROOT)]
    result = subprocess.run(command, capture_output=True, text=True, cwd=workdir,
                            env=sandbox.env, timeout=120, start_new_session=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SHADOWED" not in result.stdout + result.stderr


def test_install_python_calls_all_use_safe_path_flag():
    """静态检查：install.sh 里每一处直接执行 "$VENV_PYTHON" 的地方都带 -P。

    只看"后面紧跟一个以 - 开头的参数"的出现（也就是真正在执行解释器）；
    作为 uv 的 --python 参数值出现的不算。
    """
    source = INSTALL_SH.read_text(encoding="utf-8")
    flags = re.findall(r'(?<!--python )"\$VENV_PYTHON" (-\S*)', source)
    assert len(flags) >= 5, flags
    assert all(flag == "-P" for flag in flags), flags


def test_scripts_have_no_unbraced_variable_before_multibyte_text():
    """bash 3.2 在 UTF-8 环境下会把紧跟变量名的中文字节当成名字的一部分。"""
    pattern = re.compile(rb"\$[A-Za-z_][A-Za-z0-9_]*[\x80-\xff]")
    for script in (INSTALL_SH, UNINSTALL_SH):
        assert not pattern.search(script.read_bytes()), script.name

