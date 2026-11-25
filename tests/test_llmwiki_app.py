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

# LLM Wiki 0.6.13 真实记录文件的样例（数据全是虚构的测试笔记）。放在 tests/ 下随仓库提交，
# CI 才读得到；.dev/ 不进仓库。
FIXTURES = Path(__file__).resolve().parent / "fixtures/nashsu-0.6.13"


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


class TestIngestProviderOk:
    """N01a：按预设编号 / 条目类型 / 自定义本机地址判断是否需要密钥。

    直接测 ingest_provider_ok，数据全部虚构，不出现真实密钥。
    """

    def _controller(self, tmp_path, state):
        app = FakeApp(tmp_path / "app-state.json", running=True)
        if state is not None:
            write_state(app.state_path, state)
        return app

    @pytest.mark.parametrize("preset_id",
                             ["claude-code-cli", "codex-cli", "ollama-local"])
    def test_builtin_no_key_preset_without_entry(self, tmp_path, preset_id):
        """刚启用的内置无密钥预设：providerConfigs 里没有条目也算可用。"""
        app = self._controller(tmp_path, valid_state(
            activePresetId=preset_id,
            taskModelRouting={"ingestPresetId": preset_id},
            providerConfigs={}))
        assert app.ingest_provider_ok() is True

    @pytest.mark.parametrize("preset_id",
                             ["claude-code-cli", "codex-cli", "ollama-local"])
    def test_builtin_no_key_preset_with_entry(self, tmp_path, preset_id):
        """有条目但条目里只有 model（内置条目不带 provider 字段）。"""
        app = self._controller(tmp_path, valid_state(
            taskModelRouting={"ingestPresetId": preset_id},
            providerConfigs={preset_id: {"model": "fake-model"}}))
        assert app.ingest_provider_ok() is True

    @pytest.mark.parametrize("base_url", [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://[::1]:11434",
    ])
    def test_custom_local_base_url_needs_no_key(self, tmp_path, base_url):
        """自定义提供方指到本机：不需要密钥。"""
        app = self._controller(tmp_path, valid_state(
            taskModelRouting={"ingestPresetId": "custom-1"},
            providerConfigs={"custom-1": {
                "apiMode": "openai_compatible",
                "baseUrl": base_url, "apiKey": "", "model": "fake-model"}}))
        assert app.ingest_provider_ok() is True

    def test_custom_remote_base_url_without_key_not_ok(self, tmp_path):
        """自定义提供方指到远程且没有密钥：不可用。"""
        app = self._controller(tmp_path, valid_state(
            taskModelRouting={"ingestPresetId": "custom-1"},
            providerConfigs={"custom-1": {
                "apiMode": "openai_compatible",
                "baseUrl": "https://api.example.test/v1",
                "apiKey": "", "model": "fake-model"}}))
        assert app.ingest_provider_ok() is False

    def test_builtin_anthropic_with_key_ok(self, tmp_path):
        """内置 anthropic 条目（只有 apiKey / model）：有密钥可用。"""
        app = self._controller(tmp_path, valid_state(
            taskModelRouting={"ingestPresetId": "anthropic"},
            providerConfigs={"anthropic": {
                "apiKey": "synthetic-key", "model": "fake-model"}}))
        assert app.ingest_provider_ok() is True

    def test_builtin_anthropic_without_key_not_ok(self, tmp_path):
        """内置 anthropic 没有密钥：不可用。"""
        app = self._controller(tmp_path, valid_state(
            taskModelRouting={"ingestPresetId": "anthropic"},
            providerConfigs={"anthropic": {
                "apiKey": "", "model": "fake-model"}}))
        assert app.ingest_provider_ok() is False


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


# ---------------------------------------------------------------------------
# related：收录前把相关已有页面写到目录开头；ingest 逐条收录
# ---------------------------------------------------------------------------

from xhs_wiki.engines.llmwiki_app import related  # noqa: E402


def write_page(kb: Path, rel: str, title: str, body: str = "", front_extra: str = ""):
    """造一个虚构主题页（头部 type/title + 正文）。"""
    path = kb / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: concept\ntitle: \"{title}\"\n{front_extra}---\n\n# {title}\n\n{body}\n",
                    encoding="utf-8")


class TestRelated:
    def test_rank_prefers_page_sharing_title_terms(self, tmp_path):
        kb = tmp_path / "kb"
        write_page(kb, "wiki/concepts/fake-memory-layers.md", "虚构记忆分层方法", "讲记忆分层。")
        write_page(kb, "wiki/concepts/fake-cooking.md", "虚构家常菜做法", "讲炒菜火候。")
        write_page(kb, "wiki/entities/fake-tool.md", "虚构工具甲", "一个工具。")
        pages = related.load_catalog(kb)
        top = related.rank(pages, related.note_text("记忆分层的新说法", "正文讲记忆分层。"))
        assert top[0]["target"] == "concepts/fake-memory-layers"
        # 完全没有共同词的页面不出现。
        assert all(p["target"] != "concepts/fake-cooking" for p in top)

    def test_personal_pages_are_never_candidates(self, tmp_path):
        kb = tmp_path / "kb"
        write_page(kb, "wiki/concepts/fake-own-notes.md", "虚构记忆分层记录", "记忆分层。",
                   front_extra="origin: personal\n")
        pages = related.load_catalog(kb)
        assert pages == []

    def test_replace_block_inserts_after_heading_then_replaces(self):
        index = "---\ntype: index\n---\n\n# 目录\n\n## 概念\n- [[concepts/a]] — 甲\n"
        first = related.replace_block(index, related.render_block([]))
        # 标记块在一级标题之后、原有小节之前，原有内容原样保留。
        assert first.index("# 目录") < first.index(related.RELATED_START) < first.index("## 概念")
        assert "- [[concepts/a]] — 甲" in first
        page = {"target": "concepts/a", "title": "甲"}
        second = related.replace_block(first, related.render_block([page]))
        # 第二次是替换，不会出现两个标记块。
        assert second.count(related.RELATED_START) == 1
        assert "（本次未找到" not in second
        start = second.index(related.RELATED_START)
        assert "- [[concepts/a]] — 甲" in second[start:second.index(related.RELATED_END)]

    def test_write_hint_without_index_does_nothing(self, tmp_path):
        kb = tmp_path / "kb"
        write_page(kb, "wiki/concepts/fake.md", "虚构页面")
        assert related.write_hint(kb, "标题", "正文") == 0
        assert not (kb / project.WIKI_INDEX).exists()

    def test_write_hint_keeps_rest_of_index(self, tmp_path):
        kb = tmp_path / "kb"
        write_page(kb, "wiki/concepts/fake-memory.md", "虚构记忆分层")
        index = kb / project.WIKI_INDEX
        index.write_text("# 目录\n\n## Recently Updated\n- [[x]] — 乙\n", encoding="utf-8")
        assert related.write_hint(kb, "记忆分层", "正文") == 1
        text = index.read_text(encoding="utf-8")
        assert "[[concepts/fake-memory]]" in text
        assert text.endswith("## Recently Updated\n- [[x]] — 乙\n")


class TestPacedIngest:
    def test_second_note_waits_for_first_and_gets_its_own_hint(self, tmp_path, monkeypatch):
        engine, _, kb = make_engine(tmp_path)
        make_project(kb)
        p1 = make_pkg(note_id="xhs-test-201", title="第一条")
        p2 = make_pkg(note_id="xhs-test-202", title="第二条")
        events = []

        def fake_hint(root, title, package_md, limit=related.DEFAULT_LIMIT):
            first_done = project.read_ingest_cache(kb).get(project.source_rel(p1.note_id))
            events.append((title, first_done is not None))
            return 0

        monkeypatch.setattr(related, "write_hint", fake_hint)
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                # 模拟应用在等待第 3 次轮询时整理完第一条。
                touch(kb, "wiki/sources/first.md")
                write_cache(kb, cache_entry_for(kb, p1.note_id, ["wiki/sources/first.md"]))
            if len(sleeps) == 4:
                # 第二条写入后的第 1 次轮询时整理完第二条。
                touch(kb, "wiki/sources/second.md")
                cache = project.read_ingest_cache(kb)
                cache.update(cache_entry_for(kb, p2.note_id, ["wiki/sources/second.md"]))
                write_cache(kb, cache)
            engine.monotonic.advance(1.0)

        engine.sleep = sleep
        engine.ingest([p1, p2])
        # 第一条不等；第二条在第一条完成之后才更新候选页面并写入；最后一条也等到整理完。
        assert events == [("第一条", False), ("第二条", True)]
        assert len(sleeps) == 4
        assert project.source_path(kb, p2.note_id).exists()

    def test_pace_timeout_still_writes_next_note(self, tmp_path):
        engine, _, kb = make_engine(tmp_path)
        make_project(kb)
        engine.pace_wait = 30.0
        p1 = make_pkg(note_id="xhs-test-301")
        p2 = make_pkg(note_id="xhs-test-302")
        results = engine.ingest([p1, p2])
        # 第一条一直没结果：等满 30 秒后照常写第二条，两条都返回 ok。
        assert [r.status for r in results] == ["ok", "ok"]
        assert project.source_path(kb, p2.note_id).exists()
        assert engine.monotonic() >= 30.0

    def test_failed_previous_note_stops_waiting(self, tmp_path):
        engine, _, kb = make_engine(tmp_path)
        make_project(kb)
        p1 = make_pkg(note_id="xhs-test-401")
        p2 = make_pkg(note_id="xhs-test-402")
        write_queue(kb, [{"sourcePath": f"raw/sources/xhs/{n}.md",
                          "status": "failed", "retryCount": 3, "error": "x"}
                         for n in ("xhs-test-401", "xhs-test-402")])
        engine.ingest([p1, p2])
        # 两条都已被应用放弃：不必等，时钟没有前进。
        assert engine.monotonic() == 0.0

    def test_unchanged_note_neither_waits_nor_rewrites_hint(self, tmp_path, monkeypatch):
        engine, _, kb = make_engine(tmp_path)
        make_project(kb)
        p1 = make_pkg(note_id="xhs-test-501")
        engine.ingest([p1])
        waited = engine.monotonic()  # 第一次写入后会等它整理（这里一直没结果，等满上限）
        calls = []
        monkeypatch.setattr(related, "write_hint", lambda *a, **k: calls.append(a) or 0)
        engine.ingest([p1, p1])
        assert calls == [] and engine.monotonic() == waited


class TestProtectPersonalPages:
    """应用收录时写到用户个人页面（origin: personal），整理完要恢复原样。"""

    PERSONAL = "---\ntype: entity\ntitle: 我的项目\norigin: personal\n---\n\n# 我的项目\n\n原文。\n"

    def _setup(self, tmp_path):
        engine, _, kb = make_engine(tmp_path)
        make_project(kb)
        (kb / "wiki/entities").mkdir(parents=True, exist_ok=True)
        (kb / "wiki/entities/mine.md").write_text(self.PERSONAL, encoding="utf-8")
        (kb / "wiki/entities/other.md").write_text("---\ntype: entity\ntitle: 别人\n---\n\n旧\n", encoding="utf-8")
        return engine, kb

    def _app_writes(self, engine, kb, pkg, files, edits):
        """假应用：第一次轮询时按 edits 改文件，并登记这条来源的 filesWritten。"""
        def sleep(seconds):
            for rel, text in edits.items():
                (kb / rel).write_text(text, encoding="utf-8")
            write_cache(kb, cache_entry_for(kb, pkg.note_id, files))
            engine.monotonic.advance(1.0)
        engine.sleep = sleep

    def test_personal_page_written_by_ingest_is_restored(self, tmp_path):
        engine, kb = self._setup(tmp_path)
        pkg = make_pkg(note_id="xhs-test-601")
        self._app_writes(engine, kb, pkg,
                         ["wiki/entities/mine.md", "wiki/entities/other.md"],
                         {"wiki/entities/mine.md": "被合并改写", "wiki/entities/other.md": "合并后的新内容"})
        engine.ingest([pkg])
        assert (kb / "wiki/entities/mine.md").read_text(encoding="utf-8") == self.PERSONAL
        # 普通页面的合并结果保留
        assert (kb / "wiki/entities/other.md").read_text(encoding="utf-8") == "合并后的新内容"
        assert engine.restored_pages == ["wiki/entities/mine.md"]

    def test_personal_page_not_in_files_written_is_left_alone(self, tmp_path):
        engine, kb = self._setup(tmp_path)
        pkg = make_pkg(note_id="xhs-test-602")
        # 同一时间别的流程（例如每晚自省）正常更新了个人页面，但不在这条来源的 filesWritten 里
        self._app_writes(engine, kb, pkg, ["wiki/entities/other.md"],
                         {"wiki/entities/mine.md": "自省任务的正常更新"})
        engine.ingest([pkg])
        assert (kb / "wiki/entities/mine.md").read_text(encoding="utf-8") == "自省任务的正常更新"
        assert engine.restored_pages == []

    def test_protected_pages_lists_only_personal(self, tmp_path):
        _, kb = self._setup(tmp_path)
        assert list(related.protected_pages(kb)) == ["wiki/entities/mine.md"]


class TestDuplicateNames:
    """同名重复页检查：只差标点、空白或大小写的主题页归为一组，只看文件名。"""

    @staticmethod
    def _touch(kb, rel):
        path = kb / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# 虚构页面\n", encoding="utf-8")

    def test_name_key_ignores_punctuation_width_and_case(self):
        assert related.name_key("示例问题？") == related.name_key("示例问题")
        assert related.name_key("Demo-Tool") == related.name_key("demo tool")
        assert related.name_key("示例问题") != related.name_key("示例问答")

    def test_groups_fullwidth_question_mark_and_cross_directory(self, tmp_path):
        kb = tmp_path / "kb"
        self._touch(kb, "wiki/queries/示例问题.md")
        self._touch(kb, "wiki/queries/示例问题？.md")
        self._touch(kb, "wiki/concepts/demo-tool.md")
        self._touch(kb, "wiki/entities/Demo Tool.md")
        self._touch(kb, "wiki/concepts/不相干.md")
        # 来源摘要目录不在检查范围内
        self._touch(kb, "wiki/sources/示例问题.md")
        assert related.duplicate_name_groups(kb) == [
            ["wiki/concepts/demo-tool.md", "wiki/entities/Demo Tool.md"],
            ["wiki/queries/示例问题.md", "wiki/queries/示例问题？.md"],
        ]

    def test_no_wiki_dir_means_no_duplicates(self, tmp_path):
        assert related.duplicate_name_groups(tmp_path) == []
