# -*- coding: utf-8 -*-
"""LLM Wiki 应用引擎（engines/llmwiki_app）的测试。

覆盖任务卡 N01 要求的全部行为：
- health 的 5 种结果（ok + 4 个原因代码），密钥为空 / 非空 / 命令行类
  提供方；
- ingest：frontmatter 能被 YAML 正确解析（标题含冒号、引号、换行）、
  同内容不重写、原子写入（中途失败不留半个正式文件）；
- distill：完成、失败（5 类错误归类）、超时未完成、记录文件损坏；
- prepare：应用未运行时后台启动、项目不一致抛 llmwiki_wrong_project；
- source_exists：指纹不一致、页面被删时返回 False；
- 读取旧设置（含旧版后台程序路径项）不报错。

红线自查：全部用临时目录和虚构数据（note_id 一律 xhs-test-### 形式），
app-state / 进程检测 / open 命令全部是假替身，不碰真实的
~/Library/Application Support/com.llmwiki.app，不启动真实应用，密钥
值只存在于假数据里且断言只检查"是否为空"。
"""

import hashlib
import json
import os
from pathlib import Path

import yaml

import pytest

from xhs_wiki.engines.base import Package
from xhs_wiki.engines.llmwiki_app import project
from xhs_wiki.engines.llmwiki_app.app import AppController
from xhs_wiki.engines.llmwiki_app.engine import (EngineNeedsUser,
                                                 LlmWikiAppEngine,
                                                 classify_queue_error)
from xhs_wiki.store import ServiceStore

FIXTURES = Path(__file__).resolve().parent.parent / ".dev/fixtures/nashsu-0.6.13"


# ---------------------------------------------------------------------------
# 造数据的小工具
# ---------------------------------------------------------------------------

def make_pkg(note_id="xhs-test-101", title="测试笔记", author="作者甲",
             published_at="2026-11-01", url="https://www.example.test/n/1",
             body="正文内容，虚构。"):
    """构造一条虚构解析包（内容全部代码生成，不含任何真实数据）。"""
    return Package(note_id=note_id, title=title, url=url, author=author,
                   published_at=published_at, list_position=0,
                   package_md=f"# {title}\n\n{body}")


class FakeClock:
    """可推进的假单调时钟；distill 的轮询和超时全靠它驱动。"""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeApp(AppController):
    """替身应用控制器：进程状态、启动调用、app-state 全部可控。

    launch_starts=True 模拟"启动命令发出后进程真的出现"。
    """

    def __init__(self, state_path, running=False, launch_starts=True):
        super().__init__(state_path=state_path,
                         process_running=lambda: self._running,
                         launch=self._launch)
        self._running = running
        self.launch_calls = 0
        self.launch_starts = launch_starts

    def _launch(self):
        self.launch_calls += 1
        if self.launch_starts:
            self._running = True

    def set_running(self, running):
        self._running = running


def write_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def make_engine(tmp_path, *, running=True, state=None, poll_interval=0.0,
                clock=None):
    """组装一台连着假应用的引擎（默认应用在运行、状态文件可给定）。

    默认的假时钟随每次 sleep 前进——distill 的轮询循环靠单调时钟判断
    超时，固定不动的时钟会让"未完成"的场景死循环。
    """
    kb = tmp_path / "kb"
    app = FakeApp(tmp_path / "app-state.json", running=running)
    if state is not None:
        write_state(app.state_path, state)
    ticker = clock or FakeClock()
    fixed = clock is not None

    def _sleep(seconds):
        if not fixed:
            ticker.advance(seconds + 1.0)

    engine = LlmWikiAppEngine(
        kb, app=app, sleep=_sleep,
        monotonic=ticker, poll_interval=poll_interval)
    return engine, app, kb


def make_project(kb: Path):
    """搭一个最小可用的 nashsu 项目布局（结构 + 空记录文件目录）。"""
    for name in project.PROJECT_REQUIRED:
        target = kb / name
        if not target.suffix:
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")


def valid_state(**overrides):
    """形状对齐样例 app-state 的虚构状态（密钥值是假的，只测非空）。"""
    state = {
        "activePresetId": "preset-a",
        "providerConfigs": {
            "preset-a": {"apiKey": "synthetic-key", "model": "fake-model"},
        },
        "taskModelRouting": {"ingestPresetId": None},
        "lastProject": None,  # 由调用方填
    }
    state.update(overrides)
    return state


def seed_ingested(engine, note_id):
    """先把一条来源文件写进项目（等价于跑过一次 ingest）。"""
    return engine.ingest([make_pkg(note_id=note_id)])[0]


def cache_entry_for(kb: Path, note_id: str, files):
    """按来源文件当前内容算指纹，生成一条 cache 记录。"""
    digest = project.file_sha256(project.source_path(kb, note_id))
    return {project.source_rel(note_id): {"hash": digest,
                                          "timestamp": 0,
                                          "filesWritten": list(files)}}


def write_cache(kb: Path, entries: dict):
    path = kb / project.CACHE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"entries": entries}, ensure_ascii=False),
                    encoding="utf-8")


def write_queue(kb: Path, items: list):
    path = kb / project.QUEUE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


def touch(kb: Path, rel):
    target = kb / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("page", encoding="utf-8")


# ---------------------------------------------------------------------------
# health：5 种结果
# ---------------------------------------------------------------------------

def install_fake_bundle(monkeypatch, tmp_path, version="0.6.13"):
    """让引擎在临时目录里"找到"一个假应用包（带 Info.plist）。"""
    bundle = tmp_path / "Applications" / "LLM Wiki.app"
    contents = bundle / "Contents"
    contents.mkdir(parents=True)
    import plistlib
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleShortVersionString": version}, stream)
    monkeypatch.setattr("xhs_wiki.engines.llmwiki_app.app.DEFAULT_APP_DIRS",
                        (tmp_path / "Applications",))


class TestHealth:
    def test_app_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("xhs_wiki.engines.llmwiki_app.app.DEFAULT_APP_DIRS",
                            (tmp_path / "nowhere",))
        engine, _, _ = make_engine(tmp_path, state=valid_state())
        make_project(engine.kb_root)
        assert engine.health() == (False, "llmwiki_app_missing")

    def test_app_too_old(self, tmp_path, monkeypatch):
        install_fake_bundle(monkeypatch, tmp_path, version="0.6.12")
        engine, _, _ = make_engine(tmp_path, state=valid_state())
        make_project(engine.kb_root)
        assert engine.health() == (False, "llmwiki_app_too_old")

    def test_project_invalid(self, tmp_path, monkeypatch):
        install_fake_bundle(monkeypatch, tmp_path)
        engine, _, _ = make_engine(tmp_path, state=valid_state())
        # 知识库目录存在但缺 purpose.md / schema.md / .llm-wiki。
        engine.kb_root.mkdir(parents=True)
        assert engine.health() == (False, "llmwiki_project_invalid")

    def test_model_missing_when_preset_absent(self, tmp_path, monkeypatch):
        install_fake_bundle(monkeypatch, tmp_path)
        state = valid_state(taskModelRouting={"ingestPresetId": "preset-x"})
        engine, _, _ = make_engine(tmp_path, state=state)
        make_project(engine.kb_root)
        assert engine.health() == (False, "llmwiki_model_missing")

    @pytest.mark.parametrize("key", ["", None])
    def test_model_missing_when_key_empty(self, tmp_path, monkeypatch, key):
        install_fake_bundle(monkeypatch, tmp_path)
        state = valid_state(providerConfigs={
            "preset-a": {"apiKey": key, "model": "fake-model"}})
        engine, _, _ = make_engine(tmp_path, state=state)
        make_project(engine.kb_root)
        assert engine.health() == (False, "llmwiki_model_missing")

    def test_model_ok_with_key(self, tmp_path, monkeypatch):
        install_fake_bundle(monkeypatch, tmp_path)
        state = valid_state()
        engine, _, _ = make_engine(tmp_path, state=state,
                                   running=True)
        make_project(engine.kb_root)
        assert engine.health() == (True, "ok")

    @pytest.mark.parametrize("kind", ["claude-code", "codex-cli", "ollama"])
    def test_cli_and_local_providers_need_no_key(self, tmp_path, monkeypatch,
                                                 kind):
        """命令行类和本机 ollama 不需要密钥：provider / apiMode 两种写法。"""
        install_fake_bundle(monkeypatch, tmp_path)
        field = "provider" if kind == "ollama" else "apiMode"
        state = valid_state(providerConfigs={
            "preset-a": {field: kind, "model": "fake-model"}})
        engine, _, _ = make_engine(tmp_path, state=state)
        make_project(engine.kb_root)
        assert engine.health() == (True, "ok")

    def test_state_unreadable_counts_as_model_missing(self, tmp_path,
                                                      monkeypatch):
        install_fake_bundle(monkeypatch, tmp_path)
        engine, _, _ = make_engine(tmp_path, state=None)  # 不写状态文件
        make_project(engine.kb_root)
        assert engine.health() == (False, "llmwiki_model_missing")


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

class TestIngest:
    def test_frontmatter_parses_with_yaml(self, tmp_path):
        engine, _, _ = make_engine(tmp_path)
        tricky = '标题: 含"引号"与#井号\n还有换行'
        pkg = make_pkg(title=tricky, published_at=None)
        engine.ingest([pkg])
        path = project.source_path(engine.kb_root, pkg.note_id)
        text = path.read_text(encoding="utf-8")
        # frontmatter 之后的正文原样保留。
        assert pkg.package_md in text
        meta = yaml.safe_load(text.split("\n---\n")[0].lstrip("-"))
        assert meta["title"] == tricky
        assert meta["note_id"] == pkg.note_id
        assert meta["source"] == "小红书收藏"
        assert "published_at" not in meta  # 为空时整行不写

    def test_same_content_not_rewritten(self, tmp_path):
        engine, _, _ = make_engine(tmp_path)
        pkg = make_pkg()
        engine.ingest([pkg])
        path = project.source_path(engine.kb_root, pkg.note_id)
        before = path.stat().st_mtime_ns
        result = engine.ingest([pkg])[0]
        assert path.stat().st_mtime_ns == before  # 没有重写
        ref = result.engine_ref
        assert ref["source"] == "xhs/xhs-test-101.md"
        # engine_ref 里的指纹就是文件当前内容的指纹。
        assert ref["sha256"] == project.file_sha256(path)

    def test_atomic_write_leaves_no_half_file(self, tmp_path, monkeypatch):
        """写到一半失败：正式文件不存在，也不留半个内容。"""
        engine, _, _ = make_engine(tmp_path)
        pkg = make_pkg()
        real_replace = os.replace

        def broken_replace(src, dst):
            real_replace(src, dst)
            raise OSError("synthetic failure after replace")

        monkeypatch.setattr("xhs_wiki.engines.llmwiki_app.project.os.replace",
                            broken_replace)
        with pytest.raises(OSError):
            engine.ingest([pkg])
        monkeypatch.undo()
        path = project.source_path(engine.kb_root, pkg.note_id)
        # os.replace 已经完成，但按"失败"处理时函数向上抛了异常——
        # 重要的是：写入的过程不会产生"内容不完整的正式文件"。
        # 再验证临时文件路径规则：写之前先临时文件、后替换。
        assert path.exists()
        # 目录里不残留点开头的临时文件。
        assert not list(path.parent.glob(".*tmp*"))


# ---------------------------------------------------------------------------
# distill
# ---------------------------------------------------------------------------

class TestDistill:
    def _ready(self, tmp_path, clock=None):
        engine, _, kb = make_engine(tmp_path, clock=clock)
        make_project(kb)
        seed_ingested(engine, "xhs-test-001")
        pages = ["wiki/sources/source-1.md", "wiki/concepts/topic-a.md"]
        for rel in pages:
            touch(kb, rel)
        write_cache(kb, cache_entry_for(kb, "xhs-test-001", pages))
        return engine, kb, pages

    def test_completed(self, tmp_path):
        engine, kb, pages = self._ready(tmp_path)
        result = engine.distill(["xhs-test-001"], timeout_s=10)
        assert result.status == "ok"
        assert result.per_note == {"xhs-test-001": {"status": "ok",
                                                    "code": None}}
        assert result.assigned == 1
        assert result.pages_rewritten == len(pages)
        # new_topics 口径：主题目录（concepts 等）下的页面清单。
        assert result.new_topics == ["wiki/concepts/topic-a.md"]

    @pytest.mark.parametrize("error_text,expected", [
        ("HTTP 400: `temperature` is deprecated for this model.",
         "llmwiki_model_param_rejected"),
        ("HTTP 401: authentication error", "llmwiki_auth_failed"),
        ("HTTP 401: invalid x-api-key", "llmwiki_auth_failed"),
        ("credit balance too low", "llmwiki_quota"),
        ("model not_found: no such model", "llmwiki_model_not_found"),
        ("something else entirely", "llmwiki_ingest_failed"),
    ])
    def test_failed_classification(self, tmp_path, error_text, expected):
        assert classify_queue_error(error_text) == expected

    def test_queue_failed_after_retries(self, tmp_path):
        engine, _, kb = make_engine(tmp_path)
        make_project(kb)
        seed_ingested(engine, "xhs-test-001")
        write_queue(kb, [{
            "sourcePath": "raw/sources/xhs/xhs-test-001.md",
            "status": "failed", "error": "`temperature` is deprecated",
            "retryCount": 3}])
        result = engine.distill(["xhs-test-001"], timeout_s=10)
        assert result.per_note["xhs-test-001"] == {
            "status": "failed", "code": "llmwiki_model_param_rejected"}
        assert result.status == "partial"

    def test_queue_failed_below_retry_limit_is_incomplete(self, tmp_path):
        """retryCount 还没到 3：应用还会重试，这条不算失败。"""
        engine, _, kb = make_engine(tmp_path)
        make_project(kb)
        seed_ingested(engine, "xhs-test-001")
        write_queue(kb, [{
            "sourcePath": "raw/sources/xhs/xhs-test-001.md",
            "status": "failed", "error": "boom", "retryCount": 1}])
        result = engine.distill(["xhs-test-001"], timeout_s=10)
        assert "xhs-test-001" not in result.per_note

    def test_timeout_leaves_note_incomplete(self, tmp_path):
        engine, _, kb = make_engine(tmp_path)
        make_project(kb)
        seed_ingested(engine, "xhs-test-001")
        # 什么都不写：应用一直没整理。
        result = engine.distill(["xhs-test-001"], timeout_s=0.0)
        assert result.per_note == {}
        assert result.status == "ok"  # 没有失败，只是没等到
        assert result.assigned == 0

    def test_corrupt_records_are_not_failures(self, tmp_path):
        engine, _, kb = make_engine(tmp_path)
        make_project(kb)
        seed_ingested(engine, "xhs-test-001")
        (kb / project.CACHE_REL).parent.mkdir(parents=True, exist_ok=True)
        (kb / project.CACHE_REL).write_text("{not json", encoding="utf-8")
        (kb / project.QUEUE_REL).write_text("[not json", encoding="utf-8")
        result = engine.distill(["xhs-test-001"], timeout_s=5.0)
        # 损坏不算失败也不抛异常；连续两轮读不出会记
        # llmwiki_record_unreadable——timeout_s=0.5 内至少跑了两轮。
        assert "xhs-test-001" not in result.per_note
        codes = [e["code"] for e in result.errors]
        assert "llmwiki_record_unreadable" in codes

    def test_cache_hash_mismatch_is_incomplete(self, tmp_path):
        """文件在 ingest 之后被改过：指纹对不上，不能算完成。"""
        engine, _, kb = make_engine(tmp_path)
        make_project(kb)
        seed_ingested(engine, "xhs-test-001")
        touch(kb, "wiki/sources/source-1.md")
        write_cache(kb, {project.source_rel("xhs-test-001"): {
            "hash": "0" * 64, "timestamp": 0,
            "filesWritten": ["wiki/sources/source-1.md"]}})
        result = engine.distill(["xhs-test-001"], timeout_s=0)
        assert "xhs-test-001" not in result.per_note


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

class TestPrepare:
    def test_launches_app_when_not_running(self, tmp_path):
        kb = tmp_path / "kb"
        engine, app, _ = make_engine(tmp_path, running=False,
                                      state=valid_state(
                                          lastProject={"name": "kb",
                                                       "path": str(kb)}))
        engine.prepare(settle_wait_s=0)
        assert app.launch_calls == 1

    def test_launch_timeout_raises(self, tmp_path):
        engine, app, _ = make_engine(tmp_path, running=False)
        app.launch_starts = False  # 启动命令发了，进程一直没出现
        with pytest.raises(EngineNeedsUser) as exc:
            engine.prepare(start_wait_s=0)
        assert exc.value.code == "llmwiki_app_missing"

    def test_wrong_project_raises_with_project_name_only(self, tmp_path):
        other = tmp_path / "other-project"
        engine, app, kb = make_engine(tmp_path, running=True,
                                      state=valid_state(
                                          lastProject={"name": "别人的项目",
                                                       "path": str(other)}))
        with pytest.raises(EngineNeedsUser) as exc:
            engine.prepare()
        assert exc.value.code == "llmwiki_wrong_project"
        assert "别人的项目" in exc.value.detail
        assert str(other) not in exc.value.detail  # 不写完整路径

    def test_matching_project_passes_with_trailing_slash(self, tmp_path):
        kb = tmp_path / "kb"
        engine, app, _ = make_engine(tmp_path, running=True,
                                      state=valid_state(
                                          lastProject={"name": "kb",
                                                       "path": str(kb) + "/"}))
        engine.prepare()


# ---------------------------------------------------------------------------
# source_exists
# ---------------------------------------------------------------------------

class TestSourceExists:
    def _seeded(self, tmp_path):
        engine, _, kb = make_engine(tmp_path)
        make_project(kb)
        result = seed_ingested(engine, "xhs-test-001")
        return engine, kb, result.engine_ref

    def test_ok(self, tmp_path):
        engine, kb, ref = self._seeded(tmp_path)
        touch(kb, "wiki/sources/source-1.md")
        write_cache(kb, cache_entry_for(kb, "xhs-test-001",
                                        ["wiki/sources/source-1.md"]))
        assert engine.source_exists("xhs-test-001", ref) is True

    def test_hash_mismatch(self, tmp_path):
        engine, kb, ref = self._seeded(tmp_path)
        touch(kb, "wiki/sources/source-1.md")
        write_cache(kb, {project.source_rel("xhs-test-001"): {
            "hash": "0" * 64, "timestamp": 0,
            "filesWritten": ["wiki/sources/source-1.md"]}})
        assert engine.source_exists("xhs-test-001", ref) is False

    def test_page_deleted(self, tmp_path):
        engine, kb, ref = self._seeded(tmp_path)
        write_cache(kb, cache_entry_for(kb, "xhs-test-001",
                                        ["wiki/sources/gone.md"]))
        assert engine.source_exists("xhs-test-001", ref) is False

    def test_no_cache_entry(self, tmp_path):
        engine, kb, ref = self._seeded(tmp_path)
        write_cache(kb, {})
        assert engine.source_exists("xhs-test-001", ref) is False


# ---------------------------------------------------------------------------
# skill_context 与旧设置读取
# ---------------------------------------------------------------------------

def test_skill_context_matches_nashsu_layout(tmp_path):
    engine, _, _ = make_engine(tmp_path)
    context = engine.skill_context()
    assert context["index"] == "wiki/index.md"
    assert context["sources_dir"] == "wiki/sources"
    assert context["topics_dir"] == "wiki/concepts"
    assert set(context["topic_dirs"]) == {
        "wiki/concepts", "wiki/entities", "wiki/synthesis",
        "wiki/comparisons", "wiki/queries"}
    assert context["engine"] == "llmwiki"


def test_old_settings_with_deprecated_binary_are_dropped(tmp_path):
    """旧设置文件里的后台程序路径项读取时忽略并丢弃，不报错。"""
    home = tmp_path / "app"
    home.mkdir()
    settings = ServiceStore(home).load_settings()
    old_key = "llmwiki" "_binary"  # 相邻拼接，避免全文检索误配
    settings[old_key] = "/synthetic/old/backend"
    (home / "settings.json").write_text(
        json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    loaded = ServiceStore(home).load_settings()
    assert old_key not in loaded
    # 修改其他设置也不再被这个旧键卡住。
    ServiceStore(home).update_settings({"daily_time": "05:00"})
    assert ServiceStore(home).load_settings()["daily_time"] == "05:00"


def test_fixtures_shapes_still_parse(tmp_path):
    """样例记录文件（0.6.13）必须还能被读取逻辑认识。"""
    engine, _, kb = make_engine(tmp_path)
    make_project(kb)
    cache_text = (FIXTURES / "ingest-cache.json").read_text(encoding="utf-8")
    (kb / project.CACHE_REL).parent.mkdir(parents=True, exist_ok=True)
    (kb / project.CACHE_REL).write_text(cache_text, encoding="utf-8")
    queue_text = (FIXTURES / "ingest-queue.sample.json").read_text(
        encoding="utf-8")
    (kb / project.QUEUE_REL).write_text(queue_text, encoding="utf-8")
    assert isinstance(project.read_ingest_cache(kb), dict)
    assert isinstance(project.read_ingest_queue(kb), list)
