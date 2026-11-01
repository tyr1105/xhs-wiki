"""模型调用层的公共基础：Provider 协议和统一的错误类型。

整个项目里，引擎（engines/）不关心用户选的是 Claude Code、Codex、Anthropic API
还是 OpenAI 兼容接口；它只拿这里的 `Provider` 协议干活。每个提供方模块
（claude_code / codex / anthropic_api / openai_compatible）都实现这两个方法，
由 llm/__init__.py 的 make_provider() 按设置实例化。

在整体架构（docs/architecture.md §8）中，本文件是模型调用层的接口契约。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol


# 错误代码集合。提供方模块抛 LLMError 时必须从这里选一个 code，
# 这样上层（runner / 设置界面）可以针对不同失败做不同提示，
# 而不用去解析各家 SDK 或 CLI 千差万别的报错文本。
ERROR_CODES = {
    "not_logged_in",       # CLI 类提供方未登录（claude / codex 没有登录态）
    "quota_or_rate_limit", # 配额用完或被限流
    "timeout",             # 调用超时
    "bad_output",          # 模型有响应，但内容不是预期的格式（如不是 JSON）
    "auth_failed",         # API Key 无效或被拒绝
    "unavailable",         # 服务不可用、网络错误等其他失败
}


class LLMError(Exception):
    """模型调用失败的统一异常。

    code 是上面 ERROR_CODES 里的一个短字符串，message 是给人看的补充说明。
    注意：message 里绝不能包含 API Key 或提示词全文（敏感内容不进报错信息），
    所以各个提供方在构造 message 时都要先截断、再过滤敏感内容。
    """

    def __init__(self, code: str, message: str = "") -> None:
        if code not in ERROR_CODES:
            # 这是编程错误（code 拼错了），直接抛 ValueError 让开发者发现。
            raise ValueError(f"unknown_llm_error_code:{code}")
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class Provider(Protocol):
    """所有模型提供方都要满足的协议（结构化鸭子类型）。

    complete(): 发一次"系统提示词 + 用户提示词"的补全请求，返回模型的文本输出。
                所有参数都带默认值语义上的关键字形式，方便各实现按需扩展。
    check():    做一次尽量便宜（甚至免费）的连通性检查，返回 (是否正常, 说明)。
                说明里不要包含敏感信息；用于设置界面的"测试"按钮。
    """

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_output_tokens: int,
        timeout_s: float = 300,
    ) -> str: ...

    def check(self) -> tuple[bool, str]: ...


# ---------------------------------------------------------------------------
# 子进程执行器（CLI 类提供方共用）
# ---------------------------------------------------------------------------
# claude_code 和 codex 都是包装命令行程序。为了让测试不真的启动子进程，
# 执行子进程的动作被抽象成一个可注入的函数（CONTRIBUTING.md："外部依赖都要能注入"）。
# 真实实现是 default_runner；测试里传入记录参数并返回预设结果的假替身。


@dataclass
class ProcResult:
    """一次子进程调用的结果。

    timed_out 为 True 表示进程超时被杀（此时 returncode 无意义）。
    stdout / stderr 已经解码成文本，方便上层做字符串判断。
    """

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class ProcessRunner(Protocol):
    """子进程执行器的接口：给它参数列表，返回 ProcResult。

    注意 argv 必须是参数列表（路径里可能有空格，项目规范禁止 shell=True）。
    cwd：子进程的工作目录。CLI 类提供方（claude / codex）会读当前
    目录里的项目说明文件，也会受目录权限影响，所以统一传专用空目录
    （config.llm_work_dir()）。默认 None 表示继承当前进程的目录，
    与旧行为一致；测试用的假 runner 应兼容这个可选关键字。
    """

    def __call__(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        input: str,
        timeout_s: float,
        cwd: str | None = None,
    ) -> ProcResult: ...


def default_runner(
    argv: list[str],
    *,
    env: dict[str, str],
    input: str,
    timeout_s: float,
    cwd: str | None = None,
) -> ProcResult:
    """真实的子进程执行器：用 subprocess.run 跑命令并捕获输出。

    超时不抛异常而是返回 timed_out=True，让上层统一翻译成 LLMError("timeout")。
    cwd 为 None 时继承当前进程的工作目录（和旧版行为一致）。
    """

    try:
        proc = subprocess.run(
            argv,
            env=env,
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return ProcResult(returncode=-1, stdout="", stderr="", timed_out=True)
    return ProcResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
