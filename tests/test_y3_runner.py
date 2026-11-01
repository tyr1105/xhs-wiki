"""运行流程回归测试：验证首次范围、逐条失败和中断恢复真正反映到台账。

复用既有的假扫描、下载和解析夹具，只在临时目录写入代码生成的数据；
模型始终是假替身，不使用浏览器、网络、钥匙串或真实用户目录。
"""

from types import SimpleNamespace

import pytest

from xhs_wiki import runner
from xhs_wiki.engines.base import DistillResult
from xhs_wiki.engines.markdown import MarkdownEngine
from xhs_wiki.ledger import Ledger
from xhs_wiki.llm.base import LLMError

from test_markdown_engine import (
    FakeProvider, assign_new, card_resp, page_from_prompt,
)
from test_runner import FIXED_NOW, Rig, fake_items, nid


@pytest.fixture
def make_rig(tmp_path, monkeypatch):
    """每例把三种运行目录都指向临时区，并在结束时关闭台账连接。"""
    monkeypatch.setenv("XHS_WIKI_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("XHS_WIKI_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("XHS_WIKI_SKILLS_ROOT", str(tmp_path / "skills"))
    rigs = []

    def create(**kwargs):
        """按测试需要构建隔离的运行依赖，保存句柄供统一释放。"""
        rig = Rig(tmp_path, monkeypatch, **kwargs)
        rigs.append(rig)
        return rig

    yield create
    for rig in rigs:
        rig.ledger.close()
        if isinstance(rig.engine, MarkdownEngine) and rig.engine._store is not None:
            rig.engine.store.conn.close()


def replace_scan(rig, items):
    """替换下一轮扫描快照，让两轮之间的位置变化来自可控的虚构列表。"""
    rig.scan_result.update(status="ok", items=items, visible=len(items),
                           nominal=len(items), reason=None)


def test_first_baseline_does_not_limit_25_new_notes(make_rig):
    """首次 20 条之外是 skipped；下一次新增 25 条全部处理，跨重启仍有效。"""
    rig = make_rig(scan_items=fake_items(30), settings={"first_import_limit": 20})
    first = rig.run()
    assert first["result"] == "completed"
    assert first["counts"] == {"distilled": 20, "skipped": 10}
    path = rig.ledger.path
    rig.ledger.close()
    rig.ledger = Ledger(path)
    rig.deps.ledger = rig.ledger

    all_items = fake_items(55)
    ordered = all_items[30:] + all_items[:30]
    replace_scan(rig, [{**item, "list_position": pos}
                       for pos, item in enumerate(ordered)])
    second = rig.run()
    assert second["result"] == "completed", second
    assert second["processed"]["download"]["ok"] == 25
    assert second["counts"] == {"distilled": 45, "skipped": 10}
    assert all(rig.note(i)["stage"] == "distilled" for i in range(30, 55))
    assert rig.ledger.next_batch("discovered", 100) == []


def test_switch_to_all_imports_skipped_notes(make_rig):
    """改选全部会恢复旧收藏；已完成的收藏不重复下载。"""
    rig = make_rig(scan_items=fake_items(5), settings={"first_import_limit": 2})
    rig.run()
    rig.store.update_settings({"first_import_limit": None})
    record = rig.run()
    assert record["result"] == "completed"
    assert record["processed"]["download"]["ok"] == 3
    assert record["counts"] == {"distilled": 5}
    assert len(rig.download_calls) == len(set(rig.download_calls)) == 5


def test_switch_to_all_restores_before_failed_scan(make_rig):
    """即使下一轮扫描要求登录，运行开始时也已恢复用户选择的全部范围。"""
    rig = make_rig(scan_items=fake_items(3), settings={"first_import_limit": 1})
    rig.run()
    rig.store.update_settings({"first_import_limit": None})
    rig.scan_result["status"] = "login_required"
    record = rig.run()
    assert record["result"] == "needs_user"
    assert rig.ledger.counts() == {"distilled": 1, "discovered": 2}


def test_failed_scan_does_not_consume_first_baseline(make_rig):
    """扫描被拦截不能抢先建立基线，否则真正首次导入会越过范围。"""
    rig = make_rig(scan_items=fake_items(4), scan_status="blocked",
                   settings={"first_import_limit": 2})
    assert rig.run()["result"] == "needs_user"
    replace_scan(rig, fake_items(4))
    assert rig.run()["counts"] == {"distilled": 2, "skipped": 2}


def test_scan_registration_and_baseline_are_atomic(tmp_path):
    """登记失败时连同基线一起回滚，下一次成功扫描仍只保留首次 N 条。"""
    with Ledger(tmp_path / "ledger.sqlite3") as ledger:
        with pytest.raises(ValueError, match="note_id_invalid"):
            ledger.upsert_discovered(fake_items(3) + [{"note_id": ""}],
                                     first_import_limit=1)
        assert ledger.counts() == {}
        ledger.upsert_discovered(fake_items(3), first_import_limit=1)
        assert ledger.counts() == {"discovered": 1, "skipped": 2}


def test_all_first_baseline_is_not_replaced_by_later_limit(tmp_path):
    """最初选全部同样完成基线，后改 N 条不会跳过已等待处理的记录。"""
    with Ledger(tmp_path / "ledger.sqlite3") as ledger:
        ledger.upsert_discovered(fake_items(3), first_import_limit=None)
        ledger.upsert_discovered(fake_items(5), first_import_limit=1)
        assert ledger.counts() == {"discovered": 5}


def raise_llm(code):
    """生成按需失败的模型替身，异常只含固定错误代码。"""
    def fail(prompt):
        """在模型调用边界抛错，覆盖引擎和 runner 的真实衔接。"""
        raise LLMError(code, "generated_failure")
    return fail


def test_partial_ingest_only_failed_note_uses_attempt(make_rig, tmp_path):
    """使用真实 MarkdownEngine，单条超时不能给同批成功项记失败。"""
    provider = FakeProvider([
        card_resp(0), raise_llm("timeout"), card_resp(2),
        assign_new([nid(0), nid(2)]), page_from_prompt,
    ])
    engine = MarkdownEngine(tmp_path / "kb", provider, concurrency=1)
    rig = make_rig(scan_items=fake_items(3), engine=engine,
                   settings={"cleanup_enabled": False})
    record = rig.run()
    assert record["result"] == "incomplete", record
    assert record["processed"]["ingest"] == {"ok": 2, "deferred": 0, "failed": 1}
    assert rig.note(1)["attempts"] == 1
    assert rig.note(1)["last_error"] == "timeout"
    assert rig.note(0)["stage"] == rig.note(2)["stage"] == "distilled"
    assert rig.note(0)["attempts"] == rig.note(2)["attempts"] == 0


def test_pause_during_ingest_keeps_results_and_stops_before_distill(make_rig):
    """入库期间请求暂停，整批结果仍记账，但不能开始下一次整理模型调用。"""
    rig = make_rig(scan_items=fake_items(3))
    ingest = rig.engine.ingest

    def ingest_then_pause(packages):
        """模型已完成本批调用后模拟用户点击暂停，检验下个环节的安全点。"""
        results = ingest(packages)
        rig.store.request_pause()
        return results

    rig.engine.ingest = ingest_then_pause
    record = rig.run()
    assert record["result"] == "paused", record
    assert record["processed"]["ingest"] == {"ok": 3, "deferred": 0, "failed": 0}
    assert rig.engine.distill_calls == []
    assert all(rig.note(i)["stage"] == "ingested" for i in range(3))


@pytest.mark.parametrize("code", ["quota_or_rate_limit", "auth_failed"])
def test_ingest_break_preserves_completed_ledger_results(make_rig, tmp_path, code):
    """熔断前成功项立即推进，熔断项及未执行项没有失败次数或无结果惩罚。"""
    provider = FakeProvider([card_resp(0), raise_llm(code)])
    engine = MarkdownEngine(tmp_path / "kb", provider, concurrency=1)
    rig = make_rig(scan_items=fake_items(3), engine=engine,
                   settings={"cleanup_enabled": False})
    record = rig.run()
    expected = "needs_user" if code == "auth_failed" else "incomplete"
    assert record["result"] == expected, record
    assert record["processed"]["ingest"] == {"ok": 1, "deferred": 0, "failed": 0}
    assert rig.note(0)["stage"] == "ingested"
    assert rig.note(1)["stage"] == rig.note(2)["stage"] == "parsed"
    assert all(rig.note(i)["attempts"] == 0 for i in range(3))
    assert len(provider.calls) == 2


def test_ingest_mixed_failure_success_break_and_unstarted(make_rig, tmp_path):
    """普通失败、成功、熔断和未执行同批出现时，只扣真正失败项的一次预算。"""
    provider = FakeProvider([
        raise_llm("timeout"), card_resp(1), raise_llm("quota_or_rate_limit"),
    ])
    engine = MarkdownEngine(tmp_path / "kb", provider, concurrency=1)
    rig = make_rig(scan_items=fake_items(4), engine=engine,
                   settings={"cleanup_enabled": False})
    record = rig.run()
    assert record["result"] == "incomplete", record
    assert record["processed"]["ingest"] == {"ok": 1, "deferred": 0, "failed": 1}
    assert [rig.note(i)["stage"] for i in range(4)] == [
        "parsed", "ingested", "parsed", "parsed",
    ]
    assert [rig.note(i)["attempts"] for i in range(4)] == [1, 0, 0, 0]
    assert len(provider.calls) == 3


def test_distill_interrupt_resumes_without_repeating_card_calls(make_rig, tmp_path):
    """归类已保存、主题重写熔断后，下一轮只补主题页并推进台账。"""
    provider = FakeProvider([
        card_resp(0), card_resp(1), assign_new([nid(0), nid(1)]),
        raise_llm("quota_or_rate_limit"),
    ])
    engine = MarkdownEngine(tmp_path / "kb", provider, concurrency=1)
    rig = make_rig(scan_items=fake_items(2), engine=engine,
                   settings={"cleanup_enabled": False})
    assert rig.run()["result"] == "incomplete"
    assert rig.note(0)["stage"] == rig.note(1)["stage"] == "ingested"
    provider.responses.append(page_from_prompt)
    record = rig.run()
    assert record["result"] == "completed", record
    assert record["processed"]["distill"] == {"ok": 2, "failed": 0}
    assert len(provider.calls) == 5


@pytest.mark.parametrize("result", [
    DistillResult(status="partial"),
    SimpleNamespace(status="partial", errors=[]),
    {"status": "partial", "per_note": {}, "errors": []},
])
def test_apply_distill_accepts_empty_result_fields(tmp_path, result):
    """空的逐条结果仍是有效字典，不能误走数据类的下标访问。"""
    with Ledger(tmp_path / "ledger.sqlite3") as ledger:
        ledger.upsert_discovered(fake_items(1))
        ledger.advance(nid(0), "ingested")
        record = runner._base_record("generated-run", "manual", FIXED_NOW)
        runner._apply_distill(ledger, record, [nid(0)], result)
        assert ledger.get(nid(0))["attempts"] == 1
        assert record["processed"]["distill"]["failed"] == 1
