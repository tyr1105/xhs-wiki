"""模型调用层（llm/）的测试。

全部用假替身：CLI 类（claude_code / codex）用假的子进程 runner，
API 类（anthropic / openai_compatible）用假客户端和假 key_provider，
钥匙串（keys）用假 security runner。不访问网络、不启动真实进程、
不碰真实钥匙串。

所有测试数据（模型名、提示词、Key）都是虚构的字符串。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import openai  # 只为在测试里引用 SDK 的异常类（不真的发请求）

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xhs_wiki.llm import make_provider
from xhs_wiki.llm.base import LLMError, ProcResult
from xhs_wiki.llm.claude_code import build_env, find_claude
from xhs_wiki.llm.codex import find_codex
from xhs_wiki.llm.keys import Keychain, SERVICE_NAME

# 虚构的模型名和 Key（不是任何真实账号的凭据）
FAKE_MODEL = "test-model-x"
FAKE_KEY = "test-key-do-not-use-0000"
SYSTEM = "你是整理笔记的助手（测试用虚构提示词）"
PROMPT = "把这段测试笔记整理成条目（虚构内容）"


# ---------------------------------------------------------------------------
# 工具：假的子进程 runner
# ---------------------------------------------------------------------------
class FakeRunner:
    """记录调用参数并按预设返回结果的假 runner。

    responses 是一个列表：每次调用弹出第一项；调用记录存进 calls，
    每条是 {"argv", "env", "input", "timeout_s", "cwd"}。
    on_call：可选的回调（argv, env, input, timeout_s, cwd) -> ProcResult），
    提供时优先于 responses，用于 codex 测试里"先写 -o 文件再返回成功"。
    help_stdout：假的 `--help` 输出（claude_code 的 complete() 现在
    会先跑一次 `<claude> --help` 检测 --safe-mode）。默认输出里不含
    --safe-mode，即"旧版本不支持"；需要测支持时再传含该词的输出。
    """

    # 旧版 claude 的假帮助文本：没有 --safe-mode 字样
    DEFAULT_HELP = "Usage: claude [options] Options: --model -p --tools ..."

    def __init__(self, *responses: ProcResult, on_call=None,
                 help_stdout: str | None = None,
                 help_result: ProcResult | None = None):
        self.responses = list(responses)
        self.on_call = on_call
        # help_calls 记录每次 --help 探测调用的参数（不含在 calls 里，
        # 这样旧断言"业务调用"的测试不用改）
        self.help_calls: list[dict] = []
        self.help_stdout = (
            self.DEFAULT_HELP if help_stdout is None else help_stdout)
        self.help_result = help_result
        self.calls: list[dict] = []

    def __call__(self, argv, *, env, input, timeout_s, cwd=None):
        # --help 探测调用单独应答，不消耗预设的 responses
        if "--help" in argv:
            self.help_calls.append(
                {"argv": list(argv), "env": env, "input": input,
                 "timeout_s": timeout_s, "cwd": cwd}
            )
            if self.help_result is not None:
                return self.help_result
            return ProcResult(0, self.help_stdout, "")
        self.calls.append(
            {"argv": list(argv), "env": env, "input": input,
             "timeout_s": timeout_s, "cwd": cwd}
        )
        if self.on_call is not None:
            return self.on_call(list(argv), env, input, timeout_s, cwd)
        if not self.responses:
            raise AssertionError("FakeRunner 没有预设的返回了")
        return self.responses.pop(0)


def ok_json(payload: dict) -> ProcResult:
    """构造"stdout 是 JSON、退出码 0"的成功结果。"""

    return ProcResult(returncode=0, stdout=json.dumps(payload), stderr="")


# ---------------------------------------------------------------------------
# claude_code
# ---------------------------------------------------------------------------
class TestClaudeCode(unittest.TestCase):
    def _provider(self, runner):
        # claude_path 显式给一个虚构路径，避免测试依赖本机是否装了 claude
        from xhs_wiki.llm.claude_code import ClaudeCodeProvider

        return ClaudeCodeProvider(model=FAKE_MODEL, explicit_path="/tmp/fake-claude", runner=runner)

    def test_success(self):
        r = FakeRunner(ok_json({"is_error": False, "result": "整理好了"}))
        text = self._provider(r).complete(
            system=SYSTEM, prompt=PROMPT, max_output_tokens=100
        )
        self.assertEqual(text, "整理好了")
        # 只发了一次子进程调用
        self.assertEqual(len(r.calls), 1)

    def test_argv_and_stdin(self):
        r = FakeRunner(ok_json({"is_error": False, "result": "ok"}))
        self._provider(r).complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50)
        call = r.calls[0]
        argv = call["argv"]
        self.assertEqual(argv[0], "/tmp/fake-claude")
        self.assertIn("-p", argv)
        # 指定模型
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], FAKE_MODEL)
        # 关掉所有工具：--tools 后面跟空串
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        # 不加载 MCP：strict 配置 + 空配置
        self.assertIn("--strict-mcp-config", argv)
        mcp = json.loads(argv[argv.index("--mcp-config") + 1])
        self.assertEqual(mcp, {"mcpServers": {}})
        # 不读用户/项目/本地设置
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        # 禁用 Skill（斜杠命令）
        self.assertIn("--disable-slash-commands", argv)
        # JSON 输出 + system 提示词
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], SYSTEM)
        # 提示词从标准输入传入，且不在 argv 里
        self.assertEqual(call["input"], PROMPT)
        self.assertNotIn(PROMPT, argv)

    # ------------------------------------------------------------------
    # --safe-mode 支持检测
    # 含该词的假帮助文本（新版 claude）
    HELP_WITH_SAFE_MODE = (
        "Usage: claude [options]\n"
        "  --safe-mode  Start with all customizations disabled\n"
        "  --model -p --tools ...\n"
    )

    def test_safe_mode_added_after_print_when_supported(self):
        """--help 输出里有 --safe-mode：argv 带上它，且紧跟在 -p 后面。"""
        r = FakeRunner(ok_json({"is_error": False, "result": "ok"}),
                       help_stdout=self.HELP_WITH_SAFE_MODE)
        self._provider(r).complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50)
        argv = r.calls[0]["argv"]
        self.assertIn("--safe-mode", argv)
        # 位置必须在 -p 后面
        self.assertEqual(argv[argv.index("-p") + 1], "--safe-mode")
        # 探测命令本身：--help、15 秒超时、cwd 是专用工作目录
        self.assertEqual(len(r.help_calls), 1)
        help_call = r.help_calls[0]
        self.assertEqual(help_call["argv"], ["/tmp/fake-claude", "--help"])
        self.assertEqual(help_call["timeout_s"], 15)

    def test_safe_mode_absent_when_help_lacks_flag(self):
        """--help 输出里没有 --safe-mode（旧版）：argv 不带它，调用照常成功。"""
        r = FakeRunner(ok_json({"is_error": False, "result": "旧版也行"}),
                       help_stdout="Usage: claude [options] --model -p")
        text = self._provider(r).complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50)
        self.assertEqual(text, "旧版也行")
        self.assertNotIn("--safe-mode", r.calls[0]["argv"])

    def test_safe_mode_absent_when_help_times_out(self):
        """--help 超时：当作不支持，complete() 照常工作。"""
        r = FakeRunner(ok_json({"is_error": False, "result": "ok"}),
                       help_result=ProcResult(-1, "", "", timed_out=True))
        text = self._provider(r).complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50)
        self.assertEqual(text, "ok")
        self.assertNotIn("--safe-mode", r.calls[0]["argv"])

    def test_safe_mode_absent_when_help_fails(self):
        """--help 退出码非 0：当作不支持，complete() 照常工作。"""
        r = FakeRunner(ok_json({"is_error": False, "result": "ok"}),
                       help_result=ProcResult(1, "", "boom"))
        text = self._provider(r).complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50)
        self.assertEqual(text, "ok")
        self.assertNotIn("--safe-mode", r.calls[0]["argv"])

    def test_help_probed_only_once_per_instance(self):
        """同一个实例连续两次 complete()：--help 只运行一次，结果被缓存。"""
        r = FakeRunner(ok_json({"is_error": False, "result": "a"}),
                       ok_json({"is_error": False, "result": "b"}),
                       help_stdout=self.HELP_WITH_SAFE_MODE)
        provider = self._provider(r)
        self.assertEqual(provider.complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50), "a")
        self.assertEqual(provider.complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50), "b")
        self.assertEqual(len(r.help_calls), 1)
        # 两次真正的调用都带上了 --safe-mode（缓存结果是"支持"）
        for call in r.calls:
            self.assertIn("--safe-mode", call["argv"])

    def test_env_home_user_no_config_dir(self):
        r = FakeRunner(ok_json({"is_error": False, "result": "ok"}))
        self._provider(r).complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50)
        env = r.calls[0]["env"]
        # 补上 HOME 和 USER
        self.assertIn("HOME", env)
        self.assertIn("USER", env)
        # 去掉 CLAUDE_CONFIG_DIR
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        # PATH 包含常见安装位置
        self.assertIn(".local/bin", env["PATH"])
        self.assertIn("/opt/homebrew/bin", env["PATH"])
        self.assertIn("/usr/local/bin", env["PATH"])

    def test_timeout(self):
        r = FakeRunner(ProcResult(-1, "", "", timed_out=True))
        with self.assertRaises(LLMError) as ctx:
            self._provider(r).complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50)
        self.assertEqual(ctx.exception.code, "timeout")

    def test_not_logged_in(self):
        r = FakeRunner(ProcResult(1, "", "Error: Not logged in. Run claude auth login"))
        with self.assertRaises(LLMError) as ctx:
            self._provider(r).complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50)
        self.assertEqual(ctx.exception.code, "not_logged_in")

    def test_bad_output_not_json(self):
        r = FakeRunner(ProcResult(0, "这不是 JSON", ""))
        with self.assertRaises(LLMError) as ctx:
            self._provider(r).complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50)
        self.assertEqual(ctx.exception.code, "bad_output")

    def test_is_error_output(self):
        r = FakeRunner(ok_json({"is_error": True, "result": "model overloaded"}))
        with self.assertRaises(LLMError) as ctx:
            self._provider(r).complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50)
        self.assertEqual(ctx.exception.code, "unavailable")

    def test_is_error_rate_limited(self):
        r = FakeRunner(ok_json({"is_error": True, "result": "rate limit exceeded"}))
        with self.assertRaises(LLMError) as ctx:
            self._provider(r).complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50)
        self.assertEqual(ctx.exception.code, "quota_or_rate_limit")

    def test_check_logged_in(self):
        r = FakeRunner(ProcResult(0, json.dumps({"loggedIn": True}), ""))
        ok, detail = self._provider(r).check()
        self.assertTrue(ok)
        # check 用 auth status 子命令，不消耗用量
        self.assertIn("auth", r.calls[0]["argv"])

    def test_check_not_logged_in(self):
        r = FakeRunner(ProcResult(0, json.dumps({"loggedIn": False}), ""))
        ok, detail = self._provider(r).check()
        self.assertFalse(ok)

    def test_check_timeout(self):
        r = FakeRunner(ProcResult(-1, "", "", timed_out=True))
        ok, detail = self._provider(r).check()
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# codex
# ---------------------------------------------------------------------------
class TestCodex(unittest.TestCase):
    def _provider(self, runner):
        from xhs_wiki.llm.codex import CodexProvider

        return CodexProvider(model=FAKE_MODEL, explicit_path="/tmp/fake-codex", runner=runner)

    def test_success(self):
        # codex 成功时会把最终回复写进 -o 文件，这里让假 runner 模拟这个行为
        def on_call(argv, env, input, timeout_s, cwd=None):
            out_path = argv[argv.index("-o") + 1]
            Path(out_path).write_text("整理结果", encoding="utf-8")
            return ProcResult(returncode=0, stdout="events...", stderr="")

        r = FakeRunner(on_call=on_call)
        text = self._provider(r).complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=100)
        self.assertEqual(text, "整理结果")

    def test_argv_and_stdin(self):
        def on_call(argv, env, input, timeout_s, cwd=None):
            out_path = argv[argv.index("-o") + 1]
            Path(out_path).write_text("ok", encoding="utf-8")
            return ProcResult(returncode=0, stdout="", stderr="")

        r = FakeRunner(on_call=on_call)
        self._provider(r).complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50)
        call = r.calls[0]
        argv = call["argv"]
        self.assertEqual(argv[0], "/tmp/fake-codex")
        # -C 指向专用工作目录（见 test_x98 的详细断言），exec 跟在后面
        self.assertEqual(argv[1], "-C")
        self.assertEqual(argv[3], "exec")
        self.assertIn("--skip-git-repo-check", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn("--ephemeral", argv)
        self.assertEqual(argv[argv.index("-m") + 1], FAKE_MODEL)
        # 位置参数 "-" 表示从标准输入读提示词
        self.assertEqual(argv[-1], "-")
        # system 拼在提示词开头，都从标准输入传入
        self.assertIn(SYSTEM, call["input"])
        self.assertIn(PROMPT, call["input"])
        self.assertNotIn(PROMPT, argv)

    def test_timeout(self):
        r = FakeRunner(ProcResult(-1, "", "", timed_out=True))
        with self.assertRaises(LLMError) as ctx:
            self._provider(r).complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50)
        self.assertEqual(ctx.exception.code, "timeout")

    def test_not_logged_in(self):
        r = FakeRunner(ProcResult(1, "", "Not logged in: run codex login"))
        with self.assertRaises(LLMError) as ctx:
            self._provider(r).complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50)
        self.assertEqual(ctx.exception.code, "not_logged_in")

    def test_exit_nonzero_unavailable(self):
        r = FakeRunner(ProcResult(2, "", "some other failure"))
        with self.assertRaises(LLMError) as ctx:
            self._provider(r).complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50)
        self.assertEqual(ctx.exception.code, "unavailable")

    def test_output_file_missing(self):
        # 成功退出但没写 -o 文件（runner 什么都不写）
        r = FakeRunner(ProcResult(0, "", ""))
        with self.assertRaises(LLMError) as ctx:
            self._provider(r).complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50)
        self.assertEqual(ctx.exception.code, "bad_output")

    def test_check_logged_in(self):
        r = FakeRunner(ProcResult(0, "Logged in using ChatGPT", ""))
        ok, _ = self._provider(r).check()
        self.assertTrue(ok)
        self.assertIn("login", r.calls[0]["argv"])
        self.assertIn("status", r.calls[0]["argv"])

    def test_check_not_logged_in(self):
        r = FakeRunner(ProcResult(1, "", "Not logged in"))
        ok, _ = self._provider(r).check()
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# anthropic / openai_compatible
# ---------------------------------------------------------------------------
class FakeMessage:
    """假的 anthropic Message 返回值：content 是文本块列表。"""

    def __init__(self, texts):
        self.content = [type("Block", (), {"type": "text", "text": t})() for t in texts]


class FakeChatCompletions:
    """假的 openai chat.completions：按预设返回或抛异常。"""

    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.exc is not None:
            raise self.exc
        return self.payload


class FakeOpenAIClient:
    def __init__(self, chat):
        self.chat = type("Chat", (), {"completions": chat})()


class FakeAnthropicMessages:
    """假的 Messages 资源；create 只属于这个资源，不属于客户端。"""

    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.kwargs = None

    def create(self, **kwargs):
        """记录请求参数，再按预设返回消息或抛出 SDK 异常。"""
        self.kwargs = kwargs
        if self.exc is not None:
            raise self.exc
        return self.payload


class FakeAnthropicClient:
    """假的 anthropic.Anthropic：严格提供 messages.create 这条调用路径。"""

    def __init__(self, payload=None, exc=None):
        self.messages = FakeAnthropicMessages(payload=payload, exc=exc)
        self.init_kwargs = None

    # Anthropic() 构造器本身被 client_factory 换掉，这里模拟类式调用
    @classmethod
    def as_factory(cls, payload=None, exc=None):
        captured = {}

        def factory(**kwargs):
            client = cls(payload=payload, exc=exc)
            client.init_kwargs = kwargs
            captured["kwargs"] = kwargs
            return client

        return factory, captured

class TestAnthropicApi(unittest.TestCase):
    def _provider(self, payload=None, exc=None, key=FAKE_KEY):
        factory, captured = FakeAnthropicClient.as_factory(payload=payload, exc=exc)
        from xhs_wiki.llm.anthropic_api import AnthropicApiProvider

        provider = AnthropicApiProvider(
            model=FAKE_MODEL,
            key_provider=(lambda: key) if isinstance(key, str) else key,
            client_factory=factory,
        )
        return provider, captured

    def test_success(self):
        provider, _ = self._provider(payload=FakeMessage(["整理", "好了"]))
        text = provider.complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=80)
        self.assertEqual(text, "整理好了")

    def test_request_params(self):
        provider, captured = self._provider(payload=FakeMessage(["ok"]))
        provider.complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=80)
        # max_retries=0、api_key 来自 key_provider（client_factory 收到的 kwargs）
        self.assertEqual(captured["kwargs"]["api_key"], FAKE_KEY)
        self.assertEqual(captured["kwargs"]["max_retries"], 0)

    def test_auth_failed(self):
        from anthropic import AuthenticationError

        err = _make_anthropic_status_error(AuthenticationError, 401)
        provider, _ = self._provider(exc=err)
        with self.assertRaises(LLMError) as ctx:
            provider.complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=80)
        self.assertEqual(ctx.exception.code, "auth_failed")

    def test_rate_limited(self):
        from anthropic import RateLimitError

        err = _make_anthropic_status_error(RateLimitError, 429)
        provider, _ = self._provider(exc=err)
        with self.assertRaises(LLMError) as ctx:
            provider.complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=80)
        self.assertEqual(ctx.exception.code, "quota_or_rate_limit")

    def test_timeout(self):
        provider, _ = self._provider(exc=_anthropic_timeout_error())
        with self.assertRaises(LLMError) as ctx:
            provider.complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=80)
        self.assertEqual(ctx.exception.code, "timeout")

    def test_key_provider_fails(self):
        def broken():
            raise RuntimeError("keychain 里没有条目（虚构）")

        provider, _ = self._provider(key=broken)
        with self.assertRaises(LLMError) as ctx:
            provider.complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=80)
        self.assertEqual(ctx.exception.code, "auth_failed")

    def test_no_text_block(self):
        provider, _ = self._provider(payload=FakeMessage([]))
        with self.assertRaises(LLMError) as ctx:
            provider.complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=80)
        self.assertEqual(ctx.exception.code, "bad_output")

    def test_check_ok(self):
        provider, _ = self._provider(payload=FakeMessage(["ok"]))
        ok, detail = provider.check()
        self.assertTrue(ok)

    def test_check_auth_failed(self):
        from anthropic import AuthenticationError

        provider, _ = self._provider(exc=_make_anthropic_status_error(AuthenticationError, 401))
        ok, detail = provider.check()
        self.assertFalse(ok)
        self.assertEqual(detail, "auth_failed")


def _make_anthropic_status_error(exc_cls, status_code):
    """构造 anthropic SDK 的 APIStatusError 子类实例（不发请求）。

    SDK 把 httpx 以 "httpx2" 的名字打进包里，构造 Request/Response
    要用这个名字（本机 anthropic 1.15.0 实测）。
    """

    import httpx2

    request = httpx2.Request("POST", "https://example.invalid/v1/messages")
    response = httpx2.Response(status_code=status_code, request=request)
    return exc_cls("fake error", response=response, body=None)


def _anthropic_timeout_error():
    """构造 anthropic 的 APITimeoutError（只收一个 request 参数）。"""

    import httpx2
    from anthropic import APITimeoutError

    return APITimeoutError(request=httpx2.Request("POST", "https://example.invalid/x"))


class TestOpenAICompatible(unittest.TestCase):
    def _provider(self, chat=None):
        from xhs_wiki.llm.openai_compatible import OpenAICompatibleProvider

        captured = {}

        def factory(**kwargs):
            captured["kwargs"] = kwargs
            return FakeOpenAIClient(chat)

        provider = OpenAICompatibleProvider(
            model=FAKE_MODEL,
            base_url="https://fake-openai.example/v1",
            key_provider=lambda: FAKE_KEY,
            client_factory=factory,
        )
        return provider, captured

    def test_success(self):
        msg = type("M", (), {"content": "整理结果"})()
        choice = type("C", (), {"message": msg})()
        payload = type("P", (), {"choices": [choice]})()
        chat = FakeChatCompletions(payload=payload)
        provider, _ = self._provider(chat)
        text = provider.complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=60)
        self.assertEqual(text, "整理结果")

    def test_request_params(self):
        msg = type("M", (), {"content": "ok"})()
        payload = type("P", (), {"choices": [type("C", (), {"message": msg})()]})()
        chat = FakeChatCompletions(payload=payload)
        provider, captured = self._provider(chat)
        provider.complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=60)
        # base_url 和 api_key 都传给了客户端
        self.assertEqual(captured["kwargs"]["base_url"], "https://fake-openai.example/v1")
        self.assertEqual(captured["kwargs"]["api_key"], FAKE_KEY)
        # 两条消息：system + user
        msgs = chat.kwargs["messages"]
        self.assertEqual([m["role"] for m in msgs], ["system", "user"])
        self.assertEqual(msgs[0]["content"], SYSTEM)
        self.assertEqual(msgs[1]["content"], PROMPT)

    def test_auth_failed(self):
        err = _make_openai_status_error(openai.AuthenticationError, 401)
        provider, _ = self._provider(FakeChatCompletions(exc=err))
        with self.assertRaises(LLMError) as ctx:
            provider.complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=60)
        self.assertEqual(ctx.exception.code, "auth_failed")

    def test_rate_limited(self):
        err = _make_openai_status_error(openai.RateLimitError, 429)
        provider, _ = self._provider(FakeChatCompletions(exc=err))
        with self.assertRaises(LLMError) as ctx:
            provider.complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=60)
        self.assertEqual(ctx.exception.code, "quota_or_rate_limit")

    def test_timeout(self):
        provider, _ = self._provider(FakeChatCompletions(exc=_openai_timeout_error()))
        with self.assertRaises(LLMError) as ctx:
            provider.complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=60)
        self.assertEqual(ctx.exception.code, "timeout")

    def test_empty_content(self):
        msg = type("M", (), {"content": ""})()
        payload = type("P", (), {"choices": [type("C", (), {"message": msg})()]})()
        provider, _ = self._provider(FakeChatCompletions(payload=payload))
        with self.assertRaises(LLMError) as ctx:
            provider.complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=60)
        self.assertEqual(ctx.exception.code, "bad_output")

    def test_base_url_required(self):
        from xhs_wiki.llm.openai_compatible import OpenAICompatibleProvider

        with self.assertRaises(ValueError):
            OpenAICompatibleProvider(
                model=FAKE_MODEL, base_url="", key_provider=lambda: FAKE_KEY
            )

    def test_check_rate_limited(self):
        err = _make_openai_status_error(openai.RateLimitError, 429)
        provider, _ = self._provider(FakeChatCompletions(exc=err))
        ok, detail = provider.check()
        self.assertFalse(ok)
        self.assertEqual(detail, "quota_or_rate_limit")


def _make_openai_status_error(cls, status_code):
    """构造 openai SDK 的 APIStatusError 子类实例（不发请求，用 httpx2）。"""

    import httpx2

    request = httpx2.Request("POST", "https://example.invalid/chat/completions")
    response = httpx2.Response(status_code=status_code, request=request)
    return cls("fake error", response=response, body=None)


def _openai_timeout_error():
    """构造 openai 的 APITimeoutError（只收一个 request 参数）。"""

    import httpx2
    from openai import APITimeoutError

    return APITimeoutError(request=httpx2.Request("POST", "https://example.invalid/x"))


# ---------------------------------------------------------------------------
# keys（钥匙串）
# ---------------------------------------------------------------------------
class TestKeychain(unittest.TestCase):
    def test_save_key_not_in_argv(self):
        r = FakeRunner(ProcResult(0, "", ""))
        Keychain(runner=r).save("anthropic", FAKE_KEY)
        call = r.calls[0]
        argv = call["argv"]
        # 命令行参数里不能出现 Key 本身
        self.assertNotIn(FAKE_KEY, argv)
        # Key 通过标准输入传入；-w 是最后一个选项且不带值
        self.assertEqual(call["input"], FAKE_KEY)
        self.assertEqual(argv[-1], "-w")
        # 账户名和服务名正确
        self.assertEqual(argv[argv.index("-a") + 1], "anthropic")
        self.assertEqual(argv[argv.index("-s") + 1], SERVICE_NAME)
        # -U 表示更新已存在条目
        self.assertIn("-U", argv)

    def test_load(self):
        r = FakeRunner(ProcResult(0, FAKE_KEY + "\n", ""))
        key = Keychain(runner=r).load("anthropic")
        self.assertEqual(key, FAKE_KEY)
        argv = r.calls[0]["argv"]
        self.assertIn("find-generic-password", argv)
        self.assertNotIn(FAKE_KEY, argv)

    def test_load_missing(self):
        r = FakeRunner(ProcResult(44, "", "could not be found"))
        with self.assertRaises(LLMError) as ctx:
            Keychain(runner=r).load("anthropic")
        self.assertEqual(ctx.exception.code, "auth_failed")

    def test_delete(self):
        r = FakeRunner(ProcResult(0, "", ""))
        Keychain(runner=r).delete("anthropic")
        self.assertIn("delete-generic-password", r.calls[0]["argv"])


# ---------------------------------------------------------------------------
# make_provider
# ---------------------------------------------------------------------------
class TestMakeProvider(unittest.TestCase):
    def test_creates_each_type(self):
        from xhs_wiki.llm.claude_code import ClaudeCodeProvider
        from xhs_wiki.llm.codex import CodexProvider
        from xhs_wiki.llm.anthropic_api import AnthropicApiProvider
        from xhs_wiki.llm.openai_compatible import OpenAICompatibleProvider

        p1 = make_provider(
            {"provider": "claude_code", "model": FAKE_MODEL},
            key_provider=lambda: FAKE_KEY,
        )
        self.assertIsInstance(p1, ClaudeCodeProvider)
        p2 = make_provider(
            {"provider": "codex", "model": FAKE_MODEL},
            key_provider=lambda: FAKE_KEY,
        )
        self.assertIsInstance(p2, CodexProvider)
        p3 = make_provider(
            {"provider": "anthropic", "model": FAKE_MODEL},
            key_provider=lambda: FAKE_KEY,
        )
        self.assertIsInstance(p3, AnthropicApiProvider)
        p4 = make_provider(
            {"provider": "openai_compatible", "model": FAKE_MODEL, "base_url": "https://fake.example/v1"},
            key_provider=lambda: FAKE_KEY,
        )
        self.assertIsInstance(p4, OpenAICompatibleProvider)

    def test_unknown_provider(self):
        with self.assertRaises(ValueError):
            make_provider(
                {"provider": "nope", "model": FAKE_MODEL},
                key_provider=lambda: FAKE_KEY,
            )

    def test_missing_model(self):
        with self.assertRaises(ValueError):
            make_provider(
                {"provider": "claude_code"},
                key_provider=lambda: FAKE_KEY,
            )

    def test_explicit_path_used(self):
        """设置里的 claude_path / codex_path 不再被当成可执行文件。

        控制台接口曾能把任意路径写进设置，守护进程照着执行。现在
        make_provider 完全不读这两个键；需要自定义路径时只能由调用方
        （测试、本地脚本）直接给提供方类传 explicit_path。
        """
        from xhs_wiki.llm.claude_code import ClaudeCodeProvider
        from xhs_wiki.llm.codex import CodexProvider

        runner = FakeRunner(ok_json({"is_error": False, "result": "ok"}))
        # 1. 设置里带了 claude_path / codex_path：make_provider 忽略它们。
        p = make_provider(
            {
                "provider": "claude_code",
                "model": FAKE_MODEL,
                "claude_path": "/tmp/fake-claude",
            },
            key_provider=lambda: FAKE_KEY,
            runner=runner,
        )
        self.assertNotEqual(p._exe, "/tmp/fake-claude")
        p = make_provider(
            {"provider": "codex", "model": FAKE_MODEL,
             "codex_path": "/tmp/fake-codex"},
            key_provider=lambda: FAKE_KEY,
            runner=runner,
        )
        self.assertNotEqual(p._exe, "/tmp/fake-codex")
        # 2. 显式参数（代码直接传入，不经设置）仍然生效。
        p = ClaudeCodeProvider(model=FAKE_MODEL,
                               explicit_path="/tmp/fake-claude", runner=runner)
        self.assertEqual(p._exe, "/tmp/fake-claude")
        p = CodexProvider(model=FAKE_MODEL,
                          explicit_path="/tmp/fake-codex", runner=runner)
        self.assertEqual(p._exe, "/tmp/fake-codex")


# ---------------------------------------------------------------------------
# 错误信息不泄露敏感内容
# ---------------------------------------------------------------------------
class TestNoSecretsInErrors(unittest.TestCase):
    def _run_and_collect(self, provider):
        try:
            provider.complete(system=SYSTEM, prompt=PROMPT, max_output_tokens=50)
        except LLMError as e:
            return str(e)
        return ""

    def test_cli_errors_no_prompt_or_key(self):
        from xhs_wiki.llm.claude_code import ClaudeCodeProvider
        from xhs_wiki.llm.codex import CodexProvider

        # 各种失败形态都跑一遍，收集错误信息
        cases = [
            (ClaudeCodeProvider(model=FAKE_MODEL, explicit_path="/tmp/fake-claude",
                                runner=FakeRunner(ProcResult(-1, "", "", timed_out=True))),
             ClaudeCodeProvider(model=FAKE_MODEL, explicit_path="/tmp/fake-claude",
                                runner=FakeRunner(ProcResult(1, "", "Not logged in"))),
             ClaudeCodeProvider(model=FAKE_MODEL, explicit_path="/tmp/fake-claude",
                                runner=FakeRunner(ProcResult(0, "not json", ""))),
             ClaudeCodeProvider(model=FAKE_MODEL, explicit_path="/tmp/fake-claude",
                                runner=FakeRunner(ok_json({"is_error": True, "result": "boom"})))),
            (CodexProvider(model=FAKE_MODEL, explicit_path="/tmp/fake-codex",
                           runner=FakeRunner(ProcResult(-1, "", "", timed_out=True))),
             CodexProvider(model=FAKE_MODEL, explicit_path="/tmp/fake-codex",
                           runner=FakeRunner(ProcResult(1, "", "Not logged in"))),
             CodexProvider(model=FAKE_MODEL, explicit_path="/tmp/fake-codex",
                           runner=FakeRunner(ProcResult(0, "", "")))),
        ]
        for providers in cases:
            for p in providers:
                msg = self._run_and_collect(p)
                self.assertNotIn(PROMPT, msg)
                self.assertNotIn(FAKE_KEY, msg)

    def test_api_errors_no_prompt_or_key(self):
        from anthropic import AuthenticationError
        from xhs_wiki.llm.anthropic_api import AnthropicApiProvider

        provider = AnthropicApiProvider(
            model=FAKE_MODEL,
            key_provider=lambda: FAKE_KEY,
            client_factory=FakeAnthropicClient.as_factory(
                exc=_make_anthropic_status_error(AuthenticationError, 401)
            )[0],
        )
        msg = self._run_and_collect(provider)
        self.assertNotIn(PROMPT, msg)
        self.assertNotIn(FAKE_KEY, msg)


# ---------------------------------------------------------------------------
# 环境构造与可执行文件发现
# ---------------------------------------------------------------------------
class TestEnvAndDiscovery(unittest.TestCase):
    def test_build_env_strips_config_dir(self):
        env = build_env(
            {"HOME": "/tmp/fake-home", "USER": "fakeuser",
             "CLAUDE_CONFIG_DIR": "/tmp/fake-config", "PATH": "/usr/bin:/bin"}
        )
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        self.assertEqual(env["HOME"], "/tmp/fake-home")
        self.assertEqual(env["USER"], "fakeuser")

    def test_find_claude_explicit(self):
        self.assertEqual(find_claude("/tmp/fake-claude"), "/tmp/fake-claude")

    def test_find_codex_fallback_skips_missing(self):
        # 显式路径优先；不存在的不兜底（兜底路径依赖本机安装，只测显式路径分支）
        self.assertEqual(find_codex("/tmp/fake-codex"), "/tmp/fake-codex")


if __name__ == "__main__":
    unittest.main()
