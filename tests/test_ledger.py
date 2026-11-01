# -*- coding: utf-8 -*-
"""test_ledger.py —— 收藏台账 ledger.py 的单元测试。

全部在临时目录里进行（数据库文件放 tmp_path），不碰真实的 APP_HOME。
测试里的笔记编号、标题、链接全部是代码生成的假值，签名 token 用
FAKE 占位，不含任何真实数据。

覆盖点：
- 新增和已有笔记的处理；
- 签名链接会被更新；
- 按收藏位置排序；
- 失败 3 次后停在失败状态；
- retry 能退回；
- 两个进程同时写不会出错（subprocess 起子进程）；
- 数据库文件权限是 0600；
- 在 repr 和异常信息里搜不到签名链接。
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from xhs_wiki.ledger import Ledger, MAX_ATTEMPTS, STAGES


def fake_note_id(seed: int) -> str:
    """生成假的 24 位十六进制笔记编号（确定性，方便断言）。"""
    return f"{seed:024x}"


def fake_signed_url(seed: int) -> str:
    """生成假的签名链接（带 FAKE 占位的 xsec_token，不对应任何真实笔记）。"""
    return f"https://www.xiaohongshu.com/explore/{fake_note_id(seed)}?xsec_token=FAKE{seed}"


def plain_url(seed: int) -> str:
    """假的不带签名的规范链接。"""
    return f"https://www.xiaohongshu.com/explore/{fake_note_id(seed)}"


def discovered_item(seed: int, position: int, signed=True):
    return {
        "note_id": fake_note_id(seed),
        "title": f"虚构标题{seed}",
        "url": plain_url(seed),
        "signed_url": fake_signed_url(seed) if signed else None,
        "list_position": position,
    }


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    """每个测试一个全新的台账（数据库在临时目录里）。"""
    led = Ledger(tmp_path / "ledger.sqlite3")
    yield led
    led.close()


class TestUpsertDiscovered:
    def test_new_note_inserted_at_discovered(self, ledger):
        added = ledger.upsert_discovered([discovered_item(1, 0)])
        assert added == [fake_note_id(1)]
        note = ledger.get(fake_note_id(1))
        assert note["stage"] == "discovered"
        assert note["attempts"] == 0
        assert note["title"] == "虚构标题1"
        assert note["first_seen_at"] is not None

    def test_existing_note_stage_not_changed(self, ledger):
        ledger.upsert_discovered([discovered_item(1, 0)])
        ledger.advance(fake_note_id(1), "downloaded")
        # 再次扫描发现同一条：阶段保持 downloaded，不算新增
        added = ledger.upsert_discovered([discovered_item(1, 5)])
        assert added == []
        assert ledger.get(fake_note_id(1))["stage"] == "downloaded"

    def test_signed_link_updated_on_rescan(self, ledger):
        # 每次扫描签名都会换新，台账里要跟着更新
        ledger.upsert_discovered([discovered_item(1, 0, signed=True)])
        assert ledger.signed_url(fake_note_id(1)) == fake_signed_url(1)
        new_item = discovered_item(1, 0, signed=True)
        new_item["signed_url"] = fake_signed_url(99)
        ledger.upsert_discovered([new_item])
        assert ledger.signed_url(fake_note_id(1)) == fake_signed_url(99)

    def test_list_position_updated_on_rescan(self, ledger):
        ledger.upsert_discovered([discovered_item(1, 7)])
        ledger.upsert_discovered([discovered_item(1, 2)])
        assert ledger.get(fake_note_id(1))["list_position"] == 2

    def test_batch_returns_only_new_ids(self, ledger):
        ledger.upsert_discovered([discovered_item(1, 0), discovered_item(2, 1)])
        added = ledger.upsert_discovered([discovered_item(2, 0), discovered_item(3, 2)])
        assert added == [fake_note_id(3)]

    def test_invalid_note_id_rejected(self, ledger):
        with pytest.raises(ValueError):
            ledger.upsert_discovered([{"note_id": "", "list_position": 0}])

    def test_missing_signed_url_ok(self, ledger):
        # signed_url 允许缺省（比如某种扫描结果拿不到签名）
        ledger.upsert_discovered([discovered_item(1, 0, signed=False)])
        assert ledger.signed_url(fake_note_id(1)) is None


class TestOrderingAndQuery:
    def test_next_batch_orders_by_list_position(self, ledger):
        # list_position 小 = 最新收藏，排前面
        ledger.upsert_discovered(
            [discovered_item(1, 10), discovered_item(2, 5), discovered_item(3, 20)]
        )
        batch = ledger.next_batch("discovered", 10)
        assert [n["note_id"] for n in batch] == [fake_note_id(2), fake_note_id(1), fake_note_id(3)]

    def test_next_batch_limit(self, ledger):
        ledger.upsert_discovered([discovered_item(i, i) for i in range(5)])
        assert len(ledger.next_batch("discovered", 2)) == 2

    def test_next_batch_excludes_exhausted_attempts(self, ledger):
        ledger.upsert_discovered([discovered_item(1, 0)])
        ledger.fail(fake_note_id(1), "discovered", "metadata_unavailable")
        ledger.fail(fake_note_id(1), "discovered", "metadata_unavailable")
        # attempts=2 < 3 还能取到
        assert len(ledger.next_batch("discovered", 10)) == 1
        ledger.fail(fake_note_id(1), "discovered", "metadata_unavailable")
        # 第 3 次失败后变成 failed:download，discovered 批次里不再出现
        assert ledger.next_batch("discovered", 10) == []

    def test_next_batch_respects_custom_max_attempts(self, ledger):
        ledger.upsert_discovered([discovered_item(1, 0)])
        ledger.fail(fake_note_id(1), "discovered", "x")
        assert ledger.next_batch("discovered", 10, max_attempts=1) == []
        assert len(ledger.next_batch("discovered", 10, max_attempts=2)) == 1

    def test_next_batch_unknown_stage_rejected(self, ledger):
        with pytest.raises(ValueError):
            ledger.next_batch("nope", 10)

    def test_all_notes_and_stage_filter(self, ledger):
        ledger.upsert_discovered([discovered_item(1, 0), discovered_item(2, 1)])
        ledger.advance(fake_note_id(2), "downloaded")
        assert len(ledger.all_notes()) == 2
        assert [n["note_id"] for n in ledger.all_notes(stage="discovered")] == [fake_note_id(1)]

    def test_get_missing_returns_none(self, ledger):
        assert ledger.get(fake_note_id(404)) is None

    def test_counts_by_stage(self, ledger):
        ledger.upsert_discovered([discovered_item(i, i) for i in range(4)])
        ledger.advance(fake_note_id(0), "downloaded")
        ledger.fail(fake_note_id(3), "discovered", "x")
        ledger.fail(fake_note_id(3), "discovered", "x")
        ledger.fail(fake_note_id(3), "discovered", "x")
        counts = ledger.counts()
        assert counts == {"discovered": 2, "downloaded": 1, "failed:download": 1}


class TestAdvance:
    def test_advance_resets_attempts_and_error(self, ledger):
        ledger.upsert_discovered([discovered_item(1, 0)])
        ledger.fail(fake_note_id(1), "discovered", "metadata_unavailable")
        ledger.advance(fake_note_id(1), "downloaded", media_kind="video")
        note = ledger.get(fake_note_id(1))
        assert note["stage"] == "downloaded"
        assert note["attempts"] == 0
        assert note["last_error"] is None
        assert note["media_kind"] == "video"

    def test_advance_to_deferred(self, ledger):
        ledger.upsert_discovered([discovered_item(1, 0)])
        ledger.advance(fake_note_id(1), "deferred", deferred_reason="content_thin")
        assert ledger.get(fake_note_id(1))["stage"] == "deferred"

    def test_advance_unknown_stage_rejected(self, ledger):
        ledger.upsert_discovered([discovered_item(1, 0)])
        with pytest.raises(ValueError):
            ledger.advance(fake_note_id(1), "finished")

    def test_advance_unknown_field_rejected(self, ledger):
        ledger.upsert_discovered([discovered_item(1, 0)])
        with pytest.raises(ValueError):
            ledger.advance(fake_note_id(1), "downloaded", attempts=5)

    def test_advance_missing_note_raises(self, ledger):
        with pytest.raises(ValueError):
            ledger.advance(fake_note_id(404), "downloaded")

    def test_stage_order_constants(self):
        assert STAGES == ("discovered", "downloaded", "parsed", "ingested", "distilled")
        assert MAX_ATTEMPTS == 3


class TestFailAndRetry:
    def test_fail_once_stays_at_stage(self, ledger):
        # 例子：下载失败 1 次后仍是 discovered、attempts=1
        ledger.upsert_discovered([discovered_item(1, 0)])
        ledger.fail(fake_note_id(1), "discovered", "metadata_unavailable")
        note = ledger.get(fake_note_id(1))
        assert note["stage"] == "discovered"
        assert note["attempts"] == 1
        assert note["last_error"] == "metadata_unavailable"

    def test_fail_three_times_lands_failed_stage(self, ledger):
        # 例子：失败 3 次后变成 failed:download
        ledger.upsert_discovered([discovered_item(1, 0)])
        for _ in range(3):
            ledger.fail(fake_note_id(1), "discovered", "media_check_failed")
        note = ledger.get(fake_note_id(1))
        assert note["stage"] == "failed:download"
        assert note["attempts"] == 3

    def test_failed_stays_after_more_fails(self, ledger):
        # 停在失败状态后继续 fail 不再改变阶段（防御性调用）
        ledger.upsert_discovered([discovered_item(1, 0)])
        for _ in range(5):
            ledger.fail(fake_note_id(1), "discovered", "x")
        assert ledger.get(fake_note_id(1))["stage"] == "failed:download"

    def test_fail_at_each_stage_names_right_op(self, ledger):
        cases = [
            ("discovered", "failed:download"),
            ("downloaded", "failed:parse"),
            ("parsed", "failed:ingest"),
            ("ingested", "failed:distill"),
        ]
        for i, (stage, expected) in enumerate(cases):
            note_id = fake_note_id(i)
            ledger.upsert_discovered([discovered_item(i, i)])
            ledger.advance(note_id, stage)
            for _ in range(3):
                ledger.fail(note_id, stage, "some_error")
            assert ledger.get(note_id)["stage"] == expected

    def test_fail_unknown_stage_rejected(self, ledger):
        ledger.upsert_discovered([discovered_item(1, 0)])
        with pytest.raises(ValueError):
            ledger.fail(fake_note_id(1), "distilled", "x")

    def test_fail_missing_note_raises(self, ledger):
        with pytest.raises(ValueError):
            ledger.fail(fake_note_id(404), "discovered", "x")

    def test_retry_returns_to_pre_fail_stage(self, ledger):
        # 失败前的阶段是 discovered（下载环节），retry 退回 discovered
        ledger.upsert_discovered([discovered_item(1, 0)])
        for _ in range(3):
            ledger.fail(fake_note_id(1), "discovered", "x")
        ledger.retry(fake_note_id(1))
        note = ledger.get(fake_note_id(1))
        assert note["stage"] == "discovered"
        assert note["attempts"] == 0
        assert note["last_error"] is None

    def test_retry_mid_chain_returns_to_input_stage(self, ledger):
        # 解析环节失败 → retry 退回 downloaded
        ledger.upsert_discovered([discovered_item(1, 0)])
        ledger.advance(fake_note_id(1), "downloaded")
        for _ in range(3):
            ledger.fail(fake_note_id(1), "downloaded", "ocr_failed")
        assert ledger.get(fake_note_id(1))["stage"] == "failed:parse"
        ledger.retry(fake_note_id(1))
        assert ledger.get(fake_note_id(1))["stage"] == "downloaded"

    def test_retry_not_failed_raises(self, ledger):
        ledger.upsert_discovered([discovered_item(1, 0)])
        with pytest.raises(ValueError):
            ledger.retry(fake_note_id(1))

    def test_retry_missing_note_raises(self, ledger):
        with pytest.raises(ValueError):
            ledger.retry(fake_note_id(404))


class TestPrivacy:
    """签名链接是敏感信息：不能出现在 repr、普通查询结果或异常信息里。"""

    def test_db_file_mode_0600(self, tmp_path):
        led = Ledger(tmp_path / "ledger.sqlite3")
        try:
            mode = stat.S_IMODE(os.stat(tmp_path / "ledger.sqlite3").st_mode)
            assert mode == 0o600
        finally:
            led.close()

    def test_get_and_next_batch_exclude_signed_url(self, ledger):
        ledger.upsert_discovered([discovered_item(1, 0)])
        token = fake_signed_url(1)
        assert token not in repr(ledger.get(fake_note_id(1)))
        assert token not in repr(ledger.next_batch("discovered", 10))
        assert token not in repr(ledger.all_notes())
        assert "xsec_token" not in repr(ledger.get(fake_note_id(1)))

    def test_ledger_repr_has_no_signed_url(self, ledger):
        ledger.upsert_discovered([discovered_item(1, 0)])
        assert fake_signed_url(1) not in repr(ledger)

    def test_exceptions_carry_no_signed_url(self, ledger):
        # 各类错误路径的异常消息里都不能有签名链接（只有错误代码）
        ledger.upsert_discovered([discovered_item(1, 0)])
        token = fake_signed_url(1)
        for action in (
            lambda: ledger.advance(fake_note_id(1), "bad_stage"),
            lambda: ledger.fail(fake_note_id(1), "bad_stage", "x"),
            lambda: ledger.retry(fake_note_id(1)),
            lambda: ledger.next_batch("bad_stage", 1),
            lambda: ledger.upsert_discovered([{"note_id": ""}]),
            lambda: ledger.advance(fake_note_id(1), "downloaded", bad_column=1),
        ):
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - 就是要检查所有异常
                assert token not in str(exc)
                assert "xsec_token" not in str(exc)

    def test_signed_url_only_via_dedicated_method(self, ledger):
        ledger.upsert_discovered([discovered_item(1, 0)])
        # 唯一出口：signed_url()
        assert ledger.signed_url(fake_note_id(1)) == fake_signed_url(1)
        assert ledger.signed_url(fake_note_id(404)) is None


class TestConcurrency:
    """两个进程同时写同一个台账（subprocess 起真的子进程）。"""

    WORKER = (
        "import sys\n"
        "from pathlib import Path\n"
        "from xhs_wiki.ledger import Ledger\n"
        "db, seed = sys.argv[1], int(sys.argv[2])\n"
        "led = Ledger(Path(db))\n"
        "try:\n"
        "    for i in range(30):\n"
        "        item = {'note_id': f'{seed * 1000 + i:024x}',\n"
        "                'title': f'虚构{seed}-{i}',\n"
        "                'url': 'https://www.xiaohongshu.com/explore/' + f'{seed * 1000 + i:024x}',\n"
        "                'signed_url': 'https://x.test/explore?xsec_token=FAKEW' + str(seed),\n"
        "                'list_position': i}\n"
        "        led.upsert_discovered([item])\n"
        "        led.advance(item['note_id'], 'downloaded')\n"
        "        led.fail(item['note_id'], 'downloaded', 'fake_error')\n"
        "    counts = led.counts()\n"
        "    assert counts.get('failed:parse', 0) + counts.get('downloaded', 0) >= 0\n"
        "finally:\n"
        "    led.close()\n"
    )

    def test_two_processes_write_without_error(self, tmp_path):
        db = tmp_path / "ledger.sqlite3"
        # 先建库，两个子进程再并发写
        led = Ledger(db)
        led.close()
        procs = [
            subprocess.Popen([sys.executable, "-c", self.WORKER, str(db), str(seed)])
            for seed in (1, 2)
        ]
        for p in procs:
            assert p.wait(timeout=120) == 0, "子进程写入出错（并发不安全）"
        # 两边各 30 条都在，且都推进过阶段
        check = Ledger(db)
        try:
            assert check.counts()["downloaded"] == 60
        finally:
            check.close()

    def test_concurrent_upsert_same_note_no_duplicate(self, tmp_path):
        db = tmp_path / "ledger.sqlite3"
        led = Ledger(db)
        led.close()
        worker = (
            "import sys\n"
            "from pathlib import Path\n"
            "from xhs_wiki.ledger import Ledger\n"
            "db, pos = sys.argv[1], sys.argv[2]\n"
            "led = Ledger(Path(db))\n"
            "try:\n"
            "    for _ in range(20):\n"
            "        led.upsert_discovered([{'note_id': 'a' * 24,\n"
            "            'title': '虚构', 'url': 'https://x.test/e',\n"
            "            'signed_url': 'https://x.test/e?xsec_token=FAKEDUP',\n"
            "            'list_position': int(pos)}])\n"
            "finally:\n"
            "    led.close()\n"
        )
        procs = [
            subprocess.Popen([sys.executable, "-c", worker, str(db), str(pos)])
            for pos in (1, 2)
        ]
        for p in procs:
            assert p.wait(timeout=120) == 0
        check = Ledger(db)
        try:
            notes = check.all_notes()
            assert len(notes) == 1  # 同一条笔记不能被插成两行
            assert notes[0]["stage"] == "discovered"
        finally:
            check.close()


class TestSchemaMeta:
    def test_schema_version_written(self, ledger):
        row = ledger._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        assert row["value"] == "1"

    def test_wal_mode_enabled(self, ledger):
        mode = ledger._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_busy_timeout_set(self, ledger):
        value = ledger._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert value == 10000
