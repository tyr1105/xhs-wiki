# -*- coding: utf-8 -*-
"""test_runner.py —— runner.py 的测试。

全部用假依赖：假扫描、假下载（真的往临时目录写文件，让清理环节有东西
可删）、假解析、假引擎、假模型提供方。不访问网络、不启动 Chrome、
不调用真实模型。

笔记编号、标题、签名链接全部由代码生成（虚构数据，不用任何真实数据）。
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from xhs_wiki import config, runner
from xhs_wiki.ledger import Ledger
from xhs_wiki.llm.base import LLMError
from xhs_wiki.store import BEIJING_TZ, ServiceStore

# 固定的"当前时间"（带北京时间），所有测试共享，结果可复现。
FIXED_NOW = datetime(2026, 3, 1, 10, 0, 0, tzinfo=BEIJING_TZ)


def nid(i: int) -> str:
    """生成虚构的 24 位十六进制笔记编号（参数不同编号不同）。"""
    return f"{i + 1:024x}"


def fake_items(n: int):
    """生成 n 条虚构的扫描结果（signed_url 是编造的假签名，仅供断言不泄漏）。"""
    return [
        {
            "note_id": nid(i),
            "title": f"虚构笔记{i}",
            "url": f"https://www.xiaohongshu.com/explore/{nid(i)}",
            "signed_url": f"https://www.xiaohongshu.com/explore/{nid(i)}"
            f"?xsec_token=fake{i}",
            "list_position": i,
        }
        for i in range(n)
    ]


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 假组件
# ---------------------------------------------------------------------------

class FakeProvider:
    """假的模型提供方：check() 默认通过，可配置失败。"""

    def __init__(self, ok=True, reason="ok"):
        self.ok, self.reason = ok, reason
        self.check_calls = 0

    def check(self):
        self.check_calls += 1
        if not self.ok:
            raise LLMError("not_logged_in", self.reason)
        return (True, "ok")


class FakeEngine:
    """假的引擎：health/ingest/distill/source_exists 全部可配置。"""

    name = "fake"

    def __init__(self, *, health_ok=True, deferred=(), ingest_failed=(),
                 distill_ok=True, source_ok=True, llm_error=None):
        self.health_ok = health_ok
        self.deferred = set(deferred)          # 这些笔记 ingest 返回 deferred
        self.ingest_failed = set(ingest_failed)
        self.distill_ok = distill_ok           # False 时 distill 全部失败
        self.source_ok = source_ok
        self.llm_error = llm_error             # ("ingest"/"distill", 错误代码)
        self.ingest_calls = []
        self.distill_calls = []

    def health(self):
        return (self.health_ok, "ok" if self.health_ok else "kb_not_writable")

    def ingest(self, packages):
        self.ingest_calls.append([p.note_id for p in packages])
        if self.llm_error and self.llm_error[0] == "ingest":
            raise LLMError(self.llm_error[1], "fake")
        results = []
        for pkg in packages:
            if pkg.note_id in self.deferred:
                results.append(SimpleNamespace(
                    note_id=pkg.note_id, status="deferred", error=None,
                    insufficient_reason="正文太短",
                    engine_ref={"source": f"{pkg.note_id}.md"}))
            elif pkg.note_id in self.ingest_failed:
                results.append(SimpleNamespace(
                    note_id=pkg.note_id, status="failed", error="card_invalid",
                    insufficient_reason=None, engine_ref={}))
            else:
                results.append(SimpleNamespace(
                    note_id=pkg.note_id, status="ok", error=None,
                    insufficient_reason=None,
                    engine_ref={"source": f"{pkg.note_id}.md"}))
        return results

    def distill(self, note_ids):
        self.distill_calls.append(list(note_ids))
        if self.llm_error and self.llm_error[0] == "distill":
            raise LLMError(self.llm_error[1], "fake")
        status = "ok" if self.distill_ok else "partial"
        per_note = {i: {"status": "ok" if self.distill_ok else "failed",
                        "code": None if self.distill_ok else "page_invalid"}
                    for i in note_ids}
        return SimpleNamespace(status=status, per_note=per_note, errors=[])

    def source_exists(self, note_id, engine_ref):
        return self.source_ok


class Rig:
    """一套可配置的假依赖 + 临时 APP_HOME（每个测试自己建一个）。"""

    def __init__(self, tmp_path, monkeypatch, *, scan_items=None,
                 scan_status="ok", scan_reason=None, nominal=None,
                 engine=None, provider=None, settings=None,
                 fail_download_for=(), download_hook=None):
        self.home = tmp_path / "home"
        monkeypatch.setenv("XHS_WIKI_HOME", str(self.home))
        self.store = ServiceStore(config.app_home())
        self.ledger = Ledger(config.ledger_path())
        self.notified = []

        items = fake_items(0) if scan_items is None else scan_items
        merged = dict(items=items, scan_status=scan_status,
                      scan_reason=scan_reason,
                      nominal=len(items) if nominal is None else nominal)
        self.scan_result = {
            "status": merged["scan_status"],
            "visible": len(merged["items"]),
            "nominal": merged["nominal"],
            "reason": merged["scan_reason"],
            "items": merged["items"],
        }
        self.engine = engine or FakeEngine()
        self.provider = provider or FakeProvider()
        self.download_calls = []
        self.fail_download_for = set(fail_download_for)
        self.download_hook = download_hook
        self.detected_profile = "https://www.xiaohongshu.com/user/profile/" + "a" * 24
        self.build_calls = []
        self.load_calls = []
        self.cleanup_skipped_seen = []

        # 基础设置：默认已完成设置、已登录（profile_url 已识别）、清理开启。
        base = {
            "setup_completed": True,
            "profile_url": "https://www.xiaohongshu.com/user/profile/" + "b" * 24,
            "cleanup_enabled": True,
            "first_import_limit": 100,
            "max_new_per_run": 100,
        }
        base.update(settings or {})
        self.store.update_settings(base)

        self.deps = runner.RunnerDeps(
            store=self.store,
            ledger=self.ledger,
            now=lambda: FIXED_NOW,
            sleep=lambda seconds: None,
            notify=self._notify,
            disk_free_bytes=lambda path: 500 * 1024 ** 3,
            scan_favorites=self._scan_favorites,
            detect_profile_url=self._detect_profile_url,
            download_note=self._download_note,
            build_package=self._build_package,
            load_package=self._load_package,
            make_engine=lambda settings: self.engine,
            make_provider=lambda settings: self.provider,
            key_exists=lambda name: True,
            cleanup_apply=self._cleanup_apply,
        )

    # ---- 假实现 ----
    def _notify(self, code):
        self.notified.append(code)
        return True

    def _scan_favorites(self, profile_url, *, headless):
        return dict(self.scan_result)

    def _detect_profile_url(self):
        return self.detected_profile

    def _download_note(self, note_id, signed_url, work_dir, *, vendor_dir=None):
        # 记录调用（只记编号，不记签名链接——测试本身也不留它）。
        self.download_calls.append(note_id)
        if self.download_hook:
            self.download_hook(note_id)
        if note_id in self.fail_download_for:
            return {"status": "failed", "code": "metadata_unavailable",
                    "media_kind": "text", "media": [], "meta": {}}
        # 真的写一个媒体文件，让 parse/cleanup 有真实文件可操作。
        work = Path(work_dir)
        media_dir = work / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        data = f"虚构媒体内容-{note_id}".encode("utf-8")
        target = media_dir / f"{note_id}.mp4"
        target.write_bytes(data)
        return {"status": "ok", "code": None, "media_kind": "video",
                "media": [{"path": f"media/{note_id}.mp4", "bytes": len(data),
                           "sha256": _digest_bytes(data)}],
                "meta": {"title": f"虚构笔记-{note_id[:4]}", "author": "虚构作者",
                         "desc": "", "tags": "", "published_at": "2026-01-01",
                         "type": "视频"}}

    def _build_package(self, note_id, work_dir, download_result):
        self.build_calls.append(note_id)
        work = Path(work_dir)
        # 解析包按下载清单生成原件清单（真实的解析会做转写/OCR，这里从简）。
        manifest = []
        for media in download_result.get("media", []):
            path = work / media["path"]
            manifest.append({"path": media["path"],
                             "bytes": path.stat().st_size,
                             "sha256": _digest_bytes(path.read_bytes())})
        meta = download_result.get("meta", {})
        package = {
            "note_id": note_id,
            "title": meta.get("title", ""),
            "url": f"https://www.xiaohongshu.com/explore/{note_id}",
            "author": meta.get("author", ""),
            "published_at": meta.get("published_at"),
            # package_md 按解析包的形状给一份虚构文本（ingest 从磁盘读它）。
            "package_md": f"# {meta.get('title', '')}\n\n虚构正文",
            "coverage": "text_only",
            "media": manifest,
        }
        package_path = work / "package.json"
        package_path.write_text(json.dumps(package, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        package_path.chmod(0o600)
        return {"package": None,
                "package_sha256": _digest_bytes(package_path.read_bytes())}

    def _load_package(self, work_dir):
        # 模拟 parse.package 的 load_package：读 package.json，返回同形状 dict 并附上
        # 文件当前的 sha256（文件被动过就和台账对不上）。
        self.load_calls.append(Path(work_dir).name)
        path = Path(work_dir) / "package.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["package_sha256"] = _digest_bytes(path.read_bytes())
        return data

    def _cleanup_apply(self, ledger, engine, store):
        from xhs_wiki import cleanup
        candidates = cleanup.find_candidates(ledger, engine)
        return cleanup.apply(candidates, ledger=ledger, engine=engine,
                             store=store, now=lambda: FIXED_NOW)

    # ---- 便捷方法 ----
    def run(self, trigger="manual"):
        return runner.run(trigger, deps=self.deps)

    def note(self, i):
        return self.ledger.get(nid(i))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """默认一套能跑通的假依赖：扫描到 3 条新收藏。"""
    return Rig(tmp_path, monkeypatch, scan_items=fake_items(3))


# ---------------------------------------------------------------------------
# 完整一轮
# ---------------------------------------------------------------------------

class TestFullRun:
    def test_full_pipeline(self, rig):
        """3 条新收藏走完全流程：下载→解析→入库→整理→清理，全部 distilled。"""
        record = rig.run()
        assert record["result"] == "completed", record
        assert record["new_found"] == 3
        assert record["processed"]["download"] == {"ok": 3, "failed": 0}
        assert record["processed"]["parse"]["ok"] == 3
        assert record["processed"]["ingest"]["ok"] == 3
        assert record["processed"]["distill"]["ok"] == 3
        for i in range(3):
            row = rig.note(i)
            assert row["stage"] == "distilled"
            assert row["cleanup_status"] == "cleaned"
            assert row["engine"] == "fake"
            # 原件确实删掉了。
            assert not (config.work_dir(nid(i)) / "media" / f"{nid(i)}.mp4").exists()
        assert record["cleanup"]["notes"] == 3
        assert record["cleanup"]["freed_bytes"] > 0
        # distill 一轮只调用一次（本批 3 条）。
        assert len(rig.engine.distill_calls) == 1
        assert len(rig.engine.distill_calls[0]) == 3

    def test_no_signed_url_leak(self, rig):
        """运行记录、状态文件、日志里都搜不到签名链接（红线）。"""
        rig.run()
        for name in ("runs.jsonl", "status.json", "scheduler.json"):
            text = (rig.home / name).read_text(encoding="utf-8")
            assert "xsec_token" not in text
            assert "fake0" not in text
        log = rig.home / "logs" / "run.log"
        if log.exists():
            assert "xsec_token" not in log.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 范围和上限
# ---------------------------------------------------------------------------

class TestLimits:
    def test_first_import_limit(self, tmp_path, monkeypatch):
        """首次导入范围：只处理收藏列表前 N 条，范围外的不算未完成。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(5),
                  settings={"first_import_limit": 2})
        record = rig.run()
        # 前 2 条处理完；本轮该做的（范围内）都做完了，判 completed。
        assert record["result"] == "completed", record
        assert set(rig.download_calls) == {nid(0), nid(1)}
        for i in range(2):
            assert rig.note(i)["stage"] == "distilled"
        for i in range(2, 5):
            assert rig.note(i)["stage"] == "skipped"
        assert record["counts"]["skipped"] == 3

    def test_max_new_per_run(self, tmp_path, monkeypatch):
        """每次运行的上限：download 环节最多处理 max_new_per_run 条。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(4),
                  settings={"max_new_per_run": 2})
        record = rig.run()
        assert len(rig.download_calls) == 2
        # 还有 2 条没下载（在首次导入范围内），判 incomplete 等追加运行。
        assert record["result"] == "incomplete"


# ---------------------------------------------------------------------------
# 失败和重试
# ---------------------------------------------------------------------------

class TestFailures:
    def test_download_fail_three_times_then_skip(self, tmp_path, monkeypatch):
        """下载失败 3 次后停在 failed:download，下一次运行不再处理它。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(1),
                  fail_download_for={nid(0)})
        for _ in range(2):
            record = rig.run()
            assert rig.note(0)["stage"] == "discovered"  # 失败 1、2 次还在重试
        record = rig.run()                               # 第 3 次失败后停终态
        assert rig.note(0)["stage"] == "failed:download"
        assert record["processed"]["download"]["failed"] >= 1
        calls_before = len(rig.download_calls)
        record = rig.run()
        # 第 4 次运行：不再下载这条笔记，也没有别的待处理，判 completed。
        assert len(rig.download_calls) == calls_before
        assert record["result"] == "completed", record
        assert rig.note(0)["stage"] == "failed:download"

    def test_deferred_note(self, tmp_path, monkeypatch):
        """内容不足的笔记进入 deferred（终态，不参与 distill）。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(2),
                  engine=FakeEngine(deferred={nid(0)}))
        record = rig.run()
        assert rig.note(0)["stage"] == "deferred"
        assert rig.note(0)["deferred_reason"] == "正文太短"
        assert rig.note(1)["stage"] == "distilled"
        assert record["processed"]["ingest"]["deferred"] == 1
        # deferred 的笔记也清理了原件（来源页已入库）。
        assert rig.note(0)["cleanup_status"] == "cleaned"


# ---------------------------------------------------------------------------
# 熔断
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    def test_quota_break_stops_model(self, tmp_path, monkeypatch):
        """quota_or_rate_limit：熔断后不再调模型，结果 incomplete，失败次数不增。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(2),
                  engine=FakeEngine(llm_error=("ingest", "quota_or_rate_limit")))
        record = rig.run()
        assert record["result"] == "incomplete"
        assert record["stop_reason"] == "llm_quota_or_rate_limit"
        assert record["llm_break"] == "quota_or_rate_limit"
        # ingest 抛错后 distill 不再调用。
        assert rig.engine.distill_calls == []
        # 笔记停在 parsed、attempts 不增加（熔断不计入笔记失败次数）。
        for i in range(2):
            row = rig.note(i)
            assert row["stage"] == "parsed"
            assert row["attempts"] == 0
            assert row["last_error"] is None

    def test_not_logged_in_break(self, tmp_path, monkeypatch):
        """not_logged_in：同样按 incomplete 熔断（稍后追加运行）。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(1),
                  engine=FakeEngine(llm_error=("distill", "not_logged_in")))
        record = rig.run()
        assert record["result"] == "incomplete"
        assert record["llm_break"] == "not_logged_in"
        # distill 抛错：笔记还停在 ingested，attempts 不变。
        assert rig.note(0)["stage"] == "ingested"
        assert rig.note(0)["attempts"] == 0

    def test_auth_failed_needs_user(self, tmp_path, monkeypatch):
        """auth_failed：判 needs_user: llm_unavailable，并通知。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(1),
                  engine=FakeEngine(llm_error=("ingest", "auth_failed")))
        record = rig.run()
        assert record["result"] == "needs_user"
        assert record["stop_reason"] == "llm_unavailable"
        assert "llm_unavailable" in rig.notified
        status = rig.store.read_status()
        assert any(i["code"] == "llm_unavailable" for i in status["needs_user"])


# ---------------------------------------------------------------------------
# 扫描和主页识别
# ---------------------------------------------------------------------------

class TestScan:
    def test_detect_profile_url_written_back(self, tmp_path, monkeypatch):
        """profile_url 为空时自动识别并写回设置。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(1),
                  settings={"profile_url": None})
        # update_settings 里 profile_url=None 合法（默认就是 null）。
        record = rig.run()
        assert record["result"] == "completed"
        assert rig.store.load_settings()["profile_url"] == rig.detected_profile

    def test_detect_fails_reports_xhs_login(self, tmp_path, monkeypatch):
        """识别不到主页（未登录）：报 xhs_login 并通知。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(1),
                  settings={"profile_url": None})
        rig.detected_profile = None
        record = rig.run()
        assert record["result"] == "needs_user"
        assert record["stop_reason"] == "xhs_login"
        assert "xhs_login" in rig.notified
        assert rig.download_calls == []

    def test_scan_login_required(self, tmp_path, monkeypatch):
        """扫描返回 login_required：报 xhs_login。"""
        rig = Rig(tmp_path, monkeypatch, scan_status="login_required",
                  scan_items=[])
        record = rig.run()
        assert record["result"] == "needs_user"
        assert record["stop_reason"] == "xhs_login"

    def test_scan_blocked(self, tmp_path, monkeypatch):
        """扫描被拦截：报 xhs_blocked，detail 带 reason。"""
        rig = Rig(tmp_path, monkeypatch, scan_status="blocked",
                  scan_items=[], scan_reason="blocked:captcha:300012")
        record = rig.run()
        assert record["result"] == "needs_user"
        assert record["stop_reason"] == "xhs_blocked"

    def test_scan_incomplete_still_processes(self, tmp_path, monkeypatch):
        """扫描不完整：照常处理扫到的部分，跳过每周重试，结果 incomplete。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(2),
                  scan_reason="scan_incomplete", nominal=10)
        # 先制造一条可重试的 failed:download 笔记。
        rig.ledger.upsert_discovered(fake_items(1))
        rig.ledger.fail(nid(0), "discovered", "metadata_unavailable")
        for _ in range(2):
            rig.ledger.fail(nid(0), "discovered", "metadata_unavailable")
        record = rig.run()
        assert record["scan"]["incomplete"] is True
        # 扫到的 2 条照常处理到终态。
        assert rig.note(1)["stage"] == "distilled"  # nid(1) 是本轮新增里的一条
        assert record["retry"]["ran"] is False       # 不完整时跳过每周重试
        assert record["result"] == "incomplete"
        assert record["stop_reason"] == "scan_incomplete"

    def test_weekly_retry_when_due(self, tmp_path, monkeypatch):
        """距上次满 download_retry_every_days 天且扫描完整：重置可重试的笔记。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(1))
        # 制造一条 failed:download（metadata_unavailable）。
        rig.ledger.upsert_discovered(fake_items(1))
        for _ in range(3):
            rig.ledger.fail(nid(0), "discovered", "metadata_unavailable")
        assert rig.note(0)["stage"] == "failed:download"
        # 上次重试时间是 8 天前（超过默认 7 天）。
        rig.store.write_scheduler(
            last_download_retry_at=(FIXED_NOW - timedelta(days=8)).isoformat())
        record = rig.run()
        assert record["retry"] == {"ran": True, "retried": 1}
        # 重置回 discovered 后本轮接着处理，最终 distilled。
        assert rig.note(0)["stage"] == "distilled"


# ---------------------------------------------------------------------------
# 预检
# ---------------------------------------------------------------------------

class TestPreflight:
    def test_setup_incomplete(self, tmp_path, monkeypatch):
        rig = Rig(tmp_path, monkeypatch, settings={"setup_completed": False})
        record = rig.run()
        assert record["result"] == "needs_user"
        assert record["stop_reason"] == "setup_incomplete"
        assert "setup_incomplete" in rig.notified
        assert record["message"]

    def test_disk_low(self, tmp_path, monkeypatch):
        rig = Rig(tmp_path, monkeypatch)
        rig.deps.disk_free_bytes = lambda path: 5 * 1024 ** 3  # 5 GiB < 10 GiB
        record = rig.run()
        assert record["result"] == "needs_user"
        assert record["stop_reason"] == "disk_low"

    def test_llm_check_fails(self, tmp_path, monkeypatch):
        """claude_code 的 check() 未登录：报 llm_unavailable，不开跑。"""
        rig = Rig(tmp_path, monkeypatch, provider=FakeProvider(ok=False))
        record = rig.run()
        assert record["result"] == "needs_user"
        assert record["stop_reason"] == "llm_unavailable"
        assert rig.download_calls == []

    def test_api_key_missing(self, tmp_path, monkeypatch):
        """anthropic 只查钥匙串（不发请求），没有 Key 报 llm_unavailable。"""
        # 模型名用 ASCII 虚构名：模型名只允许 [A-Za-z0-9._:/@-]。
        rig = Rig(tmp_path, monkeypatch,
                  settings={"llm": {"provider": "anthropic",
                                    "model": "fake-model", "base_url": None}})
        rig.deps.key_exists = lambda name: False
        record = rig.run()
        assert record["result"] == "needs_user"
        assert record["stop_reason"] == "llm_unavailable"

    def test_api_key_present_passes(self, tmp_path, monkeypatch):
        """有 Key 就过预检（provider.check 完全不调用——不发请求）。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(1),
                  settings={"llm": {"provider": "openai_compatible",
                                    "model": "fake-model",
                                    "base_url": "https://example.invalid"}})
        rig.deps.key_exists = lambda name: True
        record = rig.run()
        assert record["result"] == "completed"
        assert rig.provider.check_calls == 0

    def test_engine_unavailable(self, tmp_path, monkeypatch):
        rig = Rig(tmp_path, monkeypatch, engine=FakeEngine(health_ok=False))
        record = rig.run()
        assert record["result"] == "needs_user"
        assert record["stop_reason"] == "engine_unavailable"


# ---------------------------------------------------------------------------
# 暂停与单实例
# ---------------------------------------------------------------------------

class TestPauseAndLock:
    def test_pause_between_stages(self, tmp_path, monkeypatch):
        """运行开始前就有 PAUSE：在第一个安全点停下，结果 paused。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(1))
        rig.store.request_pause()
        record = rig.run()
        assert record["result"] == "paused"
        assert rig.download_calls == []
        assert rig.store.read_status()["state"] == "paused"

    def test_pause_between_notes(self, tmp_path, monkeypatch):
        """处理期间请求暂停：第一条笔记做完就停在安全点。"""
        def hook(note_id):
            # 下载第一条笔记后请求暂停（模拟用户此刻点了暂停）。
            rig.store.request_pause()
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(2),
                  download_hook=hook)
        record = rig.run()
        assert record["result"] == "paused"
        assert len(rig.download_calls) == 1
        # 第一条已推进，第二条保持 discovered；没做清理。
        assert rig.note(0)["stage"] == "distilled" or \
            rig.note(0)["stage"] == "downloaded"
        assert rig.note(1)["stage"] == "discovered"
        assert record["cleanup"]["ran"] is False

    def test_second_run_skipped(self, tmp_path, monkeypatch):
        """单实例锁：已有运行在进行时第二次开跑记 skipped。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(1))
        import fcntl
        # 手动先拿住 runner.lock，模拟另一个运行正在进行。
        lock_path = rig.home / "runner.lock"
        lock_path.touch()
        fd = lock_path.open("r+")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            record = rig.run()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
        assert record["result"] == "skipped"
        assert record["stop_reason"] == "already_running"
        assert rig.download_calls == []


# ---------------------------------------------------------------------------
# 杂项
# ---------------------------------------------------------------------------

class TestMisc:
    def test_missing_signed_url_fails_note(self, tmp_path, monkeypatch):
        """台账里没有签名链接：按下载失败记录，不传 None 给下载器。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(1))
        items = fake_items(1)
        # 只登记 notes 行、不写签名链接。
        rig.ledger.upsert_discovered([{**items[0], "signed_url": None}])
        rig.scan_result["items"] = [{**items[0], "signed_url": None}]
        record = rig.run()
        assert rig.download_calls == []
        assert record["processed"]["download"]["failed"] == 1
        assert rig.note(0)["last_error"] == "metadata_unavailable"

    def test_run_record_shape(self, rig):
        """运行记录的字段形状（counts / processed / cleanup）。"""
        record = rig.run()
        assert record["counts"]["distilled"] == 3
        assert set(record["processed"]) == {"download", "parse", "ingest",
                                            "distill"}
        assert record["ended_at"] is not None
        runs = rig.store.list_runs(5)
        assert len(runs) == 1 and runs[0]["run_id"] == record["run_id"]

    def test_cleanup_disabled(self, tmp_path, monkeypatch):
        """cleanup_enabled 为假：不清理，原件保留。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(1),
                  settings={"cleanup_enabled": False})
        record = rig.run()
        assert record["cleanup"]["ran"] is False
        assert rig.note(0)["stage"] == "distilled"
        assert (config.work_dir(nid(0)) / "media" / f"{nid(0)}.mp4").exists()

    def test_cleanup_skips_when_source_missing(self, tmp_path, monkeypatch):
        """引擎说来源不在：跳过清理，原件保留。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(1),
                  engine=FakeEngine(source_ok=False))
        record = rig.run()
        assert record["result"] == "completed"
        assert record["cleanup"]["skipped"] == 1
        assert rig.note(0)["cleanup_status"] is None
        assert (config.work_dir(nid(0)) / "media" / f"{nid(0)}.mp4").exists()


# ---------------------------------------------------------------------------
# 中断后恢复（已解析的笔记下一次运行要能入库）
# ---------------------------------------------------------------------------

class TestResumeAfterInterrupt:
    def test_parsed_notes_ingested_next_run(self, tmp_path, monkeypatch):
        """parse 之后、ingest 之前熔断：下一轮从磁盘加载解析包完成入库。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(2),
                  engine=FakeEngine(llm_error=("ingest", "quota_or_rate_limit")))
        record = rig.run()
        assert record["result"] == "incomplete"
        assert record["llm_break"] == "quota_or_rate_limit"
        for i in range(2):
            assert rig.note(i)["stage"] == "parsed"   # 停在 parsed
        # 第二轮：换一个正常的引擎，笔记应从磁盘的解析包继续入库到终态。
        rig.engine = FakeEngine()
        rig.deps.make_engine = lambda settings: rig.engine
        assert rig.load_calls == [nid(0), nid(1)]     # 本轮真的从磁盘读了包
        record = rig.run()
        assert record["result"] == "completed", record
        for i in range(2):
            assert rig.note(i)["stage"] == "distilled"

    def test_ingested_notes_distilled_next_run(self, tmp_path, monkeypatch):
        """distill 之前熔断：停在 ingested 的笔记下一轮接着整理。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(2),
                  engine=FakeEngine(llm_error=("distill", "not_logged_in")))
        record = rig.run()
        assert record["result"] == "incomplete"
        for i in range(2):
            assert rig.note(i)["stage"] == "ingested"
        rig.engine = FakeEngine()
        rig.deps.make_engine = lambda settings: rig.engine
        record = rig.run()
        assert record["result"] == "completed", record
        for i in range(2):
            assert rig.note(i)["stage"] == "distilled"
        # 第二轮的 distill 处理的正是上一轮滞留的两条笔记。
        assert len(rig.engine.distill_calls) == 1
        assert set(rig.engine.distill_calls[0]) == {nid(0), nid(1)}

    def test_package_sha_mismatch_marks_failed(self, tmp_path, monkeypatch):
        """解析包在解析之后被改过：sha256 对不上，记 package_changed。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(1),
                  engine=FakeEngine(llm_error=("ingest", "quota_or_rate_limit")))
        rig.run()
        assert rig.note(0)["stage"] == "parsed"
        # 篡改磁盘上的解析包（内容变一个字节，sha256 就完全不同）。
        package_path = config.work_dir(nid(0)) / "package.json"
        data = json.loads(package_path.read_text(encoding="utf-8"))
        data["title"] = data["title"] + "（被改过）"
        package_path.write_text(json.dumps(data, ensure_ascii=False),
                                encoding="utf-8")
        record = rig.run()
        row = rig.note(0)
        assert row["last_error"] == "package_changed"
        assert record["processed"]["ingest"]["failed"] >= 1
        # 失败的笔记没有进引擎（distill 没有可处理的对象）。
        assert rig.engine.distill_calls == []

    def test_package_missing_marks_failed(self, tmp_path, monkeypatch):
        """解析包文件丢了：同样按 package_changed 记失败，不中断运行。"""
        rig = Rig(tmp_path, monkeypatch, scan_items=fake_items(1),
                  engine=FakeEngine(llm_error=("ingest", "quota_or_rate_limit")))
        rig.run()
        (config.work_dir(nid(0)) / "package.json").unlink()
        record = rig.run()
        assert rig.note(0)["last_error"] == "package_changed"
        assert record["result"] in ("completed", "incomplete")
