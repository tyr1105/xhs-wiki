# -*- coding: utf-8 -*-
"""tests/test_skill.py —— Claude Skill 渲染 / 安装 / 卸载的测试。

全部在临时目录完成，绝不碰真实的 ~/.claude。渲染断言里专门检查
"不出现具体领域词"（红线：Skill 不能暗示用户的收藏范围），并核对两种
引擎渲染出的 Skill 都指向各自真实存在的目录。
"""

import os
import re
import tempfile
import unittest
from pathlib import Path

from xhs_wiki import skill


def fake_settings(**overrides) -> dict:
    """最小的一份设置（形状与 store.load_settings 的相关键一致）。"""
    settings = {
        "kb_root": "~/FakeKB",
        "engine": "markdown",
        "setup_completed": True,
    }
    settings.update(overrides)
    return settings


def skill_paths(text: str, kb_root: Path) -> set:
    """取出 Skill"查询步骤"里写到的全部路径，换算成绝对路径。

    查询步骤里的路径都写在反引号里：带知识库位置的是绝对路径
    （例如 `<知识库>/索引.md`），只写目录名的是相对知识库根目录的写法
    （例如 `主题/`）。Path 会去掉末尾的斜杠，方便直接和目录比较。
    """
    steps = text.split("## 查询步骤", 1)[1]
    paths = set()
    for raw in re.findall(r"`([^`]+)`", steps):
        path = Path(raw)
        paths.add(path if path.is_absolute() else kb_root / path)
    return paths


class TestRender(unittest.TestCase):
    """render 的内容检查。"""

    def test_render_paths_and_engine(self):
        text = skill.render(fake_settings())
        self.assertIn("name: xhs-wiki", text)
        # 知识库路径被展开并写进正文（含查询步骤里的索引路径）。
        self.assertIn(str(Path("~/FakeKB").expanduser()), text)
        self.assertIn("索引.md", text)
        self.assertIn("轻量引擎", text)
        # 已完成设置：不出现"尚未完成设置"提示。
        self.assertNotIn("尚未完成首次设置", text)

    def test_render_setup_pending_notice(self):
        text = skill.render(fake_settings(setup_completed=False))
        self.assertIn("尚未完成首次设置", text)
        # 控制台要带访问令牌才能打开，提示里给 xhs-wiki open，不再给地址。
        self.assertIn("xhs-wiki open", text)
        self.assertNotIn("127.0.0.1", text)

    def test_render_llmwiki_engine_label(self):
        text = skill.render(fake_settings(engine="llmwiki"))
        self.assertIn("LLM Wiki", text)

    def test_render_has_no_domain_words(self):
        """description 和正文都不能列具体领域（docs/architecture.md §12）。"""
        text = skill.render(fake_settings())
        for word in ("露营", "餐厅", "美食", "健身", " Osaka", "大阪", "装修",
                     "旅行", "护肤", "育儿", "股票", "咖啡"):
            self.assertNotIn(word, text)
        # 触发条件写的是通用说法。
        self.assertIn("我收藏的", text)
        self.assertIn("查一下我的收藏", text)

    def test_render_has_generic_advice_trigger(self):
        """description 有一句通用触发条件，且不出现具体领域词。"""
        text = skill.render(fake_settings())
        self.assertIn("推荐、攻略、方法、工具、清单或经验类建议", text)
        self.assertIn("先查一下他是否收藏过相关内容", text)

    def test_render_has_source_keyword_search_step(self):
        """查询步骤包含"索引找不到时用关键词搜 来源/ 目录"。"""
        text = skill.render(fake_settings())
        kb_root = str(Path("~/FakeKB").expanduser())
        self.assertIn(f"{kb_root}/来源/", text)
        self.assertIn("Grep", text)
        self.assertIn("索引里找不到合适的主题", text)

    def test_render_describes_provenance(self):
        """description 必须说明来源是"用户本人的小红书收藏"。"""
        text = skill.render(fake_settings())
        self.assertIn("用户本人", text)
        self.assertIn("小红书收藏", text)

    def test_render_markdown_steps_unchanged(self):
        """轻量引擎的查询步骤和模板改用占位符之前一字不差。"""
        kb_root = str(Path("~/FakeKB").expanduser())
        text = skill.render(fake_settings())
        self.assertIn(
            f"1. 先读 `{kb_root}/索引.md`，了解有哪些主题；\n"
            "2. 选出与用户问题相关的主题，读对应的主题页（`主题/` 目录）；\n"
            f"3. 索引里找不到合适的主题时，用关键词在 `{kb_root}/来源/` 目录里搜索"
            "（例如用 Grep），命中后再读对应的来源页；\n"
            "4. 需要细节时，顺着主题页里的链接读来源页（`来源/` 目录）；\n",
            text)

    def test_render_leaves_no_placeholder(self):
        """两种引擎、设置完成与否，渲染结果里都不能残留 {{…}} 占位符。"""
        for engine in ("markdown", "llmwiki"):
            for completed in (True, False):
                text = skill.render(
                    fake_settings(engine=engine, setup_completed=completed))
                self.assertNotIn("{{", text, (engine, completed))
                self.assertNotIn("}}", text, (engine, completed))


class TestRenderFollowsEngineLayout(unittest.TestCase):
    """Skill 里的索引、主题页、来源页位置，必须是当前引擎真实建出来的目录。

    以前模板写死了轻量引擎的 索引.md、主题/、来源/：选 LLM Wiki 引擎的人
    在 Claude Code 里提问时，Skill 指向不存在的目录。这里两种引擎各搭一次
    真实的知识库骨架（不调模型、不联网），再逐个核对 Skill 里的路径都在。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.kb = Path(self.tmp.name) / "kb"

    def assert_paths_exist(self, text: str, expected: set) -> None:
        """Skill 写到的路径正好是 expected，而且每一个都真实存在。"""
        paths = skill_paths(text, self.kb)
        self.assertEqual(paths, expected)
        for path in paths:
            self.assertTrue(path.exists(), f"Skill 指向不存在的路径：{path}")

    def test_markdown_paths_exist(self):
        from xhs_wiki.engines.markdown.engine import MarkdownEngine

        engine = MarkdownEngine(self.kb, provider=None)
        # health 建出 主题/ 和 来源/；没有待整理的笔记时 distill 不调用
        # 模型，只生成 索引.md——这就是轻量引擎第一次运行后的骨架。
        self.assertEqual(engine.health(), (True, "ok"))
        self.addCleanup(engine.store.close)
        engine.distill([])
        text = skill.render(fake_settings(kb_root=str(self.kb)))
        self.assert_paths_exist(text, {
            self.kb / "索引.md",
            self.kb / "主题",
            self.kb / "来源",
        })

    def test_llmwiki_paths_exist_in_scaffold(self):
        from xhs_wiki.engines.llmwiki.project import scaffold

        # 向导第一次选 LLM Wiki 引擎时就是这样搭出知识库的（还没导入任何收藏）。
        scaffold(self.kb)
        text = skill.render(fake_settings(kb_root=str(self.kb), engine="llmwiki"))
        self.assert_paths_exist(text, {
            self.kb / "wiki" / "index.md",
            self.kb / "wiki",
            self.kb / "wiki" / "sources",
        })
        # 轻量引擎的目录名一个都不能出现在 LLM Wiki 用户的 Skill 里。
        for name in ("索引.md", "主题/", "来源/"):
            self.assertNotIn(name, text)


class TestInstallUninstall(unittest.TestCase):
    """install / uninstall 都在临时目录完成。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "skills"

    def test_install_writes_atomic_file_with_permissions(self):
        target = skill.install(fake_settings(), skills_root=self.root)
        self.assertEqual(target, self.root / "xhs-wiki" / "SKILL.md")
        text = target.read_text(encoding="utf-8")
        self.assertIn(str(Path("~/FakeKB").expanduser()), text)
        # 私有权限：目录 0700、文件 0600。
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        # 没有临时文件残留。
        self.assertEqual([p.name for p in target.parent.iterdir()],
                         ["SKILL.md"])

    def test_install_is_idempotent_and_updatable(self):
        """重复安装（改设置后更新）不留旧临时文件，内容跟着更新。"""
        first = skill.install(fake_settings(), skills_root=self.root)
        second = skill.install(
            fake_settings(kb_root="~/OtherKB", engine="llmwiki"),
            skills_root=self.root)
        self.assertEqual(first, second)
        text = second.read_text(encoding="utf-8")
        self.assertIn("OtherKB", text)
        self.assertEqual([p.name for p in second.parent.iterdir()],
                         ["SKILL.md"])

    def test_uninstall_removes_only_our_skill(self):
        skill.install(fake_settings(), skills_root=self.root)
        other = self.root / "someone-else"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text("别人的技能", encoding="utf-8")
        self.assertTrue(skill.uninstall(skills_root=self.root))
        self.assertFalse((self.root / "xhs-wiki").exists())
        self.assertTrue((other / "SKILL.md").exists())
        # 再卸载一次：没有可删的，返回 False。
        self.assertFalse(skill.uninstall(skills_root=self.root))


class TestDaemonSync(unittest.TestCase):
    """daemon.ServiceServer.sync_skill 的行为（不启动 HTTP 服务）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _server_stub(self, skills_root):
        # sync_skill 只用到 self.skills_root；用 SimpleNamespace 代替
        # 真实 ServiceServer，避免绑定端口。
        import types
        from xhs_wiki import daemon
        return daemon.ServiceServer.sync_skill.__get__(
            types.SimpleNamespace(skills_root=skills_root))

    def test_sync_skill_writes_file(self):
        sync = self._server_stub(Path(self.tmp.name) / "skills")
        sync(fake_settings())
        target = Path(self.tmp.name) / "skills" / "xhs-wiki" / "SKILL.md"
        self.assertTrue(target.is_file())

    def test_sync_skill_swallows_errors(self):
        # skills_root 指到一个文件：install 必然失败，sync 不能抛出去
        # （Skill 更新失败不该让设置接口整体失败）。
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("我是一个文件", encoding="utf-8")
        sync = self._server_stub(blocker)
        sync(fake_settings())  # 不抛异常即通过。
        # skills_root=None（默认 ~/.claude/skills）只由真实安装流程使用，
        # 测试绝不触发对真实 ~/.claude 的写入，故不做对应用例。


if __name__ == "__main__":
    unittest.main()
