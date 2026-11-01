"""Anthropic API 提供方：用官方 anthropic SDK（本机 1.15.0）直连 Messages API。

在整体架构（docs/architecture.md §8）中，这是"用自己的 API Key"路线之一。
Key 不放在代码或设置文件里，由调用方传入的 key_provider() 现场取得
（真实实现从 macOS 钥匙串读，见 keys.py；本文件完全不碰钥匙串）。

SDK 用法依据 .venv/lib/python3.12/site-packages/anthropic 1.15.0 源码：
- 客户端构造：Anthropic(api_key=..., timeout=..., max_retries=...)
- 调用：client.messages.create(model=..., system=..., max_tokens=...,
  messages=[{"role": "user", "content": ...}])，返回的 Message.content
  是块列表，文本在 type=="text" 的块里。
- 异常体系（anthropic._exceptions）：AuthenticationError(401)、
  PermissionDeniedError(403)、RateLimitError(429)、APITimeoutError、
  APIConnectionError、OverloadedError / ServiceUnavailableError(5xx)、
  以及基类 APIStatusError / APIError。
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from anthropic import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OverloadedError,
    PermissionDeniedError,
    RateLimitError,
    ServiceUnavailableError,
)

from .base import LLMError


class _MessagesResource(Protocol):
    """Messages 资源的最小接口；客户端通过 messages 属性暴露它。"""

    def create(self, **kwargs: Any) -> Any: ...


class _MessagesClient(Protocol):
    """保持与真实 SDK 相同的层级，避免假客户端掩盖错误调用路径。"""

    messages: _MessagesResource


class AnthropicApiProvider:
    """实现 Provider 协议的 Anthropic API 调用器。"""

    def __init__(
        self,
        *,
        model: str,
        key_provider: Callable[[], str],
        client_factory: Callable[..., _MessagesClient] | None = None,
    ) -> None:
        # client_factory 可注入：默认用官方 SDK 的 Anthropic 构造器，
        # 测试时换成返回假客户端的函数，完全不发网络请求。
        if client_factory is None:
            import anthropic

            client_factory = anthropic.Anthropic
        self._client_factory = client_factory
        self._model = model
        self._key_provider = key_provider

    def _make_client(self, timeout_s: float) -> _MessagesClient:
        try:
            key = self._key_provider()
        except Exception as e:
            # key_provider 失败（比如钥匙串里没有条目）是认证问题；
            # 注意 message 里不能带异常详情，防止意外泄露路径等敏感信息
            raise LLMError("auth_failed", "api_key_unavailable") from e
        # max_retries=0：SDK 自带的指数退避重试会和我们的超时预算打架，
        # 失败要尽快抛回来交给上层决定（项目规范：异常不要吞掉，见 CONTRIBUTING.md）
        return self._client_factory(api_key=key, timeout=timeout_s, max_retries=0)

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_output_tokens: int,
        timeout_s: float = 300,
    ) -> str:
        """发一次 Messages 请求，拼接所有文本块返回。"""

        client = self._make_client(timeout_s)
        try:
            message = client.messages.create(
                model=self._model,
                system=system,
                max_tokens=max_output_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            raise _map_anthropic_error(e) from None
        # content 是块列表（文本 / 工具调用 / thinking 混排），只取文本块
        parts = [
            block.text
            for block in message.content
            if getattr(block, "type", "") == "text"
        ]
        if not parts:
            raise LLMError("bad_output", "anthropic_no_text_block")
        return "".join(parts)

    def check(self) -> tuple[bool, str]:
        """发一次 max_tokens=1 的最小请求验证 Key 有效。

        注意：这一步会产生极少量费用（几个 token），只在用户点"测试"时调用，
        不要放进每日自动流程。
        """

        try:
            client = self._make_client(60)
            client.messages.create(
                model=self._model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            return (True, "anthropic_api_ok")
        except LLMError as e:
            return (False, e.code)
        except Exception as e:
            return (False, _map_anthropic_error(e).code)


def _map_anthropic_error(e: Exception) -> LLMError:
    """把 anthropic SDK 的异常映射到统一的错误代码。

    message 只放 HTTP 状态码这类不敏感的信息，绝不放响应体原文
    （响应体理论上不含我们发的提示词，但保守起见也不放 Key 相关内容）。
    """

    if isinstance(e, AuthenticationError):
        return LLMError("auth_failed", "anthropic_401")
    if isinstance(e, PermissionDeniedError):
        return LLMError("auth_failed", "anthropic_403")
    if isinstance(e, RateLimitError):
        return LLMError("quota_or_rate_limit", "anthropic_429")
    if isinstance(e, APITimeoutError):
        return LLMError("timeout", "anthropic_timeout")
    if isinstance(e, APIConnectionError):
        return LLMError("unavailable", "anthropic_connection")
    if isinstance(e, (OverloadedError, ServiceUnavailableError)):
        return LLMError("unavailable", "anthropic_5xx")
    if isinstance(e, APIStatusError):
        # 其余带状态码的错误（4xx/5xx），带上状态码方便排查
        return LLMError("unavailable", f"anthropic_status_{e.status_code}")
    if isinstance(e, APIError):
        return LLMError("unavailable", "anthropic_api_error")
    # 完全意外的异常（比如 Key 类型不对导致 SDK 内部抛 TypeError）。
    # str(e) 可能包含任意内容，只保留异常类名，避免泄露。
    return LLMError("unavailable", f"anthropic_unexpected:{type(e).__name__}")
