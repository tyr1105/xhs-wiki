# -*- coding: utf-8 -*-
"""test_notify.py —— 系统通知 notify.py 的单元测试。

覆盖：默认通知标题为"小红书收藏知识库"，下载熔断标题为"小红书下载暂停"；代码表与
store.NEEDS_USER_TITLES 保持一致（含 llm_unavailable / engine_unavailable）。

用假的 runner（普通函数）记录 osascript 的调用参数，不真的弹窗；
服务目录用临时目录。
"""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from xhs_wiki.notify import NOTIFY_TITLE, TEMPLATES, escape_applescript, notify
from xhs_wiki.store import ServiceStore

from test_store import at


class FakeRunner:
    """假的 subprocess.run：只记录收到的参数列表，返回一个成功结果。

    接受 timeout / capture_output 关键字参数（notify 用它们调用，
    真的 subprocess.run 也接受，假的要保持一致）。
    """

    def __init__(self, returncode=0):
        self.calls = []
        self.kwargs = []
        self._returncode = returncode

    def __call__(self, args, timeout=None, capture_output=False):
        self.calls.append(args)
        self.kwargs.append({"timeout": timeout, "capture_output": capture_output})
        return self

    @property
    def returncode(self):
        return self._returncode


class RaisingRunner:
    """假的 subprocess.run：直接抛 OSError（模拟 osascript 不存在/启动失败）。"""

    def __call__(self, args, timeout=None, capture_output=False):
        raise OSError("osascript not available")


class NotifyTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ServiceStore(Path(self.tmp.name) / "service")
        self.runner = FakeRunner()

    def tearDown(self):
        self.tmp.cleanup()


class TestNotify(NotifyTestBase):
    def test_send_once_and_record_time(self):
        now = at("2026-10-29", "08:00")
        self.assertTrue(notify("xhs_login", self.store, now=now, runner=self.runner))
        self.assertEqual(len(self.runner.calls), 1)
        # 参数列表启动 osascript，第一个元素是命令本身。系统
        # 命令写绝对路径，不按 PATH 查找（防止同名命令被优先执行）。
        self.assertEqual(self.runner.calls[0][0], "/usr/bin/osascript")
        self.assertEqual(self.runner.calls[0][1], "-e")
        self.assertIn("小红书登录已过期", self.runner.calls[0][2])
        self.assertIn(NOTIFY_TITLE, self.runner.calls[0][2])
        # 发送时间被记账
        self.assertEqual(self.store.read_notify_times().get("xhs_login"), now.isoformat())

    def test_title_is_new_name(self):
        # 标题用新名字"小红书收藏知识库"（早期叫"小红书收藏服务"）
        self.assertEqual(NOTIFY_TITLE, "小红书收藏知识库")

    def test_dedup_within_6_hours(self):
        t1 = at("2026-10-29", "08:00")
        self.assertTrue(notify("disk_low", self.store, now=t1, runner=self.runner))
        # 5 小时 59 分后再发 → 被去重
        t2 = t1 + timedelta(hours=5, minutes=59)
        self.assertFalse(notify("disk_low", self.store, now=t2, runner=self.runner))
        self.assertEqual(len(self.runner.calls), 1)

    def test_send_again_after_6_hours(self):
        t1 = at("2026-10-29", "08:00")
        self.assertTrue(notify("disk_low", self.store, now=t1, runner=self.runner))
        t2 = t1 + timedelta(hours=6, minutes=1)
        self.assertTrue(notify("disk_low", self.store, now=t2, runner=self.runner))
        self.assertEqual(len(self.runner.calls), 2)

    def test_different_codes_do_not_dedup_each_other(self):
        now = at("2026-10-29", "08:00")
        self.assertTrue(notify("xhs_login", self.store, now=now, runner=self.runner))
        self.assertTrue(notify("error", self.store, now=now, runner=self.runner))
        self.assertEqual(len(self.runner.calls), 2)

    def test_unknown_code_raises(self):
        with self.assertRaises(ValueError):
            notify("no_such_code", self.store, now=at("2026-10-29", "08:00"), runner=self.runner)
        self.assertEqual(len(self.runner.calls), 0)

    # --- 通知不能卡住 ---

    def test_runner_called_with_timeout_and_capture(self):
        # 调用 runner 时必须带 timeout=15 和 capture_output=True
        notify("xhs_login", self.store, now=at("2026-10-29", "08:00"), runner=self.runner)
        self.assertEqual(
            self.runner.kwargs[0], {"timeout": 15, "capture_output": True}
        )

    def test_runner_raises_returns_false_and_no_record(self):
        # runner 抛 OSError → 返回 False，且不写 notify.json（下次还能再试）
        result = notify(
            "xhs_login", self.store, now=at("2026-10-29", "08:00"), runner=RaisingRunner()
        )
        self.assertFalse(result)
        self.assertEqual(self.store.read_notify_times(), {})
        self.assertFalse((self.store.root / "notify.json").exists())

    def test_runner_nonzero_returncode_returns_false_and_no_record(self):
        runner = FakeRunner(returncode=1)
        result = notify("xhs_login", self.store, now=at("2026-10-29", "08:00"), runner=runner)
        self.assertFalse(result)
        self.assertEqual(self.store.read_notify_times(), {})
        self.assertFalse((self.store.root / "notify.json").exists())

    def test_failed_send_can_retry_immediately(self):
        # 失败没记账 → 下一次（同一时刻）还能再发
        self.assertFalse(
            notify("xhs_login", self.store, now=at("2026-10-29", "08:00"), runner=FakeRunner(returncode=1))
        )
        self.assertTrue(
            notify("xhs_login", self.store, now=at("2026-10-29", "08:00"), runner=self.runner)
        )

    def test_new_code_llm_unavailable_notifies(self):
        self.assertTrue(
            notify("llm_unavailable", self.store, now=at("2026-10-29", "08:00"), runner=self.runner)
        )
        self.assertIn("整理模型不可用", self.runner.calls[0][2])

    def test_new_code_engine_unavailable_notifies(self):
        self.assertTrue(
            notify("engine_unavailable", self.store, now=at("2026-10-29", "08:00"), runner=self.runner)
        )
        self.assertIn("引擎不可用", self.runner.calls[0][2])

    def test_download_blocked_title_body_and_dedup(self):
        """下载熔断发明确的暂停通知，正文固定，并沿用原有六小时去重。"""
        now = at("2026-10-29", "08:00")
        self.assertTrue(notify("xhs_download_blocked", self.store,
                               now=now, runner=self.runner))
        script = self.runner.calls[0][2]
        self.assertIn('with title "小红书下载暂停"', script)
        self.assertIn("连续几条笔记下载失败，可能被平台限制了，已暂停本轮下载；下一次定时运行会再试", script)
        self.assertFalse(notify("xhs_download_blocked", self.store,
                                now=now + timedelta(hours=1), runner=self.runner))
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.store.read_notify_times()["xhs_download_blocked"],
                         now.isoformat())


class TestTemplates(unittest.TestCase):
    def test_templates_match_design_table(self):
        # store.NEEDS_USER_TITLES 里的全部代码都要有固定中文文案，
        # 并且 wiki_desktop_open 已去掉
        self.assertEqual(
            set(TEMPLATES),
            {
                "xhs_login",
                "xhs_blocked",
                "xhs_download_blocked",
                "disk_low",
                "claude_logged_out",
                "manual_stop_present",
                "batch_paused",
                "error",
                "llm_unavailable",
                "engine_unavailable",
            },
        )
        for title in TEMPLATES.values():
            self.assertIsInstance(title, str)
            self.assertTrue(title)


class TestEscape(unittest.TestCase):
    def test_escape_quotes_and_backslashes(self):
        self.assertEqual(escape_applescript('说"你好"'), "说\\\"你好\\\"")
        self.assertEqual(escape_applescript("反\\斜杠"), "反\\\\斜杠")

    def test_escaped_body_stays_inside_quotes(self):
        # 含引号的正文转义后仍是合法的 AppleScript 字符串
        body = escape_applescript('带"引号"的正文')
        self.assertNotIn('"带"引号"的正文"', f'display notification "{body}"')


if __name__ == "__main__":
    unittest.main()
