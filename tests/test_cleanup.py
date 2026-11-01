# -*- coding: utf-8 -*-
"""test_cleanup.py —— cleanup.py 的测试。

覆盖：三道核对（任何一道不通过都不删）、回执中断的处理、find_candidates
的候选条件、清理后台账和 scheduler.json 的更新。全部用临时目录和假引擎。
"""

import hashlib
import json
from datetime import datetime

import pytest

from xhs_wiki import cleanup, config
from xhs_wiki.ledger import Ledger
from xhs_wiki.store import BEIJING_TZ, ServiceStore

FIXED_NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=BEIJING_TZ)


def nid(i: int) -> str:
    """虚构的 24 位十六进制笔记编号。"""
    return f"{i + 1:024x}"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeEngine:
    """假引擎：source_exists 可配置；没有 source_exists 的引擎也有一个替身。"""

    name = "fake"

    def __init__(self, source_ok=True, with_source_exists=True):
        self.source_ok = source_ok
        self.with_source_exists = with_source_exists
        self.checked = []

    def source_exists(self, note_id, engine_ref):
        self.checked.append(note_id)
        return self.source_ok


class NoSourceCheckEngine:
    """没有 source_exists 方法的引擎（模拟旧引擎没实现该方法的情形）。"""

    name = "old"


class Fixture:
    """一条已到终态、带真实文件的笔记 + 台账 + 服务目录。"""

    def __init__(self, tmp_path, monkeypatch, *, stage="distilled",
                 index=0, extra_files=(), manifest_override=None,
                 package_bytes=None):
        self.home = tmp_path / "home"
        monkeypatch.setenv("XHS_WIKI_HOME", str(self.home))
        self.store = ServiceStore(config.app_home())
        self.ledger = Ledger(config.ledger_path())
        self.note_id = nid(index)
        work = config.work_dir(self.note_id)
        media_dir = work / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        # 写一个真实媒体文件（内容虚构）。
        self.media_data = f"虚构媒体-{index}".encode("utf-8")
        self.media_file = media_dir / f"{self.note_id}.mp4"
        self.media_file.write_bytes(self.media_data)
        manifest = [{"path": f"media/{self.note_id}.mp4",
                     "bytes": len(self.media_data),
                     "sha256": digest(self.media_data)}]
        # 清单外的文件（清理绝不能碰）。
        for name in extra_files:
            (media_dir / name).write_bytes(f"额外文件-{name}".encode("utf-8"))
        if manifest_override is not None:
            manifest = manifest_override
        package = {"note_id": self.note_id, "title": "虚构笔记",
                   "original_media": manifest}
        raw = package_bytes if package_bytes is not None else json.dumps(
            package, ensure_ascii=False, indent=2).encode("utf-8")
        self.package_path = work / "package.json"
        self.package_path.write_bytes(raw)
        # 登记 + 推进到终态（模拟一次完成的运行留下的状态）。
        self.ledger.upsert_discovered([{
            "note_id": self.note_id, "title": "虚构笔记",
            "url": f"https://www.xiaohongshu.com/explore/{self.note_id}",
            "signed_url": f"https://www.xiaohongshu.com/explore/{self.note_id}"
                          "?xsec_token=fake",
            "list_position": index}])
        self.ledger.advance(self.note_id, "downloaded")
        self.ledger.advance(self.note_id, "parsed",
                            package_sha256=digest(raw))
        self.ledger.advance(self.note_id, "ingested", engine="fake",
                            engine_ref=json.dumps({"source": "s.md"}))
        self.ledger.advance(self.note_id, stage)

    def find(self, engine=None):
        return cleanup.find_candidates(self.ledger, engine)

    def apply(self, candidates, engine):
        return cleanup.apply(candidates, ledger=self.ledger, engine=engine,
                             store=self.store, now=lambda: FIXED_NOW)


@pytest.fixture
def fx(tmp_path, monkeypatch):
    return Fixture(tmp_path, monkeypatch)


class TestFindCandidates:
    def test_distilled_with_media_is_candidate(self, fx):
        candidates = fx.find(FakeEngine())
        assert len(candidates) == 1
        assert candidates[0]["note_id"] == fx.note_id
        assert candidates[0]["total_bytes"] == len(fx.media_data)

    def test_non_terminal_stage_not_candidate(self, tmp_path, monkeypatch):
        fx = Fixture(tmp_path, monkeypatch, stage="ingested")
        assert fx.find(FakeEngine()) == []

    def test_already_cleaned_not_candidate(self, fx):
        fx.ledger.advance(fx.note_id, "distilled", cleanup_status="cleaned")
        assert fx.find(FakeEngine()) == []

    def test_no_media_files_not_candidate(self, fx):
        fx.media_file.unlink()
        assert fx.find(FakeEngine()) == []

    def test_deferred_is_candidate(self, tmp_path, monkeypatch):
        fx = Fixture(tmp_path, monkeypatch, stage="deferred")
        assert len(fx.find(FakeEngine())) == 1


class TestThreeChecks:
    def test_happy_path(self, fx):
        """三道核对全过：删文件、写回执、更新台账和 scheduler。"""
        engine = FakeEngine()
        outcome = fx.apply(fx.find(engine), engine)
        assert outcome["notes"] == 1
        assert outcome["freed_bytes"] == len(fx.media_data)
        assert outcome["skipped"] == []
        assert not fx.media_file.exists()
        assert not (config.work_dir(fx.note_id) / "media").exists()
        row = fx.ledger.get(fx.note_id)
        assert row["cleanup_status"] == "cleaned"
        assert row["freed_bytes"] == len(fx.media_data)
        assert row["cleaned_at"] == FIXED_NOW.isoformat()
        assert fx.store.read_scheduler()["cleanup_freed_bytes_total"] == \
            len(fx.media_data)
        # 回执是 completed 状态，权限 0600。
        receipt = config.work_dir(fx.note_id) / "cleanup-receipt.json"
        data = json.loads(receipt.read_text(encoding="utf-8"))
        assert data["status"] == "completed"
        assert receipt.stat().st_mode & 0o777 == 0o600

    def test_check1_source_missing(self, fx):
        """第 1 道不通过（来源不在引擎里）：不删。"""
        outcome = fx.apply(fx.find(FakeEngine(source_ok=False)),
                           FakeEngine(source_ok=False))
        assert outcome["notes"] == 0
        assert outcome["skipped"][0]["reason"] == "source_not_in_engine"
        assert fx.media_file.exists()
        assert fx.ledger.get(fx.note_id)["cleanup_status"] is None

    def test_check1_engine_lacks_method(self, fx):
        """引擎没有 source_exists 方法：按无法确认跳过。"""
        outcome = fx.apply(fx.find(NoSourceCheckEngine()), NoSourceCheckEngine())
        assert outcome["skipped"][0]["reason"] == "engine_lacks_source_exists"
        assert fx.media_file.exists()

    def test_check2_package_sha_mismatch(self, fx):
        """第 2 道不通过（解析包被改过）：不删。"""
        fx.package_path.write_text("{}篡改后的解析包{}", encoding="utf-8")
        outcome = fx.apply(fx.find(FakeEngine()), FakeEngine())
        assert outcome["skipped"][0]["reason"] == "package_sha256_mismatch"
        assert fx.media_file.exists()

    def test_check2_package_missing(self, fx):
        fx.package_path.unlink()
        outcome = fx.apply(fx.find(FakeEngine()), FakeEngine())
        assert outcome["skipped"][0]["reason"] == "package_missing"
        assert fx.media_file.exists()

    def test_check3_extra_file_preserved(self, fx):
        """media/ 里有清单外的文件：只删清单内的，清单外的一概不碰。"""
        (fx.media_file.parent / "extra.mp4").write_bytes(b"extra")
        outcome = fx.apply(fx.find(FakeEngine()), FakeEngine())
        # 核对只针对清单内的文件，删除也只删清单内的；清单外文件保留，
        # media 目录因此不空、不 rmdir。
        assert outcome["notes"] == 1
        assert not fx.media_file.exists()
        assert (fx.media_file.parent / "extra.mp4").exists()

    def test_check3_hash_changed(self, tmp_path, monkeypatch):
        """第 3 道不通过（文件内容和清单的 sha256 不一致）：不删。"""
        fx = Fixture(tmp_path, monkeypatch, manifest_override=[
            {"path": f"media/{nid(0)}.mp4", "bytes": 9,
             "sha256": digest(b"not-the-same")}])
        outcome = fx.apply(fx.find(FakeEngine()), FakeEngine())
        assert outcome["skipped"][0]["reason"] == "media_hash_changed"
        assert fx.media_file.exists()

    def test_check3_symlink_rejected(self, fx):
        """符号链接：不删（可能指向任意文件）。"""
        fx.media_file.unlink()
        outside = fx.home / "outside-target.bin"
        outside.write_bytes(b"outside")
        fx.media_file.symlink_to(outside)
        outcome = fx.apply(fx.find(FakeEngine()), FakeEngine())
        assert outcome["skipped"][0]["reason"] == "media_is_symlink"
        assert outside.exists()  # 目标文件毫发无损

    def test_check3_escape_rejected(self, tmp_path, monkeypatch):
        """清单路径逃出 media/（../）：不删。"""
        fx = Fixture(tmp_path, monkeypatch, manifest_override=[
            {"path": "../package.json", "bytes": 1, "sha256": "x" * 64}])
        outcome = fx.apply(fx.find(FakeEngine()), FakeEngine())
        assert outcome["skipped"][0]["reason"] == "media_outside_dir"
        assert fx.package_path.exists()

    def test_check3_absolute_path_rejected(self, tmp_path, monkeypatch):
        fx = Fixture(tmp_path, monkeypatch, manifest_override=[
            {"path": "/etc/hosts", "bytes": 1, "sha256": "x" * 64}])
        outcome = fx.apply(fx.find(FakeEngine()), FakeEngine())
        assert outcome["skipped"][0]["reason"] == "media_path_invalid"


class TestReceipts:
    def test_interrupted_started_receipt_skips(self, fx):
        """上次删到一半断电（started 回执）：保守跳过，等人工处理。"""
        work = config.work_dir(fx.note_id)
        receipt = work / "cleanup-receipt.json"
        receipt.write_text(json.dumps({"status": "started",
                                       "note_id": fx.note_id}),
                           encoding="utf-8")
        outcome = fx.apply(fx.find(FakeEngine()), FakeEngine())
        assert outcome["skipped"][0]["reason"] == \
            "cleanup_interrupted_requires_reconciliation"
        assert fx.media_file.exists()
        assert fx.ledger.get(fx.note_id)["cleanup_status"] is None

    def test_completed_receipt_reconciles_ledger(self, fx):
        """completed 回执（文件删完了、台账没来得及更新）：只补台账。"""
        work = config.work_dir(fx.note_id)
        receipt = work / "cleanup-receipt.json"
        receipt.write_text(json.dumps(
            {"status": "completed", "note_id": fx.note_id,
             "deleted_bytes": 123}), encoding="utf-8")
        outcome = fx.apply(fx.find(FakeEngine()), FakeEngine())
        assert outcome["notes"] == 1
        assert outcome["freed_bytes"] == 123
        row = fx.ledger.get(fx.note_id)
        assert row["cleanup_status"] == "cleaned"
        assert row["freed_bytes"] == 123

    def test_dry_run_deletes_nothing(self, fx):
        """find_candidates（--dry-run 的数据来源）只读不删。"""
        candidates = fx.find(FakeEngine())
        summary = cleanup.summarize_candidates(candidates)
        assert summary["total"]["notes"] == 1
        assert summary["total"]["bytes"] == len(fx.media_data)
        assert fx.media_file.exists()
        assert fx.ledger.get(fx.note_id)["cleanup_status"] is None

    def test_reimport_after_receipt_no_double_count(self, fx):
        """清理后再找候选：cleanup_status 已是 cleaned，不再出现。"""
        engine = FakeEngine()
        fx.apply(fx.find(engine), engine)
        assert fx.find(engine) == []
