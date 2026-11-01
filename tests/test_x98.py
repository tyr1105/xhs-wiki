# -*- coding: utf-8 -*-
"""test_x98.py —— 服务工作目录、测试按钮超时、按钮布局的测试。

覆盖四件事：
1. config.llm_work_dir()：专用空目录、0700 权限、在 APP_HOME 下面。
2. claude / codex 两个提供方启动子进程时，cwd 都是这个专用目录；
   codex 的命令行还带 "-C <目录>"（它自己的切换目录参数）。
3. daemon.serve() 一进来就把工作目录切到 APP_HOME，切不过去就报错退出。
4. console.html：post() 支持超时参数且 test-llm 用 90 秒；等待期间按钮
   禁用、提示写明"最长约 1 分钟"；按钮不换行、提示单独占一行。
   （页面交互行为靠浏览器保证，这里做字符串断言。）

红线自查：全部用临时目录（conftest.py 已把 XHS_WIKI_HOME / XHS_WIKI_LOG_DIR
指到会话临时目录）；不调用真实的 claude / codex，不访问网络。
"""

import json
import os
import re
import stat
import unittest
from pathlib import Path

from xhs_wiki import config
from xhs_wiki.llm.base import ProcResult
from xhs_wiki.llm.claude_code import ClaudeCodeProvider
from xhs_wiki.llm.codex import CodexProvider

SRC = Path(__file__).resolve().parent.parent / "src" / "xhs_wiki"
HTML = (SRC / "web" / "console.html").read_text(encoding="utf-8")

# 虚构的可执行路径和模型名（不是任何真实程序的凭据）
FAKE_CLAUDE = "/tmp/fake-claude-x98"
FAKE_CODEX = "/tmp/fake-codex-x98"
FAKE_MODEL = "test-model-x98"
SYSTEM = "你是整理笔记的助手（测试用虚构提示词）"
PROMPT = "把这段测试笔记整理成条目（虚构内容）"


class RecordingRunner:
    """记录调用参数（含 cwd）并返回预设结果的假子进程执行器。

    和 test_llm.py 的 FakeRunner 一样，只是把 cwd 也记下来，
    供工作目录的断言用。
    """

    def __init__(self, *responses: ProcResult, write_output_file=False):
        self.responses = list(responses)
        self.write_output_file = write_output_file
        self.calls: list[dict] = []

    def __call__(self, argv, *, env, input, timeout_s, cwd=None):
        # claude_code 的 complete() 会先跑一次 --help 探测
        # --safe-mode，这里单独应答（默认输出里没有该词＝不支持），
        # 不消耗预设的 responses、不记进 calls。
        if "--help" in argv:
            return ProcResult(0, "Usage: claude [options] --model -p", "")
        self.calls.append(
            {"argv": list(argv), "cwd": cwd, "timeout_s": timeout_s}
        )
        # codex 的 -o 文件：模拟真实 codex 把最终回复写进去
        if self.write_output_file and "-o" in argv:
            Path(argv[argv.index("-o") + 1]).write_text(
                "虚构回复", encoding="utf-8")
        if not self.responses:
            raise AssertionError("RecordingRunner 没有预设的返回了")
        return self.responses.pop(0)


# ---------------------------------------------------------------------------
# 1. config.llm_work_dir()
# ---------------------------------------------------------------------------
class TestLlmWorkDir(unittest.TestCase):
    def test_under_app_home_with_mode(self):
        """llm-workdir 在 APP_HOME 下，存在且权限 0700。"""
        workdir = config.llm_work_dir()
        self.assertEqual(workdir, config.app_home() / "llm-workdir")
        self.assertTrue(workdir.is_dir())
        mode = stat.S_IMODE(workdir.stat().st_mode)
        self.assertEqual(mode, 0o700)

    def test_stays_empty(self):
        """目录保持为空：提供方只把它当 cwd，不往里写东西。"""
        workdir = config.llm_work_dir()
        # 反复调用不产生文件
        config.llm_work_dir()
        self.assertEqual(list(workdir.iterdir()), [])


# ---------------------------------------------------------------------------
# 2. 两个提供方的子进程 cwd
# ---------------------------------------------------------------------------
class TestClaudeCodeCwd(unittest.TestCase):
    def _provider(self, runner):
        return ClaudeCodeProvider(
            model=FAKE_MODEL, explicit_path=FAKE_CLAUDE, runner=runner)

    def test_complete_uses_workdir(self):
        r = RecordingRunner(ProcResult(
            0, json.dumps({"is_error": False, "result": "ok"}), ""))
        self._provider(r).complete(
            system=SYSTEM, prompt=PROMPT, max_output_tokens=10)
        self.assertEqual(len(r.calls), 1)
        cwd = r.calls[0]["cwd"]
        self.assertEqual(cwd, str(config.llm_work_dir()))
        # 目录真的存在（子进程的 cwd 必须已创建）
        self.assertTrue(Path(cwd).is_dir())

    def test_check_uses_workdir(self):
        r = RecordingRunner(ProcResult(0, json.dumps({"loggedIn": True}), ""))
        ok, _ = self._provider(r).check()
        self.assertTrue(ok)
        self.assertEqual(r.calls[0]["cwd"], str(config.llm_work_dir()))


class TestCodexCwd(unittest.TestCase):
    def _provider(self, runner):
        return CodexProvider(
            model=FAKE_MODEL, explicit_path=FAKE_CODEX, runner=runner)

    def test_complete_uses_workdir_and_dash_c(self):
        r = RecordingRunner(ProcResult(0, "", ""), write_output_file=True)
        self._provider(r).complete(
            system=SYSTEM, prompt=PROMPT, max_output_tokens=10)
        self.assertEqual(len(r.calls), 1)
        workdir = str(config.llm_work_dir())
        # 子进程 cwd 指向专用目录
        self.assertEqual(r.calls[0]["cwd"], workdir)
        # codex 自己的 -C 参数也指向同一个目录
        argv = r.calls[0]["argv"]
        self.assertIn("-C", argv)
        self.assertEqual(argv[argv.index("-C") + 1], workdir)
        self.assertTrue(Path(workdir).is_dir())

    def test_check_uses_workdir(self):
        r = RecordingRunner(ProcResult(0, "Logged in using ChatGPT", ""))
        ok, _ = self._provider(r).check()
        self.assertTrue(ok)
        self.assertEqual(r.calls[0]["cwd"], str(config.llm_work_dir()))


# ---------------------------------------------------------------------------
# 3. default_runner 的 cwd 透传（真实子进程，但不碰网络和模型：
#    只跑系统自带的 /bin/pwd，验证它真的在指定目录里执行）
# ---------------------------------------------------------------------------
class TestDefaultRunnerCwd(unittest.TestCase):
    def test_cwd_passthrough(self):
        from xhs_wiki.llm.base import default_runner
        import tempfile

        with tempfile.TemporaryDirectory(prefix="x98-pwd-") as tmp:
            result = default_runner(
                ["/bin/pwd"], env={}, input="", timeout_s=10, cwd=tmp)
        self.assertEqual(result.returncode, 0)
        # /bin/pwd 打印的就是我们指定的目录（resolve 是因为 macOS 会把
        # /var/… 规范化成 /private/var/…，两边都规范化后再比）
        self.assertEqual(Path(result.stdout.strip()).resolve(),
                         Path(tmp).resolve())


# ---------------------------------------------------------------------------
# 4. serve() 先切工作目录
# ---------------------------------------------------------------------------
class TestServeChdir(unittest.TestCase):
    def test_serve_chdirs_to_app_home(self):
        """serve() 一进来就把进程工作目录切到 APP_HOME。"""
        self._run_serve_and_check()

    def _run_serve_and_check(self):
        from xhs_wiki import daemon

        home = Path(config.app_home())
        seen = {}

        class FakeSupervisor:
            def __init__(self, store):
                self.store = store
                self.assets = None

            def cleanup_stale_running(self):
                pass

            def start(self):
                pass

            def on_assets_ready(self):
                pass

        class FakeAssets:
            def __init__(self, store):
                pass

            def start(self):
                pass

            def stop(self):
                # serve 的统一退出钩子会收尾下载器；替身不创建真实进程。
                pass

        class FakeServer:
            server_port = 18765

            def __init__(self, addr, supervisor=None):
                pass

            def serve_forever(self):
                # 记录 serve 期间进程的工作目录，然后立刻返回（不真起服务）
                seen["cwd"] = Path.cwd()

            def server_close(self):
                # 假服务没有端口，但要实现与真实服务一致的关闭接口。
                pass

        import unittest.mock as mock

        old_cwd = os.getcwd()
        try:
            with mock.patch.object(daemon, "Supervisor", FakeSupervisor), \
                 mock.patch.object(daemon, "AssetManager", FakeAssets), \
                 mock.patch.object(daemon, "ServiceServer", FakeServer), \
                 mock.patch.object(daemon, "remove_stale_login_qr", lambda s: None):
                daemon.serve()
        finally:
            os.chdir(old_cwd)
        # resolve：macOS 会把 /var/… 规范化成 /private/var/…，两边统一
        self.assertEqual(seen["cwd"].resolve(), home.resolve())

    def test_serve_chdir_failure_raises(self):
        """APP_HOME 切不过去时 serve() 直接报错退出。"""
        from xhs_wiki import daemon
        import tempfile
        import unittest.mock as mock

        with tempfile.TemporaryDirectory(prefix="x98-chdir-") as tmp:
            # 用一个普通文件当 APP_HOME：对文件 chdir 必然失败
            # （NotADirectoryError 是 OSError 的子类）
            not_a_dir = Path(tmp) / "file-home"
            not_a_dir.write_text("", encoding="utf-8")
            with mock.patch.object(daemon.config, "app_home",
                                   lambda: not_a_dir):
                with self.assertRaises(RuntimeError) as ctx:
                    daemon.serve()
        # 报错信息是错误代码，不带路径细节
        self.assertEqual(str(ctx.exception), "serve_chdir_failed: NotADirectoryError")


# ---------------------------------------------------------------------------
# 5. console.html 的字符串断言
# ---------------------------------------------------------------------------
class TestConsoleHtml(unittest.TestCase):
    def test_post_supports_timeout_param(self):
        """post() 有可选超时参数，默认 12 秒。"""
        self.assertIn("async function post(path,body,timeoutMs=12000)", HTML)
        self.assertIn("signal:AbortSignal.timeout(timeoutMs)", HTML)

    def test_test_llm_uses_90s(self):
        """test-llm 的请求用 90 秒超时（比服务端 60 秒检查超时长）。"""
        self.assertIn("post('/api/setup/test-llm',body,90000)", HTML)

    def test_wait_message_and_disabled_buttons(self):
        """等待期间按钮禁用，提示写明最长约 1 分钟。"""
        self.assertIn("saveBtn.disabled=true;testBtn.disabled=true;", HTML)
        self.assertIn("saveBtn.disabled=false;testBtn.disabled=false;", HTML)
        self.assertIn("测试中…（最长约 1 分钟）", HTML)
        # 旧的短提示不应再有
        self.assertNotIn("hint.textContent='测试中…'", HTML)

    def test_hint_on_own_line_below_buttons(self):
        """第 3 步的提示单独一行、放在按钮下面（不在按钮所在的 flex 行里）。"""
        # 按钮行 div 里只剩两个按钮，没有提示 span
        row = re.search(
            r'<div style="display:flex;gap:10px;margin-top:12px[^"]*">\s*'
            r'<button id="w-save-llm">.*?</div>', HTML, re.S)
        self.assertIsNotNone(row, "找不到第 3 步的按钮行")
        self.assertNotIn("w3-hint", row.group(0))
        # 提示在按钮行之后的独立段落里
        after = HTML[row.end():row.end() + 200]
        self.assertIn('id="w3-hint"', after)

    def test_buttons_no_wrap(self):
        """全局按钮样式加了 white-space:nowrap，按钮文字不会被挤成两行。"""
        self.assertRegex(HTML, r"button\{[^}]*white-space:nowrap")


if __name__ == "__main__":
    unittest.main()
