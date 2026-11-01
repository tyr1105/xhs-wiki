"""API Key 的钥匙串存取：包装 macOS 自带的 `security` 命令。

在整体架构（docs/architecture.md §7）中，API Key 不进 settings.json，
统一存进 macOS 钥匙串：服务名固定为 "xhs-wiki"，账户名为提供方名称
（如 "anthropic"、"openai_compatible"）。

Key 不出现在命令行参数里的依据（security 帮助原文）：
- add-generic-password："Use of the -p or -w options is insecure.
  Specify -w as the last option to be prompted." —— `-w` 作为最后一个
  选项且不带值时，security 会进入交互式提示读密码；实测（macOS 15）
  在无终端交互、stdin 是管道时同样从标准输入读取，因此我们把 Key
  通过子进程的标准输入喂给 `-w`，命令行参数里只有账户名和服务名。
- find-generic-password 的 `-w`："Display only the password on stdout"
  —— 只把密码打到标准输出，用 capture_output 收下，不会进命令行。

执行 `security` 的动作被抽象成可注入的 runner（和 base.ProcessRunner
同样的签名），测试里换成假替身，完全不碰真实钥匙串。
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import LLMError, ProcessRunner, default_runner

SERVICE_NAME = "xhs-wiki"  # 钥匙串里的服务名，全项目统一

# security 是系统自带、路径固定的命令，一律用绝对路径调用，
# 不再按 PATH 查找——服务的 PATH 把 ~/.local/bin 和 Homebrew 目录排在
# /usr/bin 前面，那些目录里出现同名命令时，Key 会被写进它的标准输入。
SECURITY_BIN = "/usr/bin/security"


def _security_path() -> str:
    """返回 security 命令的绝对路径（固定 /usr/bin/security）。

    万一这个路径不存在（非标准系统），按环境问题处理报错。
    """

    if not Path(SECURITY_BIN).exists():
        # 理论上不会发生；真发生时按环境问题处理
        raise LLMError("unavailable", "security_command_not_found")
    return SECURITY_BIN


class Keychain:
    """读写钥匙串里 xhs-wiki 服务的 API Key。"""

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        # runner 可注入：测试传假替身，不碰真实钥匙串
        self._runner: ProcessRunner = runner or default_runner

    def save(self, account: str, key: str) -> None:
        """把 Key 存进（或更新）钥匙串。

        命令：security add-generic-password -U -a <account> -s xhs-wiki -w
        -U（帮助原文 "Update item if it already exists"）保证重复保存是更新
        而不是报错；-w 放在最后且不带值（见文件顶部的依据），
        Key 通过标准输入传给 security，绝不进 argv。
        """

        argv = [
            _security_path(),
            "add-generic-password",
            "-U",
            "-a", account,
            "-s", SERVICE_NAME,
            "-w",
        ]
        # 子进程继承的环境足够 security 找到默认钥匙串；显式传 os.environ
        result = self._runner(argv, env=dict(os.environ), input=key, timeout_s=30)
        if result.timed_out:
            raise LLMError("timeout", "keychain_save_timeout")
        if result.returncode != 0:
            # 只报告退出码，不透出 stderr（理论上也不含 Key，但保守起见）
            raise LLMError("unavailable", f"keychain_save_failed_{result.returncode}")

    def load(self, account: str) -> str:
        """从钥匙串读出 Key；条目不存在时抛 LLMError(auth_failed)。

        命令：security find-generic-password -a <account> -s xhs-wiki -w
        -w 表示只把密码打到标准输出（帮助原文见文件顶部）。
        """

        argv = [
            _security_path(),
            "find-generic-password",
            "-a", account,
            "-s", SERVICE_NAME,
            "-w",
        ]
        result = self._runner(argv, env=dict(os.environ), input="", timeout_s=30)
        if result.timed_out:
            raise LLMError("timeout", "keychain_load_timeout")
        if result.returncode != 0:
            # 常见原因是"找不到该条目"（退出码 44）。统一归到 auth_failed，
            # 由上层提示用户先填 Key。
            raise LLMError("auth_failed", f"keychain_item_missing_{result.returncode}")
        # security 输出密码后带一个换行，去掉首尾空白
        key = result.stdout.strip()
        if not key:
            raise LLMError("auth_failed", "keychain_empty_password")
        return key

    def delete(self, account: str) -> None:
        """删除钥匙串里的条目（用户清除配置时用）。

        命令：security delete-generic-password -a <account> -s xhs-wiki
        """

        argv = [
            _security_path(),
            "delete-generic-password",
            "-a", account,
            "-s", SERVICE_NAME,
        ]
        result = self._runner(argv, env=dict(os.environ), input="", timeout_s=30)
        if result.timed_out:
            raise LLMError("timeout", "keychain_delete_timeout")
        if result.returncode != 0:
            # 删除不存在的条目视为成功（幂等）
            raise LLMError("unavailable", f"keychain_delete_failed_{result.returncode}")
