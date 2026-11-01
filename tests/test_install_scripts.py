# -*- coding: utf-8 -*-
"""安装脚本的语法、预览和截断保护测试。

这个文件检查脚本面向用户的预览行为；实际执行分支的替身在
test_install_real_run.py 中。所有路径都来自临时目录，不扫描真实主目录。
"""

import os
import subprocess

import pytest

from test_install_real_run import INSTALL_SH, UNINSTALL_SH, ScriptSandbox


@pytest.fixture
def sandbox(tmp_path):
    """每个测试独立准备主目录与外部命令，避免并发测试互相污染。"""
    return ScriptSandbox(tmp_path)


@pytest.mark.parametrize("script", [INSTALL_SH, UNINSTALL_SH])
def test_scripts_are_executable_and_pass_bash_syntax(script, sandbox):
    """验证用户能够执行脚本，以及 Bash 能完整解析文件。"""
    assert os.access(script, os.X_OK)
    result = subprocess.run(["/bin/bash", "-n", str(script)], env=sandbox.env,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script,total", [(INSTALL_SH, 9), (UNINSTALL_SH, 5)])
def test_dry_run_prints_all_steps_without_file_changes(script, total, sandbox):
    """预览只比较临时 HOME 下的文件，不再读取真实 Claude 等目录。"""
    before = sandbox.snapshot()
    result = sandbox.run(script, "--dry-run", "--yes")
    assert result.returncode == 0, result.stderr
    for number in range(1, total + 1):
        assert f"步骤 {number}/{total}" in result.stdout
    assert "dry-run 结束" in result.stdout
    assert sandbox.snapshot() == before
    assert not sandbox.app.exists()


@pytest.mark.parametrize("remote", [False, True])
def test_dry_run_requires_binary_wheels_for_local_and_remote(remote, sandbox):
    """两种本体来源都必须禁止指定原生依赖转为源码编译。"""
    sandbox.env["XHS_WIKI_REPO"] = "example.invalid/generated/xhs-wiki"
    result = sandbox.run(INSTALL_SH, "--dry-run", "--yes", local=not remote)
    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    expected = ("av,ctranslate2,onnxruntime,numpy,tokenizers,sherpa-onnx,"
                "cryptography,lxml,curl-cffi,pydantic-core,pyobjc-core")
    assert "--only-binary" in output
    assert expected in output
    assert ("https://example.invalid/generated/xhs-wiki/archive/refs/heads/main.tar.gz" if remote
            else str(sandbox.repo)) in output


def test_dry_run_uses_authenticated_open_and_healthz(sandbox):
    """展示新登录入口及不走代理的检查，第八步不再等待终端回答。"""
    result = sandbox.run(INSTALL_SH, "--dry-run", "--yes")
    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    assert "xhs-wiki" in output and " open" in output
    assert "open http://127.0.0.1" not in output
    assert "/healthz" in output and "--noproxy" in output and "60 秒" in output
    assert result.stdout.index("步骤 5/9") < result.stdout.index("步骤 6/9")
    assert "运行 xhs-wiki open 打开控制台" in output
    assert "将询问" not in result.stdout.split("步骤 8/9", 1)[1]


def test_dry_run_no_open_skips_browser(sandbox):
    """--no-open：照常启动服务，但不运行 xhs-wiki open，只提示用户之后自己打开。"""
    result = sandbox.run(INSTALL_SH, "--dry-run", "--yes", "--no-open")
    assert result.returncode == 0, result.stderr
    step5 = result.stdout.split("步骤 5/9", 1)[1].split("步骤 6/9", 1)[0]
    assert "--no-open：不自动打开浏览器" in step5
    # 预览里每条将要执行的命令以 "$ " 开头；--no-open 时第 5 步不能出现 open 命令。
    assert not [line for line in step5.splitlines() if line.strip().startswith("$") and line.rstrip().endswith(" open")]
    assert "运行 xhs-wiki open 打开控制台" in result.stdout


def test_dry_run_no_launch_agent_uses_nohup(sandbox):
    """不设开机自启时，预览说明临时后台服务及重启后的操作。"""
    result = sandbox.run(INSTALL_SH, "--dry-run", "--yes", "--no-launch-agent")
    assert result.returncode == 0, result.stderr
    assert "nohup" in result.stdout + result.stderr
    assert "重启电脑后需要手动运行 xhs-wiki serve" in result.stdout


def test_remote_dry_run_uses_release_archive_without_external_media_tools(sandbox):
    """默认远程安装直接使用发布源码包，流程不再要求外部媒体工具。"""
    result = sandbox.run(INSTALL_SH, "--dry-run", "--yes", local=False)
    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    assert "https://github.com/tyr1105/xhs-wiki/archive/refs/heads/main.tar.gz" in output
    assert "ffmpeg" not in output and "Homebrew" not in output and "git+" not in output


def test_remote_dry_run_allows_repo_and_branch_override(sandbox):
    """仓库与分支分别来自环境变量，不再拼接旧的版本控制安装协议。"""
    sandbox.env.update(XHS_WIKI_REPO="example.invalid/generated/repository",
                       XHS_WIKI_REF="generated-branch")
    result = sandbox.run(INSTALL_SH, "--dry-run", "--yes", local=False)
    assert result.returncode == 0, result.stderr
    assert ("https://example.invalid/generated/repository/archive/refs/heads/"
            "generated-branch.tar.gz") in result.stderr


def test_cn_mirror_dry_run_prints_variables_and_github_requirement(sandbox):
    """预览说明镜像的覆盖范围与仍需访问的源码站点，且不创建文件。"""
    before = sandbox.snapshot()
    result = sandbox.run(INSTALL_SH, "--dry-run", "--yes", "--mirror", "cn", local=False)
    assert result.returncode == 0, result.stderr
    assert "export UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/" in result.stdout
    assert ("export UV_PYTHON_INSTALL_MIRROR="
            "https://registry.npmmirror.com/-/binary/python-build-standalone") in result.stdout
    assert "export HF_ENDPOINT=https://hf-mirror.com" in result.stdout
    assert "安装脚本本身和源码包仍然从 GitHub 下载，需要能访问 GitHub" in result.stdout
    assert sandbox.snapshot() == before


@pytest.mark.parametrize("arguments", [("--mirror",), ("--mirror", "unknown")])
def test_mirror_invalid_arguments_fail_before_mutation(arguments, sandbox):
    """缺失或未知的镜像参数在任何安装动作之前报错。"""
    before = sandbox.snapshot()
    result = sandbox.run(INSTALL_SH, "--yes", *arguments, local=False)
    assert result.returncode != 0 and "mirror" in result.stderr
    assert sandbox.snapshot() == before
    assert sandbox.calls("uv") == [] and sandbox.calls("launchctl") == []


@pytest.mark.parametrize("script,total", [(INSTALL_SH, 9), (UNINSTALL_SH, 5)])
def test_piped_script_completes_without_consuming_stdin(script, total, sandbox):
    """文件经标准输入送给 Bash，模拟下载管道而不启动网络工具。"""
    before = sandbox.snapshot()
    result = sandbox.run(script, "--dry-run", "--yes", piped=True)
    assert result.returncode == 0, result.stderr
    assert f"步骤 {total}/{total}" in result.stdout
    assert sandbox.snapshot() == before


@pytest.mark.parametrize("script", [INSTALL_SH, UNINSTALL_SH])
def test_truncated_script_never_starts_operations(script, sandbox):
    """缺最后 main 调用的脚本只有定义，不会执行半套安装或卸载。"""
    source = script.read_text(encoding="utf-8")
    assert source.rstrip().splitlines()[-1] == 'main "$@"'
    before = sandbox.snapshot()
    result = sandbox.run(script, "--yes", source=source[:source.rfind('main "$@"')])
    assert result.returncode == 0, result.stderr
    assert "步骤 " not in result.stdout
    assert sandbox.snapshot() == before
    assert sandbox.calls() == []


def test_uninstall_dry_run_reads_kb_before_deleting_app(sandbox):
    """临时知识库路径须出现在预览，设置及标记文件保持原样。"""
    kb = sandbox.root / "generated wiki"
    sandbox.mark_app(kb_root=str(kb))
    before = sandbox.snapshot()
    result = sandbox.run(UNINSTALL_SH, "--dry-run", "--yes")
    assert result.returncode == 0, result.stderr
    assert str(kb) in result.stdout
    assert sandbox.snapshot() == before


def test_uninstall_dry_run_reports_unknown_kb(sandbox):
    """没有设置时须明确路径尚未确认，而非声称已读取配置。"""
    result = sandbox.run(UNINSTALL_SH, "--dry-run", "--yes")
    assert result.returncode == 0, result.stderr
    assert "知识库位置未知" in result.stdout
