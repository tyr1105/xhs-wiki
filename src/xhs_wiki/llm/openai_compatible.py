"""OpenAI 兼容提供方：用官方 openai SDK（本机 3.31.0）连自定义 base_url。

在整体架构（docs/architecture.md §8）中，这一路线覆盖 DeepSeek、通义千问等
所有兼容 OpenAI Chat Completions 协议的服务：设置里只多一个 base_url。
Key 由调用方传入的 key_provider() 提供（真实实现读钥匙串，见 keys.py）。

SDK 用法依据 .venv/lib/python3.12/site-packages/openai 3.31.0 源码：
- 客户端构造：OpenAI(api_key=..., base_url=..., timeout=..., max_retries=...)
  （base_url 是关键字参数，类型 str | httpx.URL | None）
- 调用：client.chat.completions.create(model=..., messages=[...])，
  system 和 user 各一条消息；返回的 choices[0].message.content 是文本。
- 异常体系（openai._exceptions）与 anthropic SDK 同构：AuthenticationError、
  RateLimitError、APITimeoutError、APIConnectionError、APIStatusError 等。
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)

from .base import LLMError


class _ChatClient(Protocol):
    """测试假客户端要模拟的最小接口：chat.completions.create()。"""

    def create(self, **kwargs: Any) -> Any: ...


class OpenAICompatibleProvider:
    """实现 Provider 协议的 OpenAI 兼容接口调用器。"""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        key_provider: Callable[[], str],
        client_factory: Callable[..., _ChatClient] | None = None,
    ) -> None:
        if not base_url:
            # base_url 是这一路线的必备设置，缺了直接报参数错误
            raise ValueError("base_url_required")
        if client_factory is None:
            import openai

            client_factory = openai.OpenAI
        self._client_factory = client_factory
        self._model = model
        self._base_url = base_url
        self._key_provider = key_provider
        # 多数兼容服务使用 max_tokens；只有服务明确拒绝这个参数时，
        # 才切换到推理模型要求的名称，并在本实例后续调用中复用。
        self._token_parameter = "max_tokens"

    def _make_client(self, timeout_s: float) -> _ChatClient:
        try:
            key = self._key_provider()
        except Exception as e:
            # 和 anthropic_api 同理：取不到 Key 是认证问题，不透出异常详情
            raise LLMError("auth_failed", "api_key_unavailable") from e
        return self._client_factory(
            api_key=key,
            base_url=self._base_url,
            timeout=timeout_s,
            max_retries=0,
        )

    def _create_completion(self, client, *, messages, max_output_tokens):
        """共用测试调用与正式调用的参数协商，最多额外请求一次。

        只对明确提到 token 上限参数的 HTTP 400 做兼容回退；认证失败、
        限流或其他参数错误原样抛出。服务的错误正文仅用于本地判断，
        不写入状态或日志；对外仍由统一映射生成不含正文的错误代码。
        """
        parameter = self._token_parameter
        try:
            return client.chat.completions.create(
                model=self._model,
                messages=messages,
                **{parameter: max_output_tokens},
            )
        except APIStatusError as error:
            message = str(error).lower()
            if not (
                parameter == "max_tokens"
                and error.status_code == 400
                and any(name in message for name in ("max_tokens", "max_completion_tokens"))
            ):
                raise
        # 第一次请求确实因上限参数被拒绝。第二次的异常不在上面的
        # 捕获范围内，所以不会递归重试；只有成功才缓存兼容结果。
        completion = client.chat.completions.create(
            model=self._model,
            messages=messages,
            max_completion_tokens=max_output_tokens,
        )
        self._token_parameter = "max_completion_tokens"
        return completion

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_output_tokens: int,
        timeout_s: float = 300,
    ) -> str:
        """发一次 Chat Completions 请求：system 和 user 两条消息。"""

        client = self._make_client(timeout_s)
        try:
            completion = self._create_completion(
                client,
                max_output_tokens=max_output_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            )
        except Exception as e:
            raise _map_openai_error(e) from None
        try:
            text = completion.choices[0].message.content
        except (AttributeError, IndexError, TypeError):
            raise LLMError("bad_output", "openai_no_choice") from None
        if not isinstance(text, str) or not text:
            raise LLMError("bad_output", "openai_empty_content")
        return text

    def check(self) -> tuple[bool, str]:
        """发一次 max_tokens=1 的最小请求验证 Key 和 base_url 可用。

        注意：这一步会产生极少量费用，只在用户点"测试"时调用。
        """

        try:
            client = self._make_client(60)
            self._create_completion(
                client,
                max_output_tokens=1,
                messages=[
                    {"role": "system", "content": "ping"},
                    {"role": "user", "content": "ping"},
                ],
            )
            return (True, "openai_compatible_ok")
        except LLMError as e:
            return (False, e.code)
        except Exception as e:
            return (False, _map_openai_error(e).code)


def _map_openai_error(e: Exception) -> LLMError:
    """把 openai SDK 的异常映射到统一的错误代码（与 anthropic_api 同构）。"""

    if isinstance(e, AuthenticationError):
        return LLMError("auth_failed", "openai_401")
    if isinstance(e, PermissionDeniedError):
        return LLMError("auth_failed", "openai_403")
    if isinstance(e, RateLimitError):
        return LLMError("quota_or_rate_limit", "openai_429")
    if isinstance(e, APITimeoutError):
        return LLMError("timeout", "openai_timeout")
    if isinstance(e, APIConnectionError):
        return LLMError("unavailable", "openai_connection")
    if isinstance(e, (InternalServerError,)):
        return LLMError("unavailable", "openai_5xx")
    if isinstance(e, APIStatusError):
        return LLMError("unavailable", f"openai_status_{e.status_code}")
    if isinstance(e, APIError):
        return LLMError("unavailable", "openai_api_error")
    return LLMError("unavailable", f"openai_unexpected:{type(e).__name__}")
