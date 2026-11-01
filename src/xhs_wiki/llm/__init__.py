"""模型调用层的入口：make_provider() 按设置创建对应的提供方实例。

设置结构（docs/architecture.md §7）里的 llm 字段形如：
  {"provider": "claude_code", "model": "...", "base_url": null}
provider 可选值：claude_code / codex / anthropic / openai_compatible。

llm 只接受上面三个键：不再从设置里读 claude_path /
codex_path——控制台接口曾能把任意可执行文件路径写进设置，守护进程
又会照着执行，等于越权代码执行。需要自定义路径时用环境变量或
命令行参数（ClaudeCodeProvider/CodexProvider 的 explicit_path 参数
仍保留，供测试和本地脚本注入）。
"""

from __future__ import annotations

from typing import Any, Callable

from .anthropic_api import AnthropicApiProvider
from .base import LLMError, Provider
from .claude_code import ClaudeCodeProvider
from .codex import CodexProvider
from .openai_compatible import OpenAICompatibleProvider

# 各提供方在钥匙串里用的账户名（keys.py 的 account 参数用这个）
KEY_ACCOUNTS = {
    "anthropic": "anthropic",
    "openai_compatible": "openai_compatible",
}


def make_provider(
    llm_settings: dict[str, Any],
    *,
    key_provider: Callable[[], str],
    runner: Any = None,
) -> Provider:
    """根据 llm 设置创建 Provider 实例。

    key_provider：取 API Key 的回调，只有 API 类提供方会用到。
    runner：可注入的子进程执行器（测试传假替身），只有 CLI 类提供方用到。
    """

    name = llm_settings.get("provider")
    model = llm_settings.get("model") or ""
    if not name:
        raise ValueError("provider_missing")
    # model 为空只对 codex 合法：不选模型就用用户在 Codex 里自己配置的
    # 默认模型。其他提供方仍必须指定模型。
    if not model and name != "codex":
        raise ValueError("model_missing")

    if name == "claude_code":
        return ClaudeCodeProvider(
            model=model,
            # 不从设置读 claude_path（见文件顶部的说明）。
            explicit_path=None,
            runner=runner,
        )
    if name == "codex":
        return CodexProvider(
            model=model,
            # 不从设置读 codex_path，理由同上。
            explicit_path=None,
            runner=runner,
        )
    if name == "anthropic":
        return AnthropicApiProvider(model=model, key_provider=key_provider)
    if name == "openai_compatible":
        return OpenAICompatibleProvider(
            model=model,
            base_url=llm_settings.get("base_url") or "",
            key_provider=key_provider,
        )
    raise ValueError(f"unknown_provider:{name}")


__all__ = [
    "KEY_ACCOUNTS",
    "LLMError",
    "Provider",
    "make_provider",
    "ClaudeCodeProvider",
    "CodexProvider",
    "AnthropicApiProvider",
    "OpenAICompatibleProvider",
]
