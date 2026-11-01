"""轻量引擎回归测试：逐条失败、熔断保留、整理恢复和私有根目录。

所有收藏和主题由辅助函数生成，模型只用内存替身。目录全部落在 tmp_path，
不访问网络，不调用真实模型，也不接触用户主目录。
"""

from __future__ import annotations

import json
import re
import stat
import threading
import time

import pytest

from test_markdown_engine import (
    FIXED_TIME, FakeProvider, assign_new, card_resp, make_pkg, page_from_prompt,
    parse_note_ids,
)
from xhs_wiki.engines.markdown import MarkdownEngine
from xhs_wiki.engines.markdown.engine import IngestInterrupted
from xhs_wiki.llm.base import LLMError


@pytest.fixture(autouse=True)
def isolated_directories(tmp_path, monkeypatch):
    """每条测试独立指定三个运行目录，即使代码以后新增目录调用也不会越界。"""
    for name, suffix in (("XHS_WIKI_HOME", "app"), ("XHS_WIKI_LOG_DIR", "logs"),
                         ("XHS_WIKI_SKILLS_ROOT", "skills")):
        monkeypatch.setenv(name, str(tmp_path / suffix))


def _number(prompt: str) -> int:
    """从代码生成的标题取序号，使并发响应不依赖线程调度次序。"""
    return int(re.search(r"虚构标题(\d+)", prompt).group(1))


@pytest.mark.parametrize("code", ["timeout", "unavailable", "bad_output"])
@pytest.mark.parametrize("concurrency", [1, 3])
def test_ingest_partial_failure_preserves_success_without_repayment(tmp_path, code, concurrency):
    """一条普通失败只影响自己；其余卡片和来源已保存，再调用只补失败条目。"""
    calls = []

    class Provider:
        """仅第二条首次调用失败，其余请求都返回同序号的合规虚构卡片。"""
        def complete(self, *, prompt, **kwargs):
            number = _number(prompt)
            calls.append(number)
            if number == 2 and calls.count(2) == 1:
                raise LLMError(code)
            return card_resp(number)

    engine = MarkdownEngine(tmp_path / "kb", Provider(), clock=lambda: FIXED_TIME,
                            concurrency=concurrency)
    packages = [make_pkg(i) for i in range(1, 7)]
    first = engine.ingest(packages)
    assert [item.note_id for item in first] == [pkg.note_id for pkg in packages]
    assert [item.status for item in first] == ["ok", "failed", "ok", "ok", "ok", "ok"]
    assert first[1].error == code
    for item in first:
        assert engine.store.has_card(item.note_id) is (item.status == "ok")
        if item.status == "ok":
            assert engine.source_exists(item.note_id, item.engine_ref)
    second = engine.ingest(packages)
    assert all(item.status == "ok" for item in second)
    assert calls.count(2) == 2
    assert len(calls) == 7
    engine.store.close()


@pytest.mark.parametrize("code", ["not_logged_in", "quota_or_rate_limit", "auth_failed"])
def test_ingest_serial_breaker_keeps_saved_and_cached_results(tmp_path, code):
    """串行熔断携带此前成功/暂缓/普通失败，后续未开始的笔记不会消耗调用。"""
    packages = [make_pkg(i) for i in range(1, 7)]
    calls = []

    class Provider:
        """第二条暂缓、第三条普通失败、第四条熔断，后两条不应收到调用。"""
        def complete(self, *, prompt, **kwargs):
            number = _number(prompt)
            calls.append(number)
            if number == 3:
                raise LLMError("timeout")
            if number == 4:
                raise LLMError(code)
            if number == 2:
                return card_resp(number, sufficient=False, insufficient_reason="虚构内容不足",
                                 key_points=[], topic_hints=[])
            return card_resp(number)

    engine = MarkdownEngine(tmp_path / "kb", Provider(), clock=lambda: FIXED_TIME,
                            concurrency=1)
    engine.ingest(packages[:1])
    with pytest.raises(IngestInterrupted) as caught:
        engine.ingest(packages)
    error = caught.value
    assert error.code == code
    assert error.failed_note_id == packages[3].note_id
    assert [item.status for item in error.completed_results] == ["ok", "deferred", "failed"]
    assert error.completed_results[0].engine_ref["cached"] is True
    assert error.completed_results[2].error == "timeout"
    assert calls == [1, 2, 3, 4]
    assert engine.store.has_card(packages[0].note_id)
    assert engine.store.has_card(packages[1].note_id)
    assert not engine.store.has_card(packages[3].note_id)
    engine.store.close()


@pytest.mark.parametrize("code", ["not_logged_in", "quota_or_rate_limit", "auth_failed"])
def test_ingest_concurrent_breaker_keeps_running_success_and_stops_queue(tmp_path, code):
    """三路同时调用时，熔断后仍收取运行中的成功响应，不启动剩余七条。"""
    barrier = threading.Barrier(3)
    calls = []

    class Provider:
        """三条到齐后第一条熔断，另外两条稍后完成，模拟已在进行的请求。"""
        def complete(self, *, prompt, **kwargs):
            number = _number(prompt)
            calls.append(number)
            barrier.wait(timeout=5)
            if number == 1:
                raise LLMError(code)
            time.sleep(0.03)
            return card_resp(number)

    engine = MarkdownEngine(tmp_path / "kb", Provider(), clock=lambda: FIXED_TIME,
                            concurrency=3)
    packages = [make_pkg(i) for i in range(1, 11)]
    with pytest.raises(IngestInterrupted) as caught:
        engine.ingest(packages)
    error = caught.value
    assert error.code == code
    assert error.failed_note_id == packages[0].note_id
    assert sorted(calls) == [1, 2, 3]
    assert [item.note_id for item in error.completed_results] == [pkg.note_id for pkg in packages[1:3]]
    for item in error.completed_results:
        assert item.status == "ok"
        assert engine.source_exists(item.note_id, item.engine_ref)
    for pkg in [packages[0], *packages[3:]]:
        assert not engine.store.has_card(pkg.note_id)
    engine.store.close()


def _assign_separate_topics(prompt: str) -> str:
    """把每张虚构卡片放到独立主题，便于验证中途完成一页后被打断。"""
    return json.dumps({"assignments": [
        {"note_id": note_id, "new_topic": {
            "name": f"虚构主题{index}", "description": f"虚构描述{index}",
            "category": f"虚构类别{index}",
        }} for index, note_id in enumerate(parse_note_ids(prompt))
    ]}, ensure_ascii=False)


def test_distill_resume_reports_finished_and_pending_members(tmp_path):
    """第一页已完成、第二页熔断后重启，两个已有成员都得到明确 ok。"""
    packages = [make_pkg(1), make_pkg(2)]

    def interrupted(prompt):
        """只在第二个主题重写时模拟额度耗尽。"""
        raise LLMError("quota_or_rate_limit")

    first_provider = FakeProvider([card_resp(1), card_resp(2), _assign_separate_topics,
                                   page_from_prompt, interrupted])
    first = MarkdownEngine(tmp_path / "kb", first_provider, clock=lambda: FIXED_TIME,
                           concurrency=1)
    first.ingest(packages)
    with pytest.raises(LLMError):
        first.distill([pkg.note_id for pkg in packages])
    assert first.store.pending_page_ids() == [2]
    first.store.close()

    provider = FakeProvider([page_from_prompt])
    resumed = MarkdownEngine(tmp_path / "kb", provider, clock=lambda: FIXED_TIME)
    result = resumed.distill([pkg.note_id for pkg in packages])
    assert result.status == "ok"
    assert result.assigned == 0
    assert result.pages_rewritten == 1
    assert result.per_note == {pkg.note_id: {"status": "ok", "code": None} for pkg in packages}
    assert provider.count_marker("主题归类器") == 0
    assert len(provider.calls) == 1
    assert resumed.store.pending_page_ids() == []
    # 重复恢复（页面全已完成）不得重新调用模型，也不能返回空 per_note。
    again = resumed.distill([pkg.note_id for pkg in packages])
    assert again.per_note == result.per_note
    assert again.pages_rewritten == 0
    assert len(provider.calls) == 1
    resumed.store.close()


def test_distill_pending_member_validation_failure_stays_pending(tmp_path):
    """恢复时页面仍不合格必须记失败并保留断点，下一次确实补写才算成功。"""
    pkg = make_pkg(1)

    def interrupted(prompt):
        """归类已保存后模拟一次调用中断。

        超时这类非熔断错误只让这一个主题失败、不再中断整个
        distill（见 tests/test_fix_round2.py），这里改用熔断类错误模拟中断。
        """
        raise LLMError("quota_or_rate_limit")

    provider = FakeProvider([card_resp(1), lambda p: assign_new(parse_note_ids(p)), interrupted])
    engine = MarkdownEngine(tmp_path / "kb", provider, clock=lambda: FIXED_TIME)
    engine.ingest([pkg])
    with pytest.raises(LLMError):
        engine.distill([pkg.note_id])
    provider.responses = ["虚构不合规响应", "虚构不合规响应"]
    failed = engine.distill([pkg.note_id])
    assert failed.per_note[pkg.note_id] == {"status": "failed", "code": "topic_page_invalid"}
    assert engine.store.pending_page_ids() == [1]
    provider.responses = [page_from_prompt]
    recovered = engine.distill([pkg.note_id])
    assert recovered.per_note[pkg.note_id] == {"status": "ok", "code": None}
    assert engine.store.pending_page_ids() == []
    engine.store.close()


@pytest.mark.parametrize("entry", ["health", "ingest", "distill"])
def test_new_knowledge_root_is_private(tmp_path, entry):
    """无论健康检查、直接入库或直接整理，新根目录都以 0700 建立。"""
    engine = MarkdownEngine(tmp_path / "kb", FakeProvider([card_resp(1)]),
                            clock=lambda: FIXED_TIME)
    if entry == "health":
        assert engine.health() == (True, "ok")
    elif entry == "ingest":
        engine.ingest([make_pkg(1)])
    else:
        engine.distill([])
    assert stat.S_IMODE(engine.kb_root.stat().st_mode) == 0o700
    engine.store.close()


def test_existing_knowledge_root_permissions_unchanged(tmp_path):
    """用户已有目录可能用于共享；健康检查和写入都不得静默收紧其权限。"""
    root = tmp_path / "kb"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    engine = MarkdownEngine(root, FakeProvider([card_resp(1)]), clock=lambda: FIXED_TIME)
    assert engine.health() == (True, "ok")
    engine.ingest([make_pkg(1)])
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    engine.store.close()
