"""Codex 提供方：通过 `codex exec` 调用用户登录的 Codex CLI。

在整体架构（docs/architecture.md §8）中，这是复用用户 ChatGPT/Codex 订阅
登录态的提供方，不需要 API Key。

和 claude_code 一样，子进程动作用可注入的 runner 执行，测试不跑真命令。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .base import LLMError, ProcessRunner, default_runner
from .claude_code import build_env
from .. import config

# Codex 的兜底安装位置（按顺序尝试，不存在的直接跳过）：
# 1. ChatGPT 桌面应用自带的 CLI（macOS 版装的最多）
# 2. 独立 Codex 应用自带的 CLI
# 3. 之外的 PATH 发现由 shutil.which 负责
_CODEX_FALLBACK_PATHS = (
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
    "/Applications/Codex.app/Contents/Resources/codex",
)

_NOT_LOGGED_IN_HINTS = ("not logged in", "未登录", "login required", "auth login")


def find_codex(explicit_path: str | None = None) -> str | None:
    """按顺序发现 codex 可执行文件：
    设置里指定的路径 → PATH → ChatGPT.app 内置 → Codex.app 内置（不存在就跳过）。
    """

    if explicit_path:
        return explicit_path
    found = shutil.which("codex")
    if found:
        return found
    for p in _CODEX_FALLBACK_PATHS:
        if Path(p).is_file():
            return p
    return None


class CodexProvider:
    """实现 Provider 协议的 Codex CLI 调用器。"""

    def __init__(
        self,
        *,
        model: str | None,
        explicit_path: str | None = None,
        runner: ProcessRunner | None = None,
    ) -> None:
        """model 允许为空（None / ""）：空时不传 -m，由 Codex 用用户自己
        配置的默认模型。"""

        self._runner: ProcessRunner = runner or default_runner
        # 旧向导把留空保存成字面量 default；它不是模型名，也应沿用
        # Codex 自身的默认配置。其余显式模型名保持原样传给 CLI。
        self._model = None if model in (None, "", "default") else model
        self._explicit_path = explicit_path
        self._exe = find_codex(explicit_path)

    def _require_exe(self) -> str:
        if self._exe is None:
            raise LLMError("unavailable", "codex_executable_not_found")
        return self._exe

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_output_tokens: int,
        timeout_s: float = 300,
    ) -> str:
        """发一次调用并返回模型文本。

        每个参数的依据（codex exec --help 原文，2026-10 版本实测）：
        - "exec"：Run Codex non-interactively —— 非交互模式
        - "--skip-git-repo-check"：Allow running Codex outside a Git repository
          —— 我们的运行目录不一定在 git 仓库里，必须带上
        - "--sandbox read-only"：Select the sandbox policy ... [possible values:
          read-only, workspace-write, danger-full-access] —— 只读沙箱，
          整理笔记不需要让模型执行任何写操作
        - "--ephemeral"：Run without persisting session files to disk
          —— 会话不落盘，知识库内容不留在 ~/.codex 里
        - "-o/--output-last-message <file>"：Specifies file where the last
          message from the agent should be written —— 模型的最终回复写进
          这个文件，比从混杂事件流的 stdout 里捞文本可靠
        - "-m/--model <model>"：Model the agent should use。不指定时 Codex
          会用用户自己配置的默认模型——所以 model 为空时这里也不传 -m。
        - "-"（位置参数）：'If not provided as an argument (or if `-` is
          used), instructions are read from stdin' —— 提示词从标准输入传入

        system 提示词没有独立参数，Codex 只有用户消息，所以把 system
        放在提示词开头，用分隔线区分。

        工作目录：统一用 APP_HOME/llm-workdir/ 这个专用空目录，
        双保险地传两遍——
        - 子进程的 cwd（Runner 的 cwd 参数）：codex 读当前目录里的
          AGENTS.md 等项目说明，也可能受目录权限影响；
        - "-C <目录>"：codex 自己的"切换工作目录"参数，保证它内部
          定义的"项目目录"也是这个空目录，而不是启动目录。
        """

        exe = self._require_exe()
        # -o 要写到一个我们控制的临时文件；调用完读取再删除。
        # 用 NamedTemporaryFile 拿一个唯一的路径（delete=False，
        # 由 codex 进程负责写入，我们稍后读取并清理）。
        fd, out_path = tempfile.mkstemp(prefix="xhs-wiki-codex-")
        os.close(fd)
        try:
            workdir = str(config.llm_work_dir())
            argv = [
                exe,
                "-C", workdir,
                "exec",
                "--skip-git-repo-check",
                "--sandbox", "read-only",
                "--ephemeral",
                "-o", out_path,
            ]
            if self._model:
                argv += ["-m", self._model]
            argv.append("-")
            full_prompt = f"{system}\n---\n{prompt}" if system else prompt
            result = self._runner(
                argv, env=build_env(), input=full_prompt, timeout_s=timeout_s,
                cwd=workdir,
            )
            return self._parse_result(result, out_path)
        finally:
            # 无论成败都清掉临时文件，里面可能有模型输出（知识库内容）
            try:
                os.unlink(out_path)
            except OSError:
                pass

    def _parse_result(self, result, out_path: str) -> str:
        if result.timed_out:
            raise LLMError("timeout", "codex_timeout")
        if result.returncode != 0:
            blob = (result.stderr + "\n" + result.stdout).lower()
            if any(h in blob for h in _NOT_LOGGED_IN_HINTS):
                raise LLMError("not_logged_in", "codex_not_logged_in")
            if "rate limit" in blob or "quota" in blob:
                raise LLMError("quota_or_rate_limit", "codex_rate_limited")
            raise LLMError("unavailable", f"codex_exit_{result.returncode}")
        try:
            text = Path(out_path).read_text(encoding="utf-8")
        except OSError:
            # 命令成功但没写出 -o 文件：结果无法获取
            raise LLMError("bad_output", "codex_output_file_missing") from None
        if not text.strip():
            raise LLMError("bad_output", "codex_output_empty")
        return text

    def check(self) -> tuple[bool, str]:
        """用 `codex login status` 判断是否已登录（不发起会消耗用量的调用）。
        工作目录同样用专用空目录，避免无权限目录让命令卡到超时。"""

        exe = self._exe
        if exe is None:
            return (False, "codex_executable_not_found")
        result = self._runner(
            [exe, "login", "status"],
            env=build_env(),
            input="",
            timeout_s=60,
            cwd=str(config.llm_work_dir()),
        )
        if result.timed_out:
            return (False, "codex_login_status_timeout")
        blob = (result.stdout + "\n" + result.stderr).lower()
        if result.returncode == 0 and "not logged in" not in blob:
            return (True, "codex_logged_in")
        return (False, "codex_not_logged_in")
