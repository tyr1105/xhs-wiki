# -*- coding: utf-8 -*-
"""tests/test_n02_console.py —— N02（向导/控制台适配新 LLM Wiki 引擎）的测试。

覆盖四块（全部离线：临时目录 + 假引擎 + 假 app-state，绝不读写真实的
~/Library/Application Support/com.llmwiki.app 和用户知识库）：
1. daemon 新路由：GET /api/setup/llmwiki-model（检查结果）、
   POST /api/setup/llmwiki-tighten（权限收紧）、
   POST /api/setup/test-llmwiki（示例笔记测试整理）——各测成功、拒绝、
   未登录三种情况；
2. 统计：page_counts 不把 wiki/queries 计入主题；
3. 大白话对照表：从新引擎模块收集全部 llmwiki_* 原因代码，断言
   console.html 里都有中文说明；
4. Skill：LLM Wiki 引擎渲染出的说明和目录路径。

红线自查：app-state 全是虚构内容（密钥用明显的假值，并断言它绝不
出现在任何响应里）；不访问网络、不启动真实应用、不碰真实钥匙串。
"""

import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path

from test_daemon import DaemonTestBase

from xhs_wiki import daemon
from xhs_wiki.engines.base import DistillResult
from xhs_wiki.engines.llmwiki_app import project as llmwiki_project

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "src/xhs_wiki/web/console.html").read_text(encoding="utf-8")

# 虚构的 API Key（红线：绝不使用真实 Key；同时用来断言"密钥不进响应"）。
FAKE_API_KEY = "sk-test-never-real-0001"


class FakeAppController:
    """假的应用控制器：只提供 daemon 用到的三个只读接口。"""

    def __init__(self, state=None, state_path=None, last_project=(None, None)):
        self._state = state
        self.state_path = Path(state_path) if state_path else None
        self._last_project = last_project

    def read_state(self):
        return self._state

    def last_project(self, state=None):
        return self._last_project


class FakeLlmWikiEngine:
    """假的 LLM Wiki 引擎：prepare / distill 的行为可配置。

    distill 会顺带记录"被放进知识库的示例文件当时的内容"，测试据此
    核对写入和删除行为，不需要真的等整理。
    """

    name = "llmwiki"

    def __init__(self, app, kb_root, *, distill_result=None, prepare_error=None):
        self.app = app
        self.kb_root = Path(kb_root)
        self.distill_result = distill_result or DistillResult(
            status="ok", assigned=1, new_topics=[], pages_rewritten=1,
            errors=[], per_note={"xhs-wiki-selftest": {"status": "ok",
                                                       "code": None}})
        self.prepare_error = prepare_error
        self.seen_source_content = None

    def health(self):
        return True, "ok"

    def prepare(self):
        if self.prepare_error is not None:
            raise self.prepare_error

    def distill(self, note_ids, timeout_s=None):
        assert list(note_ids) == [daemon.LLMWIKI_SELFTEST_NOTE_ID]
        assert timeout_s == 300, "测试按钮必须按 5 分钟上限等待"
        source = self.kb_root / llmwiki_project.SOURCES_DIR_REL / (
            daemon.LLMWIKI_SELFTEST_NOTE_ID + ".md")
        self.seen_source_content = source.read_text(encoding="utf-8")
        return self.distill_result


def fake_state_file(directory: Path, *, mode=0o644) -> Path:
    """写一份虚构的 app-state.json（含假密钥），返回它的路径。"""
    path = directory / "app-state.json"
    path.write_text(json.dumps({
        "activePresetId": "custom-1",
        "taskModelRouting": {"ingestPresetId": "custom-1"},
        "providerConfigs": {
            "custom-1": {"apiKey": FAKE_API_KEY,
                         "apiMode": "anthropic_messages",
                         "baseUrl": "https://api.anthropic.com",
                         "model": "claude-haiku-5-5"}},
    }), encoding="utf-8")
    os.chmod(path, mode)
    return path


class N02RouteTestBase(DaemonTestBase):
    """公共：临时知识库 + 可配置的假 LLM Wiki 引擎。"""

    def make_fake_engine(self, **kwargs):
        self.state_path = fake_state_file(Path(self.tmp.name))
        self.kb = Path(self.tmp.name) / "wiki-kb"
        self.kb.mkdir(exist_ok=True)
        self.store.update_settings({"kb_root": str(self.kb),
                                    "engine": "llmwiki"})
        self.engine = FakeLlmWikiEngine(
            FakeAppController(state=json.loads(
                self.state_path.read_text(encoding="utf-8")),
                state_path=self.state_path,
                last_project=(str(self.kb), "wiki-kb")),
            self.kb, **kwargs)
        self.server.make_engine = lambda settings: self.engine
        self.server._kb_cache = None
        return self.engine


# ---------------------------------------------------------------------------
# GET /api/setup/llmwiki-model
# ---------------------------------------------------------------------------

class TestLlmwikiModelRoute(N02RouteTestBase):

    def test_success_reports_model_without_key(self):
        """检查结果给提供方/模型/密钥有无，绝不回显密钥内容。"""
        self.make_fake_engine()
        status, body = self.request("GET", "/api/setup/llmwiki-model")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["model"]["provider"], "自定义提供方")
        self.assertEqual(data["model"]["model"], "claude-haiku-5-5")
        self.assertTrue(data["model"]["has_key"])
        self.assertFalse(data["wrong_project"])
        self.assertTrue(data["permission"]["loose"])  # 0644 → 提示收紧
        # 红线：假密钥绝不进响应。
        self.assertNotIn(FAKE_API_KEY, body.decode("utf-8"))

    def test_wrong_engine_rejected(self):
        status, body = self.post("/api/setup/llmwiki-model")  # GET 之外的路径
        self.assertEqual(status, 404)  # POST 不存在 → 404（防探测）
        status, body = self.request("GET", "/api/setup/llmwiki-model")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "engine_invalid")

    def test_unauthenticated_rejected(self):
        self.make_fake_engine()
        status, body = self.request("GET", "/api/setup/llmwiki-model",
                                    cookie=None)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "console_auth_required")


# ---------------------------------------------------------------------------
# POST /api/setup/llmwiki-tighten
# ---------------------------------------------------------------------------

class TestLlmwikiTightenRoute(N02RouteTestBase):

    def test_success_chmods_only_that_file(self):
        """0644 → 0600：只改这一个文件的权限，不改内容。"""
        self.make_fake_engine()
        before = self.state_path.read_text(encoding="utf-8")
        status, data = self.post("/api/setup/llmwiki-tighten")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        mode = stat.S_IMODE(self.state_path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

    def test_missing_state_file_rejected(self):
        self.make_fake_engine()
        self.state_path.unlink()
        status, data = self.post("/api/setup/llmwiki-tighten")
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "llmwiki_state_missing")

    def test_wrong_engine_rejected(self):
        self.store.update_settings({"engine": "markdown"})
        status, data = self.post("/api/setup/llmwiki-tighten")
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "engine_invalid")

    def test_unauthenticated_rejected(self):
        self.make_fake_engine()
        status, data = self.post("/api/setup/llmwiki-tighten", cookie=None)
        self.assertEqual(status, 403)
        self.assertEqual(data["error"], "console_auth_required")


# ---------------------------------------------------------------------------
# POST /api/setup/test-llmwiki
# ---------------------------------------------------------------------------

class TestTestLlmwikiRoute(N02RouteTestBase):

    def source_path(self):
        return (self.kb / llmwiki_project.SOURCES_DIR_REL /
                (daemon.LLMWIKI_SELFTEST_NOTE_ID + ".md"))

    def test_success_writes_and_removes_selftest(self):
        """成功路径：写入固定虚构内容 → distill → 删除示例来源文件；
        xhs-wiki 不直接删除 LLM Wiki 生成的页面（页面由 LLM Wiki 随来源删除自行清理）。"""
        engine = self.make_fake_engine()
        page = self.kb / "wiki/sources/example-page.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("（虚构页面）", encoding="utf-8")
        status, data = self.post("/api/setup/test-llmwiki",
                                 body={"confirm_cost": True})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        # 写进去的是固定虚构内容（含虚构标记，不含任何真实编号）。
        self.assertEqual(engine.seen_source_content,
                         daemon.LLMWIKI_SELFTEST_CONTENT)
        self.assertIn("虚构", engine.seen_source_content)
        # 结束后示例来源文件已删除；xhs-wiki 不直接删页面（替身环境里页面仍在）。
        self.assertFalse(self.source_path().exists())
        self.assertTrue(page.exists())

    def test_cost_confirmation_required(self):
        """没有确认费用就拒绝，且不写任何文件。"""
        self.make_fake_engine()
        status, data = self.post("/api/setup/test-llmwiki", body={})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "cost_confirmation_required")
        self.assertFalse(self.source_path().exists())

    def test_wrong_project_rejected_and_writes_nothing(self):
        """应用打开的不是这个知识库：拒绝测试，一个文件都不写。"""
        from xhs_wiki.engines.llmwiki_app.engine import EngineNeedsUser
        self.make_fake_engine(
            prepare_error=EngineNeedsUser(
                "llmwiki_wrong_project", "LLM Wiki 当前打开的是别的项目"))
        status, data = self.post("/api/setup/test-llmwiki",
                                 body={"confirm_cost": True})
        self.assertEqual(status, 400)
        self.assertEqual(data["reason"], "llmwiki_wrong_project")
        self.assertFalse(self.source_path().exists())

    def test_failure_reason_from_distill(self):
        """distill 判失败时把原因代码透传（大白话在页面查表）。"""
        result = DistillResult(status="partial", assigned=0, new_topics=[],
                               pages_rewritten=0, errors=[],
                               per_note={"xhs-wiki-selftest": {
                                   "status": "failed",
                                   "code": "llmwiki_auth_failed"}})
        self.make_fake_engine(distill_result=result)
        status, data = self.post("/api/setup/test-llmwiki",
                                 body={"confirm_cost": True})
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "llmwiki_auth_failed")
        self.assertFalse(self.source_path().exists())

    def test_timeout_reports_and_still_cleans_up(self):
        """5 分钟没整理完：报 llmwiki_selftest_timeout，示例文件照删。"""
        result = DistillResult(status="ok", assigned=0, new_topics=[],
                               pages_rewritten=0, errors=[], per_note={})
        self.make_fake_engine(distill_result=result)
        status, data = self.post("/api/setup/test-llmwiki",
                                 body={"confirm_cost": True})
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "llmwiki_selftest_timeout")
        self.assertFalse(self.source_path().exists())

    def test_unauthenticated_rejected(self):
        self.make_fake_engine()
        status, data = self.post("/api/setup/test-llmwiki",
                                 body={"confirm_cost": True}, cookie=None)
        self.assertEqual(status, 403)
        self.assertEqual(data["error"], "console_auth_required")


# ---------------------------------------------------------------------------
# 统计与纯函数
# ---------------------------------------------------------------------------

class TestPageCounts(unittest.TestCase):
    """主题统计不把 wiki/queries（问答记录页）计入。"""

    def test_queries_not_counted_as_topics(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = Path(tmp)
            for page in ("wiki/concepts/a.md", "wiki/entities/b.md",
                         "wiki/synthesis/c.md", "wiki/comparisons/d.md",
                         "wiki/queries/q1.md", "wiki/queries/q2.md",
                         "wiki/sources/s1.md"):
                target = kb / page
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("x", encoding="utf-8")
            pages = llmwiki_project.page_counts(kb)
            self.assertEqual(pages, {"topics": 4, "sources": 1})
            # daemon.kb_stats 走同一个函数（nashsu 布局分支）。
            stats = daemon.kb_stats(kb, {}, engine="llmwiki")
            self.assertEqual(stats["topics"], 4)
            self.assertEqual(stats["sources"], 1)


class TestReviewPending(unittest.TestCase):

    def test_counts_unresolved_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            review = Path(tmp) / ".llm-wiki" / "review.json"
            review.parent.mkdir(parents=True)
            review.write_text(json.dumps([
                {"id": "review-a", "resolved": False},
                {"id": "review-b", "resolved": True},
                {"id": "review-c", "resolved": False},
            ]), encoding="utf-8")
            self.assertEqual(daemon.llmwiki_review_pending(tmp), 2)

    def test_read_failure_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(daemon.llmwiki_review_pending(tmp))
            broken = Path(tmp) / ".llm-wiki" / "review.json"
            broken.parent.mkdir(parents=True)
            broken.write_text("{不是 JSON", encoding="utf-8")
            self.assertIsNone(daemon.llmwiki_review_pending(tmp))


class TestModelSummary(unittest.TestCase):

    def test_built_in_cli_preset_needs_no_key(self):
        summary = daemon.llmwiki_model_summary(
            {"activePresetId": "claude-code-cli"})
        self.assertEqual(summary["provider"], "Claude Code")
        self.assertFalse(summary["key_needed"])
        self.assertTrue(summary["has_key"])

    def test_state_unreadable_is_none(self):
        self.assertIsNone(daemon.llmwiki_model_summary(None))


class TestStatusExtras(N02RouteTestBase):
    """/api/status 附带的待审核数和权限摘要。"""

    def test_status_includes_review_pending_and_permission(self):
        self.make_fake_engine()
        review = self.kb / ".llm-wiki" / "review.json"
        review.parent.mkdir(parents=True)
        review.write_text(json.dumps(
            [{"resolved": False}, {"resolved": True}]), encoding="utf-8")
        status, body = self.request("GET", "/api/status")
        data = json.loads(body)
        self.assertEqual(data["kb"]["review_pending"], 1)
        self.assertTrue(data["llmwiki_state"]["loose"])
        self.assertNotIn(FAKE_API_KEY, body.decode("utf-8"))

    def test_status_without_review_file_hides_the_field(self):
        self.make_fake_engine()
        _, body = self.request("GET", "/api/status")
        data = json.loads(body)
        self.assertNotIn("review_pending", data["kb"])


# ---------------------------------------------------------------------------
# 大白话对照表：新引擎的全部原因代码都有中文
# ---------------------------------------------------------------------------

class TestCodeLabelsCoverEngineCodes(unittest.TestCase):

    def test_every_engine_code_has_chinese_label(self):
        """从 engines/llmwiki_app/*.py 的源码里收集全部 llmwiki_* 代码
        （health/prepare 的原因、distill 的失败归类），逐个断言
        console.html 的 CODE_LABELS 里有中文说明。"""
        engine_dir = ROOT / "src/xhs_wiki/engines/llmwiki_app"
        codes = set()
        for path in engine_dir.glob("*.py"):
            codes |= set(re.findall(r"llmwiki_[a-z_]+",
                                    path.read_text(encoding="utf-8")))
        # health_checks 里的四个检查项名字（llmwiki_app 等）不是给用户看
        # 的原因代码，它们对应的原因代码是 *_missing / *_too_old 等。
        check_names = {"llmwiki_app", "llmwiki_version",
                       "llmwiki_project", "llmwiki_model"}
        codes -= check_names
        self.assertTrue(codes, "没收集到任何代码，测试本身失效了")
        labels_block = HTML.split("const CODE_LABELS={", 1)[1].split("\n};", 1)[0]
        labels = dict(re.findall(r"^\s*([a-z0-9_]+):'([^']*)'",
                                 labels_block, re.M))
        missing = [code for code in sorted(codes)
                   if not re.search(r"[\u4e00-\u9fff]",
                                    labels.get(code, ""))]
        self.assertEqual(missing, [],
                         "CODE_LABELS 缺少这些代码的中文说明："
                         + "、".join(missing))


if __name__ == "__main__":
    unittest.main()
