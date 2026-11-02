"""Claude Code 提供方：通过 `claude -p`（打印模式）调用用户登录的 Claude Code。

在整体架构（docs/architecture.md §8）中，这是四种模型提供方里唯一复用
用户已有 Claude Code 订阅登录态的一种，不需要 API Key。

为什么环境要特殊处理（实际踩过的坑）：
- 后端启动子进程时往往只给很少几个环境变量，没有 HOME，Claude Code 找不到
  钥匙串里的登录凭据，直接报 "Not logged in"（实测如此）。
- 一旦设置了 CLAUDE_CONFIG_DIR，Claude 会去找另一个名字的钥匙串条目，
  同样显示"未登录"，所以必须把它去掉。
因此这里的子进程环境会补上 HOME / USER、删掉 CLAUDE_CONFIG_DIR，
PATH 也会补上常见安装位置（~/.local/bin 等）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from .base import LLMError, ProcessRunner, default_runner
from .. import config

# PATH 里额外补充的常见安装位置。claude 常通过官方安装脚本装到 ~/.local/bin，
# 或通过 Homebrew 装到 /opt/homebrew/bin（Apple Silicon）/ /usr/local/bin（Intel）。
_EXTRA_PATH_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")


def build_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """构造 claude 子进程要用的环境变量。

    - 保留传入环境（默认取当前进程环境）里的 PATH、代理等少量无害变量；
    - 补上 HOME 和 USER（没有它们 Claude 找不到登录态）；
    - 删掉 CLAUDE_CONFIG_DIR（见文件顶部的说明）；
    - PATH 前面追加常见安装位置，保证能找到 claude 可执行文件。
    """

    src = dict(os.environ if base_env is None else base_env)
    env = {
        "HOME": src.get("HOME", str(Path.home())),
        "USER": src.get("USER", ""),
        # 代理变量原样带过去，避免内网/特殊网络环境下连不上
        "http_proxy": src.get("http_proxy", ""),
        "https_proxy": src.get("https_proxy", ""),
        "no_proxy": src.get("no_proxy", ""),
    }
    # 空代理值反而可能干扰，删掉空项
    for k in ("http_proxy", "https_proxy", "no_proxy"):
        if not env[k]:
            del env[k]
    # CLAUDE_CONFIG_DIR 会让 Claude 找错钥匙串条目，必须去掉（不是保留为空）
    src.pop("CLAUDE_CONFIG_DIR", None)
    parts = [str(Path(d).expanduser()) for d in _EXTRA_PATH_DIRS]
    parts.append(src.get("PATH", ""))
    env["PATH"] = ":".join(p for p in parts if p)
    return env


def find_claude(explicit_path: str | None = None) -> str | None:
    """按顺序发现 claude 可执行文件：设置里指定的路径 → PATH → ~/.local/bin/claude。

    返回 None 表示找不到（上层应提示用户安装或配置路径）。
    """

    if explicit_path:
        return explicit_path
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path("~/.local/bin/claude").expanduser()
    if fallback.is_file():
        return str(fallback)
    return None


def supported_print_flags(exe: str | None, *, runner=None, env=None, cwd=None) -> frozenset[str]:
    """一次读取帮助，分别判断打印模式的两个隔离参数是否受支持。

    --safe-mode 会屏蔽个人的说明文件、Skill 和插件；旧版未必支持，
    --no-session-persistence 避免整理内容写入 CLI 会话历史。两者独立
    判断，只有帮助文本明确列出完整参数名时才启用。runner、环境及
    工作目录可注入，不调用模型。超时、失败或输出异常视为均不支持。
    本函数不缓存，由调用方按实例或脚本生成周期缓存结果。
    """

    if exe is None:
        return frozenset()
    try:
        result = (runner or default_runner)(
            [exe, "--help"], env=build_env() if env is None else env,
            input="", timeout_s=15,
            cwd=str(config.llm_work_dir()) if cwd is None else str(cwd),
        )
        if result.timed_out or result.returncode != 0:
            return frozenset()
        # 按完整名称识别，防止 --safe-mode-extra 之类相似参数误报支持。
        return frozenset(flag for flag in ("--safe-mode", "--no-session-persistence")
                         if re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])",
                                      result.stdout))
    except Exception:
        # 帮助探测不承载业务请求；检测失败不应阻止兼容旧版 CLI。
        return frozenset()


def supports_safe_mode(exe: str | None, *, runner=None, env=None, cwd=None) -> bool:
    """保持轻量引擎既有布尔接口，复用包装脚本相同的帮助解析逻辑。"""

    return "--safe-mode" in supported_print_flags(exe, runner=runner, env=env, cwd=cwd)


# 判断"未登录"的特征字符串：Claude Code 未登录时输出里会出现的片段。
_NOT_LOGGED_IN_HINTS = ("not logged in", "未登录", "auth login")


class ClaudeCodeProvider:
    """实现 Provider 协议的 Claude Code 调用器。"""

    def __init__(
        self,
        *,
        model: str,
        explicit_path: str | None = None,
        runner: ProcessRunner | None = None,
    ) -> None:
        # runner 可注入：测试传假替身，不真的启动 claude。
        self._runner: ProcessRunner = runner or default_runner
        self._model = model
        self._explicit_path = explicit_path
        self._exe = find_claude(explicit_path)
        # --safe-mode 支持检测结果缓存：None 表示还没检测过，
        # 检测过一次后固定为 True/False，同一实例不再重复跑 --help。
        self._safe_mode: bool | None = None

    def _require_exe(self) -> str:
        if self._exe is None:
            # 找不到可执行文件属于环境问题，归到 unavailable
            raise LLMError("unavailable", "claude_executable_not_found")
        return self._exe

    def _supports_safe_mode(self) -> bool:
        """检测当前 claude 是否支持 `--safe-mode` 参数。

        为什么需要检测：旧版本的 claude 还没有这个参数，直接加上会报
        "未知参数"，整理会整个失败。

        做法是第一次需要时运行一次 `<claude> --help`，看输出里有没有
        `--safe-mode`。help 的原文说明是：以禁用所有个人定制
        （CLAUDE.md、Skill、插件、hooks、MCP、自定义命令、输出样式等）
        的方式启动，但订阅登录（OAuth）和内置工具照常可用——正好是整理
        需要的隔离环境，又不会丢登录态（`--bare` 会只认 API Key，不能用）。

        保守原则：超时、退出码非 0、输出读不出来，一律当作"不支持"，
        不报错（宁可少一层隔离，也不要让整理直接失败）。
        结果缓存在实例属性上，同一个实例只检测一次。
        """

        if self._safe_mode is not None:
            return self._safe_mode
        self._safe_mode = supports_safe_mode(self._exe, runner=self._runner)
        return self._safe_mode

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_output_tokens: int,
        timeout_s: float = 300,
    ) -> str:
        """发一次调用并返回模型文本。

        每个命令行参数的依据（claude --help 原文，2026-10 版本实测）：
        - "-p" / "--print"：Print response and exit（非交互模式）
        - "--safe-mode"：新版 claude 才有。以禁用所有个人定制
          （用户级 CLAUDE.md、Skill、插件、hooks 等）的方式启动，
          防止用户的个人说明影响整理结果。是否支持在运行时检测
          （见 _supports_safe_mode），不支持时保持原有参数不变。
        - "--model <m>"：Model for the current session
        - "--tools \"\""：'Use "" to disable all tools' —— 关掉所有内置工具
        - "--strict-mcp-config"：Only use MCP servers from --mcp-config,
          ignoring all other MCP configurations —— 配合空的 --mcp-config，
          用户的 MCP 一个都不加载
        - "--mcp-config {\"mcpServers\":{}}"：Load MCP servers from JSON files
          or strings —— 传一个空配置，等于没有任何 MCP 服务器
        - "--setting-sources \"\""：Comma-separated list of setting sources
          to load (user, project, local) —— 传空串即三个来源都不读，
          不加载用户的 hooks / 插件配置
        - "--disable-slash-commands"：Disable all skills —— 不加载用户的 Skill
        - "--no-session-persistence"：Disable session persistence（只在 --print
          下生效）—— 会话不落盘，避免知识库内容留在 ~/.claude 的会话记录里
        - "--output-format json"：'"json" (single result)' —— 输出单个 JSON
          对象，里面有 is_error 字段可以判断成败
        - "--system-prompt <s>"：System prompt to use for the session
        - 提示词从标准输入传入（-p 模式不传位置参数时自动读 stdin)

        工作目录：统一用 APP_HOME/llm-workdir/ 这个专用空目录。
        claude 会把当前目录当项目目录，读里面的 CLAUDE.md / AGENTS.md；
        目录没权限（macOS TCC，如"文稿"）还会让子进程一直卡到超时。
        """

        exe = self._require_exe()
        # -p 后紧跟 --safe-mode（支持时）：把用户的个人定制挡在上下文外。
        # 其他参数一个都不删——--safe-mode 万一没生效，后面那些
        # --tools ""/--setting-sources ""/… 仍然是多一层保险。
        argv = [
            exe,
            "-p",
        ]
        if self._supports_safe_mode():
            argv.append("--safe-mode")
        argv += [
            "--model", self._model,
            "--tools", "",
            "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}',
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--output-format", "json",
            "--system-prompt", system,
        ]
        result = self._runner(
            argv, env=build_env(), input=prompt, timeout_s=timeout_s,
            cwd=str(config.llm_work_dir()),
        )
        return self._parse_result(result)

    def _parse_result(self, result) -> str:
        """把子进程结果翻译成模型文本或 LLMError。所有错误信息都过滤敏感内容。"""

        if result.timed_out:
            raise LLMError("timeout", "claude_code_timeout")
        # 未登录的判定放在 JSON 解析之前：未登录时 stdout 通常不是 JSON，
        # 只能从退出码非零 + 输出特征判断
        if result.returncode != 0:
            blob = (result.stderr + "\n" + result.stdout).lower()
            if any(h in blob for h in _NOT_LOGGED_IN_HINTS):
                raise LLMError("not_logged_in", "claude_code_not_logged_in")
            if "rate limit" in blob or "quota" in blob:
                raise LLMError("quota_or_rate_limit", "claude_code_rate_limited")
            raise LLMError("unavailable", f"claude_code_exit_{result.returncode}")
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            # 输出不是 JSON：说明命令用法或环境出了问题，不是模型本身失败
            raise LLMError("bad_output", "claude_code_output_not_json") from None
        if not isinstance(payload, dict):
            raise LLMError("bad_output", "claude_code_output_not_object")
        if payload.get("is_error"):
            # 模型侧报错（如配额、限流）。只保留截断后的 result 字段做提示，
            # 不包含我们发出的提示词。
            detail = str(payload.get("result", ""))[:200]
            blob = detail.lower()
            if "rate limit" in blob or "quota" in blob:
                raise LLMError("quota_or_rate_limit", "claude_model_error_rate_limited")
            raise LLMError("unavailable", f"claude_model_error:{detail}")
        text = payload.get("result")
        if not isinstance(text, str):
            raise LLMError("bad_output", "claude_code_result_missing")
        return text

    def check(self) -> tuple[bool, str]:
        """用 `claude auth status` 判断是否已登录（不发起会消耗用量的调用）。

        auth status 输出 JSON，其中 loggedIn 字段表示登录态；
        工作目录同样用专用空目录：实测发现从无权限
        目录启动时这条命令会卡满 60 秒。
        """

        exe = self._exe
        if exe is None:
            return (False, "claude_executable_not_found")
        result = self._runner(
            [exe, "auth", "status"],
            env=build_env(),
            input="",
            timeout_s=60,
            cwd=str(config.llm_work_dir()),
        )
        if result.timed_out:
            return (False, "claude_auth_status_timeout")
        try:
            payload = json.loads(result.stdout)
            logged_in = bool(payload.get("loggedIn"))
        except ValueError:
            return (False, "claude_auth_status_unparseable")
        if logged_in:
            return (True, "claude_code_logged_in")
        return (False, "claude_code_not_logged_in")
