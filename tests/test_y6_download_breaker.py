"""下载熔断回归测试：验证三连败停止请求，同时保留台账和后续处理。

这个文件在 runner 的边界注入假下载器、假模型和假扫描，只用代码生成的
虚构笔记。运行目录、日志目录和技能目录均在临时区；解析和清理沿用既有
测试夹具，实际验证临时文件处理，不访问平台、浏览器或真实用户目录。
"""

from datetime import timedelta

import pytest

from xhs_wiki.store import due_daily, due_followup

from test_runner import FIXED_NOW, FakeEngine, Rig, fake_items, nid


BLOCKED = "xhs_download_blocked"
BLOCKED_TITLE = (
    "连续几条笔记下载失败，可能被平台限制了，已暂停本轮下载；"
    "下一次定时运行会再试"
)


@pytest.fixture
def make_rig(tmp_path, monkeypatch):
    """每例隔离三种运行目录，结束时关闭 SQLite 连接，避免句柄残留。"""
    monkeypatch.setenv("XHS_WIKI_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("XHS_WIKI_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("XHS_WIKI_SKILLS_ROOT", str(tmp_path / "skills"))
    rigs = []

    def create(**kwargs):
        """给每套依赖分配独立临时目录，以便多轮运行只共享自己的台账。"""
        rig = Rig(tmp_path / str(len(rigs)), monkeypatch, **kwargs)
        rigs.append(rig)
        return rig

    yield create
    for rig in rigs:
        rig.ledger.close()


def set_outcomes(rig, outcomes):
    """按笔记位置指定失败代码，None 表示成功；只记录实际收到的调用。"""
    by_id = {nid(i): code for i, code in enumerate(outcomes)}

    def download(note_id, signed_url, work_dir, *, vendor_dir=None):
        """失败只返回固定代码；成功复用写临时媒体文件的假下载器。"""
        code = by_id.get(note_id)
        if code is None:
            return rig._download_note(note_id, signed_url, work_dir,
                                      vendor_dir=vendor_dir)
        rig.download_calls.append(note_id)
        return {"status": "failed", "code": code,
                "media_kind": "text", "media": [], "meta": {}}

    rig.deps.download_note = download


def blocked_items(rig):
    """只取本卡事项，避免把其他需要处理的事项误判为下载熔断。"""
    return [item for item in rig.store.read_status()["needs_user"]
            if item["code"] == BLOCKED]


def assert_no_followup(rig, record):
    """对实际写盘记录核对一小时后的追加规则，避免只测构造出的假记录。"""
    saved = rig.store.list_runs(1)[0]
    assert saved["run_id"] == record["run_id"]
    assert saved["result"] == "needs_user"
    for hours in (1, 2, 24):
        later = FIXED_NOW + timedelta(hours=hours)
        assert not due_followup(saved, later, FIXED_NOW.date().isoformat(),
                                0, False, rig.store.pause_requested())


@pytest.mark.parametrize("codes", [
    ["metadata_unavailable"] * 3,
    ["network_error"] * 3,
    ["metadata_unavailable", "network_error", "metadata_unavailable"],
])
def test_three_failures_stop_before_fourth_and_preserve_attempts(make_rig, codes):
    """三连败只扣前三条的一次尝试，第四条及之后完全没有被请求。"""
    rig = make_rig(scan_items=fake_items(6))
    set_outcomes(rig, codes)

    record = rig.run()

    assert rig.download_calls == [nid(i) for i in range(3)]
    assert record["processed"]["download"] == {"ok": 0, "failed": 3}
    assert record["result"] == "needs_user"
    assert record["stop_reason"] == BLOCKED
    assert len(blocked_items(rig)) == 1
    assert blocked_items(rig)[0]["title"] == BLOCKED_TITLE
    assert rig.notified.count(BLOCKED) == 1
    for i in range(6):
        note = rig.note(i)
        assert note["stage"] == "discovered"
        assert note["attempts"] == (1 if i < 3 else 0)
        assert note["last_error"] == (codes[i] if i < 3 else None)
    assert_no_followup(rig, record)


def test_success_resets_consecutive_failure_count(make_rig):
    """两次失败、一次成功、再两次失败仍全部处理，不能累加到三连败。"""
    outcomes = ["metadata_unavailable", "network_error", None,
                "network_error", "metadata_unavailable", None]
    rig = make_rig(scan_items=fake_items(len(outcomes)))
    set_outcomes(rig, outcomes)

    record = rig.run()

    assert rig.download_calls == [nid(i) for i in range(len(outcomes))]
    assert record["processed"]["download"] == {"ok": 2, "failed": 4}
    assert record["result"] == "incomplete"
    assert blocked_items(rig) == []
    assert BLOCKED not in rig.notified


@pytest.mark.parametrize("other_code", [
    "media_check_failed", "file_too_large", "disk_low",
])
def test_other_failure_breaks_target_error_streak(make_rig, other_code):
    """非目标错误不计入三连败，也截断前后两段目标失败。"""
    outcomes = ["metadata_unavailable", "network_error", other_code,
                "network_error", "metadata_unavailable", None]
    rig = make_rig(scan_items=fake_items(len(outcomes)))
    set_outcomes(rig, outcomes)

    record = rig.run()

    assert rig.download_calls == [nid(i) for i in range(len(outcomes))]
    assert record["processed"]["download"] == {"ok": 1, "failed": 5}
    assert rig.note(2)["last_error"] == other_code
    assert blocked_items(rig) == []
    assert record["result"] == "incomplete"


def test_download_breaker_keeps_parse_ingest_distill_and_cleanup(make_rig):
    """下载成功的笔记仍走完后续阶段，真实临时原件也按原规则清掉。"""
    rig = make_rig(scan_items=fake_items(5))
    set_outcomes(rig, [None, "metadata_unavailable", "network_error",
                       "metadata_unavailable", None])

    record = rig.run()

    assert rig.download_calls == [nid(i) for i in range(4)]
    assert rig.build_calls == [nid(0)]
    assert rig.engine.ingest_calls == [[nid(0)]]
    assert rig.engine.distill_calls == [[nid(0)]]
    for stage in ("parse", "ingest", "distill"):
        assert record["processed"][stage]["ok"] == 1
    assert rig.note(0)["stage"] == "distilled"
    assert rig.note(0)["cleanup_status"] == "cleaned"
    assert record["cleanup"]["ran"] is True
    assert record["cleanup"]["notes"] == 1
    assert record["cleanup"]["freed_bytes"] > 0
    assert not list((rig.home / "work" / nid(0) / "media").rglob("*.mp4"))
    assert record["result"] == "needs_user"


@pytest.mark.parametrize("scan_incomplete,llm_quota", [
    (True, False), (False, True), (True, True),
])
def test_download_breaker_precedes_incomplete_reasons(make_rig, scan_incomplete,
                                                      llm_quota):
    """即使扫描不完整或模型额度耗尽，下载熔断仍阻止一小时后的追加。"""
    engine = FakeEngine(
        llm_error=("ingest", "quota_or_rate_limit") if llm_quota else None)
    rig = make_rig(scan_items=fake_items(5), engine=engine,
                   scan_reason="scan_incomplete" if scan_incomplete else None,
                   nominal=10 if scan_incomplete else 5)
    set_outcomes(rig, [None, "metadata_unavailable", "network_error",
                       "metadata_unavailable", None])

    record = rig.run()

    assert record["scan"]["incomplete"] is scan_incomplete
    assert record["llm_break"] == ("quota_or_rate_limit" if llm_quota else None)
    assert record["stop_reason"] == BLOCKED
    assert record["result"] == "needs_user"
    assert_no_followup(rig, record)


def test_next_daily_run_still_due_after_breaker(make_rig):
    """熔断不写用户暂停标记，翌日定时条件成立时照常允许再试。"""
    rig = make_rig(scan_items=fake_items(4))
    set_outcomes(rig, ["network_error"] * 4)
    record = rig.run(trigger="schedule")

    assert record["result"] == "needs_user"
    assert not rig.store.pause_requested()
    assert due_daily(rig.store.load_settings(), FIXED_NOW + timedelta(days=1),
                     FIXED_NOW.date().isoformat(), False,
                     rig.store.pause_requested())
    assert_no_followup(rig, record)


@pytest.mark.parametrize("next_round", ["preflight_failure", "scan_failure",
                                        "no_download", "one_failure"])
def test_old_issue_remains_without_download_success(make_rig, next_round):
    """未真正下载成功不能证明问题恢复，预检失败或空轮次也不得清事项。"""
    rig = make_rig(scan_items=fake_items(4))
    set_outcomes(rig, ["network_error"] * 4)
    assert rig.run()["result"] == "needs_user"
    original = blocked_items(rig)[0]

    if next_round == "preflight_failure":
        rig.provider.ok = False
    elif next_round == "scan_failure":
        rig.scan_result["status"] = "login_required"
    else:
        # 无下载时把已发现记录推到成功终态，避免遗留队列意外成功；
        # 单次失败时保留首条，其余终态，只检验没有达到新一轮三连败。
        for i in range(4):
            if next_round == "no_download" or i > 0:
                rig.ledger.advance(nid(i), "distilled")

    rig.run()

    assert blocked_items(rig) == [original]


def test_later_download_success_clears_old_issue(make_rig):
    """后续任意一轮出现下载成功即可移除旧事项，不必等全批完成。"""
    rig = make_rig(scan_items=fake_items(4))
    set_outcomes(rig, ["network_error"] * 4)
    assert rig.run()["result"] == "needs_user"
    assert blocked_items(rig)
    set_outcomes(rig, [None, "network_error", "metadata_unavailable", None])

    record = rig.run()

    assert record["processed"]["download"]["ok"] == 2
    assert record["result"] == "incomplete"
    assert blocked_items(rig) == []


def test_success_then_new_three_failures_retains_current_issue(make_rig):
    """同轮先成功又达到三连败时，最终仍保留新触发的暂停事项。"""
    rig = make_rig(scan_items=fake_items(5))
    set_outcomes(rig, ["network_error"] * 5)
    assert rig.run()["result"] == "needs_user"
    set_outcomes(rig, [None, "network_error", "metadata_unavailable",
                       "network_error", None])

    record = rig.run()

    assert record["processed"]["download"] == {"ok": 1, "failed": 3}
    assert record["result"] == "needs_user"
    assert len(blocked_items(rig)) == 1
    assert rig.note(4)["attempts"] == 0
    assert_no_followup(rig, record)


def test_missing_links_share_metadata_failure_count(make_rig):
    """缺签名照常记 metadata_unavailable，与下载器失败合并计算三连败。"""
    items = fake_items(5)
    items[0]["signed_url"] = None
    items[2]["signed_url"] = None
    rig = make_rig(scan_items=items)
    set_outcomes(rig, [None, "network_error", None, None, None])

    record = rig.run()

    assert rig.download_calls == [nid(1)]
    assert record["processed"]["download"] == {"ok": 0, "failed": 3}
    assert [rig.note(i)["attempts"] for i in range(5)] == [1, 1, 1, 0, 0]
    assert [rig.note(i)["last_error"] for i in range(3)] == [
        "metadata_unavailable", "network_error", "metadata_unavailable"]
    assert record["result"] == "needs_user"


def test_consecutive_count_does_not_cross_runs(make_rig):
    """每轮只有两次失败永不按跨轮累计熔断，每笔自己的三次上限仍生效。"""
    rig = make_rig(scan_items=fake_items(2))
    set_outcomes(rig, ["network_error", "metadata_unavailable"])

    for expected_attempts in (1, 2, 3):
        record = rig.run()
        assert record["processed"]["download"]["failed"] == 2
        assert record["result"] != "needs_user"
        assert blocked_items(rig) == []
        assert [rig.note(i)["attempts"] for i in range(2)] == [expected_attempts] * 2
    assert all(rig.note(i)["stage"] == "failed:download" for i in range(2))
    assert len(rig.download_calls) == 6
    assert rig.run()["processed"]["download"]["failed"] == 0
    assert len(rig.download_calls) == 6


def test_weekly_retry_remains_due_only_after_interval(make_rig):
    """耗尽尝试的笔记保持原每周重试规则，重置后仍会受本轮三连败保护。"""
    items = fake_items(3)
    rig = make_rig(scan_items=items)
    rig.ledger.upsert_discovered(items)
    for i in range(3):
        for _ in range(3):
            rig.ledger.fail(nid(i), "discovered", "network_error")
    rig.store.write_scheduler(last_download_retry_at=FIXED_NOW.isoformat())
    set_outcomes(rig, ["network_error"] * 3)

    before = rig.run()

    assert before["retry"]["ran"] is False
    assert rig.download_calls == []
    assert [rig.note(i)["attempts"] for i in range(3)] == [3, 3, 3]
    rig.deps.now = lambda: FIXED_NOW + timedelta(days=7)

    after = rig.run()

    assert after["retry"] == {"ran": True, "retried": 3}
    assert after["result"] == "needs_user"
    assert rig.download_calls == [nid(i) for i in range(3)]
    assert [rig.note(i)["attempts"] for i in range(3)] == [1, 1, 1]
    assert rig.store.read_scheduler()["last_download_retry_at"] == (
        FIXED_NOW + timedelta(days=7)).isoformat()
