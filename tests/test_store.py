# -*- coding: utf-8 -*-
"""test_store.py —— 存储层 store.py 的单元测试。

覆盖并发加固等已验证的行为（都要保住），以及
设置项（kb_root/engine/llm/asr_model/first_import_limit/setup_completed、
profile_url 可空）的校验测试。全部在临时目录里进行，时间由测试显式传入，
方便构造 "02:59 / 03:00 / 第二天" 这类边界时刻。

主页链接里的编号是代码生成的假值（'f'*24），不是任何真实用户的。
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from xhs_wiki.store import (
    BEIJING_TZ,
    DEFAULT_SETTINGS,
    ServiceStore,
    due_daily,
    due_followup,
    next_run_at,
    validate_settings,
)

# 代码生成的假主页编号（24 位十六进制），不是任何真实用户的。
FAKE_PROFILE = "https://www.xiaohongshu.com/user/profile/" + "f" * 24


def at(day: str, hhmm: str) -> datetime:
    """造一个北京时间的 datetime，如 at("2026-10-29", "03:00")。"""
    date = datetime.strptime(day, "%Y-%m-%d")
    hour, minute = (int(p) for p in hhmm.split(":"))
    return date.replace(hour=hour, minute=minute, tzinfo=BEIJING_TZ)


def valid_settings(**changes) -> dict:
    """以默认值为基础造一份合法设置（默认值本身必须通过校验）。"""
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    merged.update(changes)
    return merged


class StoreTestBase(unittest.TestCase):
    """公共脚手架：每个测试用独立的临时目录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "service"
        self.store = ServiceStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def file_mode(self, name) -> int:
        return stat.S_IMODE(os.stat(self.root / name).st_mode)


class TestSettings(StoreTestBase):
    # --- 默认值 ---

    def test_default_settings_when_file_missing(self):
        settings = self.store.load_settings()
        self.assertEqual(settings, DEFAULT_SETTINGS)
        # 默认值不必立刻写盘
        self.assertFalse((self.root / "settings.json").exists())

    def test_default_settings_pass_validation(self):
        # 默认值整体必须是合法的（否则新装用户第一次写盘就会失败）
        validate_settings(valid_settings())

    def test_saved_settings_roundtrip(self):
        self.store.update_settings({"daily_time": "05:30"})
        self.assertEqual(self.store.load_settings()["daily_time"], "05:30")
        self.assertEqual(self.store.load_settings()["max_new_per_run"], 100)

    def test_default_profile_url_is_none(self):
        self.assertIsNone(DEFAULT_SETTINGS["profile_url"])
        self.assertIsNone(self.store.load_settings()["profile_url"])

    def test_default_llm_shape(self):
        llm = self.store.load_settings()["llm"]
        self.assertEqual(llm["provider"], "claude_code")
        self.assertIsNone(llm["base_url"])

    # --- 合法修改 ---

    def test_update_editable_fields(self):
        new = self.store.update_settings(
            {"schedule_enabled": False, "daily_time": "23:59", "max_new_per_run": 1}
        )
        self.assertFalse(new["schedule_enabled"])
        self.assertEqual(new["daily_time"], "23:59")
        self.assertEqual(new["max_new_per_run"], 1)
        self.assertEqual(self.store.load_settings(), new)

    def test_profile_url_editable_and_nullable(self):
        # 先设为具体主页，再改回 null（用户重新登录前的情况）
        self.store.update_settings({"profile_url": FAKE_PROFILE})
        self.assertEqual(self.store.load_settings()["profile_url"], FAKE_PROFILE)
        self.store.update_settings({"profile_url": None})
        self.assertIsNone(self.store.load_settings()["profile_url"])

    def test_kb_root_engine_asr_setup_editable(self):
        new = self.store.update_settings(
            {
                "kb_root": "~/Documents/MyWiki",
                "engine": "llmwiki",
                "asr_model": "whisper",
                "setup_completed": True,
            }
        )
        self.assertEqual(new["engine"], "llmwiki")
        self.assertTrue(new["setup_completed"])

    def test_llm_editable(self):
        llm = {"provider": "openai_compatible", "model": "fake-model", "base_url": "https://api.example.test/v1"}
        new = self.store.update_settings({"llm": llm})
        self.assertEqual(new["llm"], llm)

    # --- 非法值：文件内容不变 ---

    def assert_rejected_keeps_file(self, changes):
        self.store.update_settings({"daily_time": "04:00"})
        before = (self.root / "settings.json").read_text()
        with self.assertRaises(ValueError):
            self.store.update_settings(changes)
        self.assertEqual((self.root / "settings.json").read_text(), before)

    def test_reject_bad_daily_time_24(self):
        self.assert_rejected_keeps_file({"daily_time": "24:00"})

    def test_reject_bad_daily_time_no_leading_zero(self):
        self.assert_rejected_keeps_file({"daily_time": "3:00"})

    def test_reject_max_new_zero(self):
        self.assert_rejected_keeps_file({"max_new_per_run": 0})

    def test_reject_max_new_over_200(self):
        self.assert_rejected_keeps_file({"max_new_per_run": 201})

    def test_reject_bool_as_number(self):
        self.assert_rejected_keeps_file({"max_new_per_run": True})

    def test_reject_unknown_field(self):
        self.assert_rejected_keeps_file({"super_mode": True})
        with self.assertRaises(ValueError) as ctx:
            validate_settings(valid_settings(foo=1))
        self.assertEqual(str(ctx.exception), "unknown_field:foo")

    def test_reject_not_editable_field(self):
        self.assert_rejected_keeps_file({"headless": False})
        self.assert_rejected_keeps_file({"schema_version": 2})

    def test_reject_bad_profile_url(self):
        for bad in ("https://example.com", "not a url", 123, ""):
            self.assert_rejected_keeps_file({"profile_url": bad})

    def test_reject_bad_engine(self):
        self.assert_rejected_keeps_file({"engine": "notion"})

    def test_reject_bad_asr_model(self):
        self.assert_rejected_keeps_file({"asr_model": "some_other_model"})

    def test_reject_bad_kb_root(self):
        for bad in ("", "   ", 123, None):
            self.assert_rejected_keeps_file({"kb_root": bad})

    def test_reject_bad_first_import_limit(self):
        for bad in (0, -1, 1001, True, "20"):
            self.assert_rejected_keeps_file({"first_import_limit": bad})

    def test_reject_bad_setup_completed(self):
        self.assert_rejected_keeps_file({"setup_completed": "yes"})

    # --- llm / provider / base_url 校验 ---

    def test_reject_unknown_provider(self):
        llm = {"provider": "gemini_cli", "model": "m", "base_url": None}
        self.assert_rejected_keeps_file({"llm": llm})

    def test_reject_llm_not_dict(self):
        self.assert_rejected_keeps_file({"llm": "claude"})

    def test_reject_empty_model(self):
        llm = {"provider": "anthropic", "model": "", "base_url": None}
        self.assert_rejected_keeps_file({"llm": llm})

    def test_openai_compatible_requires_valid_base_url(self):
        # 没有base_url / 非 https 且非本机 → 拒绝
        for base in (None, "", "http://api.example.test", "ftp://x", 123):
            llm = {"provider": "openai_compatible", "model": "m", "base_url": base}
            self.assert_rejected_keeps_file({"llm": llm})

    def test_openai_compatible_accepts_https_and_localhost(self):
        for base in ("https://api.example.test/v1", "http://127.0.0.1:8000/v1", "http://localhost:9000/"):
            llm = {"provider": "openai_compatible", "model": "m", "base_url": base}
            self.store.update_settings({"llm": llm})
            self.assertEqual(self.store.load_settings()["llm"]["base_url"], base)

    def test_other_providers_reject_base_url(self):
        # 只有 openai_compatible 需要 base_url，别的 provider 填了就拒绝
        llm = {"provider": "claude_code", "model": "m", "base_url": "https://api.example.test"}
        self.assert_rejected_keeps_file({"llm": llm})

    # --- 权限 ---

    def test_directory_mode_0700(self):
        mode = stat.S_IMODE(os.stat(self.root).st_mode)
        self.assertEqual(mode, 0o700)

    def test_settings_file_mode_0600(self):
        self.store.update_settings({"daily_time": "05:00"})
        self.assertEqual(self.file_mode("settings.json"), 0o600)


class TestStatus(StoreTestBase):
    def test_default_status(self):
        status = self.store.read_status()
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["needs_user"], [])
        self.assertIsNone(status["run_id"])

    def test_write_status_merges_and_stamps_updated_at(self):
        t1 = at("2026-10-29", "01:00")
        self.store.write_status(now=t1, state="running", step="scan")
        t2 = at("2026-10-29", "01:05")
        self.store.write_status(now=t2, step="compare")
        status = self.store.read_status()
        self.assertEqual(status["state"], "running")  # 旧字段保留
        self.assertEqual(status["step"], "compare")  # 新字段覆盖
        self.assertEqual(status["updated_at"], t2.isoformat())

    def test_needs_user_dedupe_keeps_first_since(self):
        t1 = at("2026-10-29", "08:00")
        self.store.add_needs_user("xhs_login", "第一次", now=t1)
        t2 = at("2026-10-29", "09:00")
        self.store.add_needs_user("xhs_login", "第二次", now=t2)
        items = self.store.read_status()["needs_user"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["since"], t1.isoformat())  # 保留首次时间
        self.assertEqual(items[0]["detail"], "第二次")  # 内容更新
        self.assertEqual(items[0]["title"], "小红书登录已过期，请在控制台扫码")

    def test_needs_user_multiple_codes_and_clear(self):
        self.store.add_needs_user("xhs_login", "a", now=at("2026-10-29", "08:00"))
        self.store.add_needs_user("disk_low", "b", now=at("2026-10-29", "08:01"))
        self.assertEqual(len(self.store.read_status()["needs_user"]), 2)
        self.store.clear_needs_user("xhs_login")
        items = self.store.read_status()["needs_user"]
        self.assertEqual([i["code"] for i in items], ["disk_low"])
        self.store.clear_needs_user()
        self.assertEqual(self.store.read_status()["needs_user"], [])

    def test_new_needs_user_codes(self):
        # 新增的两个代码都要有固定中文文案
        self.store.add_needs_user("llm_unavailable", "x", now=at("2026-10-29", "08:00"))
        self.store.add_needs_user("engine_unavailable", "y", now=at("2026-10-29", "08:01"))
        items = {i["code"]: i for i in self.store.read_status()["needs_user"]}
        self.assertIn("整理模型不可用", items["llm_unavailable"]["title"])
        self.assertIn("引擎不可用", items["engine_unavailable"]["title"])

    def test_wiki_desktop_open_removed(self):
        from xhs_wiki.store import NEEDS_USER_TITLES

        self.assertNotIn("wiki_desktop_open", NEEDS_USER_TITLES)
        with self.assertRaises(ValueError):
            self.store.add_needs_user("wiki_desktop_open", "x", now=at("2026-10-29", "08:00"))

    def test_unknown_needs_user_code_raises(self):
        with self.assertRaises(ValueError):
            self.store.add_needs_user("no_such", "x", now=at("2026-10-29", "08:00"))

    def test_status_file_mode_0600(self):
        self.store.write_status(now=at("2026-10-29", "08:00"), state="running")
        self.assertEqual(self.file_mode("status.json"), 0o600)


class TestRuns(StoreTestBase):
    def test_append_and_list_newest_first(self):
        self.store.append_run({"run_id": "a", "result": "completed"})
        self.store.append_run({"run_id": "b", "result": "error"})
        runs = self.store.list_runs()
        self.assertEqual([r["run_id"] for r in runs], ["b", "a"])

    def test_list_runs_skips_broken_lines(self):
        self.store.append_run({"run_id": "a", "result": "completed"})
        with open(self.root / "runs.jsonl", "a", encoding="utf-8") as f:
            f.write("{这不是合法JSON\n")
            f.write('"只是一个字符串"\n')
        self.store.append_run({"run_id": "c", "result": "paused"})
        runs = self.store.list_runs()
        self.assertEqual([r["run_id"] for r in runs], ["c", "a"])

    def test_list_runs_limit(self):
        for i in range(5):
            self.store.append_run({"run_id": f"r{i}"})
        runs = self.store.list_runs(limit=3)
        self.assertEqual([r["run_id"] for r in runs], ["r4", "r3", "r2"])

    def test_runs_file_mode_0600(self):
        self.store.append_run({"run_id": "a"})
        self.assertEqual(self.file_mode("runs.jsonl"), 0o600)


class TestScheduler(StoreTestBase):
    def test_default_scheduler(self):
        self.assertEqual(self.store.read_scheduler()["followups"], 0)
        self.assertIsNone(self.store.read_scheduler()["last_scheduled_date"])

    def test_write_scheduler_merges(self):
        self.store.write_scheduler(last_scheduled_date="2026-10-29")
        self.store.write_scheduler(followups=2)
        scheduler = self.store.read_scheduler()
        self.assertEqual(scheduler["last_scheduled_date"], "2026-10-29")
        self.assertEqual(scheduler["followups"], 2)


class TestPause(StoreTestBase):
    def test_pause_lifecycle(self):
        self.assertFalse(self.store.pause_requested())
        self.store.request_pause()
        self.assertTrue(self.store.pause_requested())
        self.assertEqual(self.file_mode("PAUSE"), 0o600)
        self.store.clear_pause()
        self.assertFalse(self.store.pause_requested())
        self.store.clear_pause()  # 重复清除不报错


class TestNextRunAt(unittest.TestCase):
    """纯函数 next_run_at 的边界：设定 03:00。"""

    SETTINGS = valid_settings()

    def test_disabled_returns_none(self):
        settings = valid_settings(schedule_enabled=False)
        self.assertIsNone(next_run_at(settings, at("2026-10-29", "15:00"), None))

    def test_before_time_returns_today(self):
        result = next_run_at(self.SETTINGS, at("2026-10-29", "02:59"), None)
        self.assertEqual(result, at("2026-10-29", "03:00"))

    def test_ran_today_returns_tomorrow(self):
        result = next_run_at(self.SETTINGS, at("2026-10-29", "15:00"), "2026-10-29")
        self.assertEqual(result, at("2026-10-30", "03:00"))

    def test_past_time_and_not_ran_returns_now(self):
        now = at("2026-10-29", "15:00")
        result = next_run_at(self.SETTINGS, now, None)
        self.assertEqual(result, now)

    def test_yesterday_ran_still_catchup_today(self):
        # 昨天跑过不影响今天的补跑
        now = at("2026-10-29", "15:00")
        result = next_run_at(self.SETTINGS, now, "2026-10-28")
        self.assertEqual(result, now)


class TestDueDaily(unittest.TestCase):
    SETTINGS = valid_settings()

    def test_at_exact_time_due(self):
        self.assertTrue(due_daily(self.SETTINGS, at("2026-10-29", "03:00"), None, False, False))

    def test_before_time_not_due(self):
        self.assertFalse(due_daily(self.SETTINGS, at("2026-10-29", "02:59"), None, False, False))

    def test_catchup_later_in_day(self):
        self.assertTrue(due_daily(self.SETTINGS, at("2026-10-29", "15:00"), None, False, False))

    def test_ran_today_not_due(self):
        self.assertFalse(due_daily(self.SETTINGS, at("2026-10-29", "15:00"), "2026-10-29", False, False))

    def test_next_day_does_not_rerun_yesterday(self):
        # 第二天 01:00：还没到今天的 03:00，不跑（也不补昨天）
        self.assertFalse(due_daily(self.SETTINGS, at("2026-10-30", "01:00"), "2026-10-29", False, False))

    def test_disabled_not_due(self):
        settings = valid_settings(schedule_enabled=False)
        self.assertFalse(due_daily(settings, at("2026-10-29", "15:00"), None, False, False))

    def test_paused_not_due(self):
        self.assertFalse(due_daily(self.SETTINGS, at("2026-10-29", "15:00"), None, False, True))

    def test_running_not_due(self):
        self.assertFalse(due_daily(self.SETTINGS, at("2026-10-29", "15:00"), None, True, False))


class TestDueFollowup(unittest.TestCase):
    def make_last_run(self, ended_at: datetime, result="incomplete"):
        return {"run_id": "svc-x", "result": result, "ended_at": ended_at.isoformat()}

    def test_after_60_minutes_due(self):
        ended = at("2026-10-29", "10:00")
        self.assertTrue(due_followup(self.make_last_run(ended), ended + timedelta(minutes=60), "2026-10-29", 0, False, False))

    def test_at_59_minutes_not_due(self):
        ended = at("2026-10-29", "10:00")
        self.assertFalse(due_followup(self.make_last_run(ended), ended + timedelta(minutes=59), "2026-10-29", 0, False, False))

    def test_completed_result_not_due(self):
        ended = at("2026-10-29", "10:00")
        self.assertFalse(due_followup(self.make_last_run(ended, "completed"), ended + timedelta(hours=2), "2026-10-29", 0, False, False))

    def test_no_last_run_not_due(self):
        self.assertFalse(due_followup(None, at("2026-10-29", "12:00"), "2026-10-29", 0, False, False))

    def test_six_followups_today_not_due(self):
        ended = at("2026-10-29", "06:00")
        self.assertFalse(due_followup(self.make_last_run(ended), at("2026-10-29", "15:00"), "2026-10-29", 6, False, False))

    def test_new_day_counter_resets(self):
        # 昨天 6 次用完，换天后计数按 0 算 → 可以跑
        ended = at("2026-10-28", "20:00")
        self.assertTrue(due_followup(self.make_last_run(ended), at("2026-10-29", "15:00"), "2026-10-28", 6, False, False))

    def test_running_or_paused_not_due(self):
        ended = at("2026-10-29", "10:00")
        self.assertFalse(due_followup(self.make_last_run(ended), at("2026-10-29", "12:00"), "2026-10-29", 0, True, False))
        self.assertFalse(due_followup(self.make_last_run(ended), at("2026-10-29", "12:00"), "2026-10-29", 0, False, True))


class TestHardening(StoreTestBase):
    """并发加固测试：默认值、临时文件、并发、向前兼容。"""

    def test_default_headless_is_true(self):
        self.assertTrue(DEFAULT_SETTINGS["headless"])
        self.assertTrue(self.store.load_settings()["headless"])

    # --- 临时文件不残留 ---

    def test_no_tmp_files_left_after_writes(self):
        # 各类写盘都做过之后，目录里不能有 *.tmp 残留
        self.store.update_settings({"daily_time": "05:00"})
        self.store.write_status(now=at("2026-10-29", "08:00"), state="running")
        self.store.write_scheduler(followups=1)
        self.store.write_notify_times({"xhs_login": at("2026-10-29", "08:00").isoformat()})
        self.store.append_run({"run_id": "a"})
        leftovers = [p.name for p in self.root.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_tmp_file_removed_on_write_error(self):
        # 写到一半出错（这里让 os.replace 抛错）也要清掉临时文件
        real_replace = os.replace

        def broken_replace(src, dst):
            raise OSError("disk full (simulated)")

        os.replace = broken_replace
        try:
            with self.assertRaises(OSError):
                self.store.write_json(Path("status.json"), {"state": "idle"})
        finally:
            os.replace = real_replace
        leftovers = [p.name for p in self.root.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    # --- 运行记录文件权限：一落地就是 0600 ---

    def test_runs_file_mode_0600_immediately(self):
        # append_run 用 os.open 以 0600 创建，没有"先 0644 再改"的窗口
        self.store.append_run({"run_id": "a"})
        self.assertEqual(self.file_mode("runs.jsonl"), 0o600)

    # --- 设置的向前兼容：旧文件缺字段时用默认值补齐 ---

    def test_old_settings_missing_field_filled_with_default(self):
        # 模拟旧版设置文件（没有 kb_root 字段）
        old = {k: v for k, v in DEFAULT_SETTINGS.items() if k != "kb_root"}
        self.store.write_json(Path("settings.json"), old)
        settings = self.store.load_settings()
        self.assertEqual(settings["kb_root"], "~/XHS-Wiki")  # 补齐成默认值
        # 其余字段保持文件里的值
        self.assertEqual(settings["daily_time"], old["daily_time"])

    def test_old_settings_missing_field_can_still_update(self):
        # 缺字段的旧文件也能正常走 update_settings（合并后校验通过）
        old = {k: v for k, v in DEFAULT_SETTINGS.items() if k != "engine"}
        self.store.write_json(Path("settings.json"), old)
        new = self.store.update_settings({"daily_time": "06:00"})
        self.assertEqual(new["daily_time"], "06:00")
        self.assertIn("engine", new)

    # --- 锁文件 ---

    def test_lock_file_created_with_mode_0600(self):
        with self.store.locked("status"):
            self.assertEqual(self.file_mode("status.lock"), 0o600)

    def test_unknown_lock_name_rejected(self):
        with self.assertRaises(ValueError):
            with self.store.locked("runner"):
                pass

    # --- 跨进程并发：两个进程同时 add_needs_user 不丢 ---

    def test_concurrent_add_needs_user_two_processes(self):
        # 用 subprocess 起两个真的独立进程（更贴近守护进程 + 运行子进程
        # 的真实场景），各自往同一个服务目录 add_needs_user 50 次。
        # 没有锁时，两个进程"读→改→写"会互相覆盖，最后只剩一个代码。
        worker_code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from xhs_wiki.store import ServiceStore, now_beijing\n"
            "root, code = sys.argv[1], sys.argv[2]\n"
            "store = ServiceStore(Path(root))\n"
            "for i in range(50):\n"
            "    store.add_needs_user(code, f'第{i}次', now=now_beijing())\n"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", worker_code, str(self.root), code],
            )
            for code in ("xhs_login", "disk_low")
        ]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        # 两个进程各加的代码都要在（没有锁时会丢一个）
        codes = sorted(i["code"] for i in self.store.read_status()["needs_user"])
        self.assertEqual(codes, ["disk_low", "xhs_login"])


if __name__ == "__main__":
    unittest.main()
