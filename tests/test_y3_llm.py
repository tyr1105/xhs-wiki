"""模型调用回归测试：验证真实 SDK 请求结构、token 参数兼容和 Codex 默认模型。

处于模型层的离线验收边界：使用真实 Anthropic/OpenAI 客户端，只替换
HTTP 传输层（MockTransport，即把请求交给本地函数而不是网络）。所有
凭据和返回内容均在测试中生成；CLI 使用假 runner，不启动真实进程。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

import anthropic
# 固定版本的两个 SDK 依赖 httpx2，本环境没有旧 httpx 包；使用其原生
# Client/MockTransport 并沿用 httpx 别名，不安装依赖、不替换 SDK 方法。
import httpx2 as httpx
import openai
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xhs_wiki.llm.anthropic_api import AnthropicApiProvider
from xhs_wiki.llm.base import LLMError, ProcResult
from xhs_wiki.llm.codex import CodexProvider
from xhs_wiki.llm.openai_compatible import OpenAICompatibleProvider


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    """三个运行目录逐项隔离，防止单独运行本文件时触碰真实用户目录。"""
    for name, folder in (
        ("XHS_WIKI_HOME", "app"),
        ("XHS_WIKI_LOG_DIR", "logs"),
        ("XHS_WIKI_SKILLS_ROOT", "skills"),
    ):
        monkeypatch.setenv(name, str(tmp_path / folder))


def _anthropic_response():
    """生成符合 Messages 响应结构的虚构消息，保留两个文本块验证拼接。"""
    return {
        "id": "msg_" + uuid4().hex,
        "type": "message",
        "role": "assistant",
        "model": "generated-model",
        "content": [
            {"type": "text", "text": "生成的"},
            {"type": "text", "text": "回复"},
        ],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 4, "output_tokens": 2},
    }


def _openai_response():
    """生成标准 Chat Completions 响应，让真实 SDK 解析 choices 中的文本。"""
    return {
        "id": "chatcmpl_" + uuid4().hex,
        "object": "chat.completion",
        "created": 0,
        "model": "generated-model",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "生成的回复"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    }


def _call_provider(provider, operation):
    """以同一套断言覆盖向导检查和正式调用，避免只修复其中一条路径。"""
    if operation == "check":
        return provider.check()
    return provider.complete(
        system="生成的系统提示", prompt="生成的输入", max_output_tokens=37
    )


@pytest.mark.parametrize("operation", ["complete", "check"])
def test_anthropic_real_sdk_mock_transport(operation):
    """通过真实 SDK 校验 /v1/messages、字段及文本；顶层 create 写错就失败。"""
    requests = []

    def handler(request):
        """截获全部 HTTP 请求，检查完整调用路径后返回标准假消息。"""
        assert request.method == "POST"
        assert request.url.path == "/v1/messages"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_anthropic_response())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        def factory(**kwargs):
            """保留生产代码的构造参数，只替换地址和 HTTP 层为本地替身。"""
            return anthropic.Anthropic(
                **kwargs, base_url="https://generated.example.invalid",
                http_client=http_client,
            )

        provider = AnthropicApiProvider(
            model="generated-model", key_provider=lambda: "generated-" + uuid4().hex,
            client_factory=factory,
        )
        result = _call_provider(provider, operation)

    expected = {
        "model": "generated-model",
        "max_tokens": 37 if operation == "complete" else 1,
        "messages": [{
            "role": "user",
            "content": "生成的输入" if operation == "complete" else "ping",
        }],
    }
    if operation == "complete":
        expected["system"] = "生成的系统提示"
    assert requests == [expected]
    assert result == ("生成的回复" if operation == "complete" else (True, "anthropic_api_ok"))


@pytest.fixture
def openai_provider_factory():
    """为每个测试构造真实 OpenAI 客户端，测试结束时关闭所有假传输对象。"""
    clients = []

    def build(handler):
        """注入 HTTP 假响应，其他客户端构造和响应解析仍使用真实 SDK。"""
        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(http_client)

        def factory(**kwargs):
            """将生产构造参数原样传给 SDK，避免替身掩盖构造或方法错误。"""
            return openai.OpenAI(**kwargs, http_client=http_client)

        return OpenAICompatibleProvider(
            model="generated-model", base_url="https://generated.example.invalid/v1",
            key_provider=lambda: "generated-" + uuid4().hex,
            client_factory=factory,
        )

    yield build
    for client in clients:
        client.close()


@pytest.mark.parametrize("operation", ["complete", "check"])
def test_openai_real_sdk_mock_transport(openai_provider_factory, operation):
    """验证真实 Chat Completions 路径、system/user 两条消息和输出文本。"""
    requests = []

    def handler(request):
        """在假 HTTP 层检查请求，再返回 SDK 能解析的标准 JSON。"""
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_openai_response())

    result = _call_provider(openai_provider_factory(handler), operation)
    assert requests == [{
        "model": "generated-model",
        "max_tokens": 37 if operation == "complete" else 1,
        "messages": [
            {"role": "system", "content": "生成的系统提示" if operation == "complete" else "ping"},
            {"role": "user", "content": "生成的输入" if operation == "complete" else "ping"},
        ],
    }]
    assert result == ("生成的回复" if operation == "complete" else (True, "openai_compatible_ok"))


@pytest.mark.parametrize("operation", ["complete", "check"])
@pytest.mark.parametrize("error_parameter", ["max_tokens", "max_completion_tokens"])
def test_openai_token_parameter_fallback_and_cache(openai_provider_factory, operation, error_parameter):
    """指定 400 只重试一次，成功后的下一次正式调用直接使用兼容参数。"""
    requests = []

    def handler(request):
        """首个请求模拟服务拒绝旧参数，后续请求返回成功响应。"""
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(400, json={"error": {
                "message": "Unsupported parameter: " + error_parameter,
                "type": "invalid_request_error",
                "param": error_parameter,
                "code": "unsupported_parameter",
            }})
        return httpx.Response(200, json=_openai_response())

    provider = openai_provider_factory(handler)
    result = _call_provider(provider, operation)
    assert result == ("生成的回复" if operation == "complete" else (True, "openai_compatible_ok"))
    assert _call_provider(provider, "complete") == "生成的回复"
    assert len(requests) == 3
    assert requests[0]["max_tokens"] == (37 if operation == "complete" else 1)
    assert "max_completion_tokens" not in requests[0]
    assert requests[1]["max_completion_tokens"] == requests[0]["max_tokens"]
    assert requests[1]["messages"] == requests[0]["messages"]
    assert requests[2]["max_completion_tokens"] == 37
    assert all("max_tokens" not in body for body in requests[1:])


@pytest.mark.parametrize("status,message,code", [
    (400, "unrelated_parameter", "unavailable"),
    (429, "max_tokens", "quota_or_rate_limit"),
    (401, "max_completion_tokens", "auth_failed"),
])
def test_openai_unrelated_errors_do_not_retry(openai_provider_factory, status, message, code):
    """不把其他 400、限流或认证失败误当作参数兼容问题重试。"""
    requests = []

    def handler(request):
        """计数后返回指定错误，任何意外的额外调用都会被最终断言发现。"""
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": message}})

    with pytest.raises(LLMError) as error:
        _call_provider(openai_provider_factory(handler), "complete")
    assert error.value.code == code
    assert len(requests) == 1


def test_openai_fallback_failure_is_bounded_and_redacted(openai_provider_factory):
    """兼容参数也失败时只发两次，服务正文中的虚构敏感值不进入异常。"""
    requests = []
    sensitive = "generated-" + uuid4().hex

    def handler(request):
        """每次都拒绝上限参数，验证实现不会反复来回切换或递归请求。"""
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {
            "message": "max_tokens max_completion_tokens " + sensitive,
        }})

    with pytest.raises(LLMError) as error:
        _call_provider(openai_provider_factory(handler), "complete")
    assert error.value.code == "unavailable"
    assert len(requests) == 2
    assert sensitive not in str(error.value)
    assert "max_tokens" in requests[0]
    assert "max_completion_tokens" in requests[1]


@pytest.mark.parametrize("model", [None, "", "default", "generated-model"])
def test_codex_default_model_omits_flag(tmp_path, model):
    """留空及旧 default 占位不传 -m，显式模型名仍精确传入。"""
    calls = []

    def runner(argv, *, env, input, timeout_s, cwd=None):
        """只记录参数并模拟输出文件，绝不执行 Codex 或任何子进程。"""
        calls.append(list(argv))
        Path(argv[argv.index("-o") + 1]).write_text("生成的回复", encoding="utf-8")
        return ProcResult(0, "", "")

    provider = CodexProvider(
        model=model, explicit_path=str(tmp_path / "generated-codex"), runner=runner
    )
    assert _call_provider(provider, "complete") == "生成的回复"
    assert len(calls) == 1
    if model == "generated-model":
        assert calls[0][calls[0].index("-m") + 1] == model
    else:
        assert "-m" not in calls[0]
