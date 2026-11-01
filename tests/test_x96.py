# -*- coding: utf-8 -*-
"""test_x96.py —— 控制台补缺（大白话对照表、隐藏样式、重新设置等）的测试。

覆盖五块：
1. 大白话对照表（console.html 的 CODE_LABELS）的完整性：用正则从各个
   源码文件里抽出会显示到页面上的代码字符串，断言每一个都在表里有
   中文说明。以后有人新加代码忘了写说明，这里会直接红；
2. .hidden 样式的回归测试：静态检查 CSS——`.hidden` 规则必须带
   `!important`，并且任何匹配"初始就带 hidden 的元素"的其它 display
   规则都不能带 `!important`（否则会盖住 .hidden，实际出现过
   的 base_url 框藏不住就是这类问题）；
3. POST /api/setup/restart：重新设置接口（正常重置 / 运行中 409）；
4. first_import_limit 允许 null（"全部收藏"真的是全部）；
5. 控制台切换语音模型后 AssetManager 被唤醒并开始准备新模型。

红线自查：全程只用临时目录（XHS_WIKI_HOME / XHS_WIKI_LOG_DIR / Skills
根目录都指到 tempfile），不访问网络、不下载任何东西、不启动真实子进程。
"""

import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path

from test_daemon import DaemonTestBase

from xhs_wiki.daemon import AssetManager, Supervisor
from xhs_wiki.store import ServiceStore, validate_settings

SRC = Path(__file__).resolve().parent.parent / "src" / "xhs_wiki"
HTML = (SRC / "web" / "console.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. CODE_LABELS 完整性：从源码抽出代码，断言表里有中文说明
# ---------------------------------------------------------------------------

def _labels():
    """从 console.html 里解析 CODE_LABELS 的全部键。"""
    match = re.search(r"const CODE_LABELS=\{(.*?)\n\};", HTML, re.S)
    assert match, "CODE_LABELS 不见了"
    return set(re.findall(r"^\s*([a-z0-9_]+):'", match.group(1), re.M))


# 每个来源文件配一组正则：把"会显示到页面上的代码"抽出来。
# 覆盖会把代码显示到页面上的各类来源。
EXTRACT_SOURCES = {
    # llm/*.py：check() 返回的原因 + LLMError 的 message（都是短代码）。
    "llm/base.py": [r'^\s{4}"([a-z_]+)",\s*#'],
    "llm/claude_code.py": [r'return \((?:True|False), "([a-z_]+)"\)',
                           r'LLMError\("[a-z_]+", f?"([a-z][a-z0-9_]*)'],
    "llm/codex.py": [r'return \((?:True|False), "([a-z_]+)"\)',
                     r'LLMError\("[a-z_]+", f?"([a-z][a-z0-9_]*)'],
    "llm/anthropic_api.py": [r'return \((?:True|False), "([a-z_]+)"\)',
                             r'LLMError\("[a-z_]+", f?"([a-z][a-z0-9_]*)',
                             r'raise ValueError\("([a-z_]+)"\)'],
    "llm/openai_compatible.py": [r'return \((?:True|False), "([a-z_]+)"\)',
                                 r'LLMError\("[a-z_]+", f?"([a-z][a-z0-9_]*)',
                                 r'raise ValueError\("([a-z_]+)"\)'],
    "llm/keys.py": [r'LLMError\("[a-z_]+", f?"([a-z][a-z0-9_]*)'],
    # daemon.py：setup 接口返回的 error / reason、运行记录的停止原因、
    # 需要用户处理的事项代码。
    "daemon.py": [r'"error": f?"([a-z][a-z0-9_]*)',
                  r'"stop_reason": "([a-z_]+)"',
                  r'raise ValueError\(f?"([a-z][a-z0-9_]*)',
                  r'needs_user\("([a-z_]+)"'],
    # store.py：设置校验抛的错误代码（经 /api/settings 的 error 上页面）。
    "store.py": [r'raise ValueError\(f?"([a-z][a-z0-9_]*)'],
    # runner.py：失败笔记的错误代码、needs_user 代码、停止原因兜底值。
    "runner.py": [r'ledger\.fail\([^)]*"([a-z][a-z0-9_]*)"\)',
                  r'or "([a-z_]+)"',
                  r'needs_user\("([a-z_]+)"',
                  r'raise ValueError\(f?"([a-z][a-z0-9_]*)'],
    # download.py：下载阶段的失败代码（_ERROR_CODES 集合 + fail() 调用）。
    "download.py": [r'^\s{4}"([a-z_]+)",\s*#', r'fail\("([a-z_]+)"'],
    # parse/package.py：解析阶段的失败代码（asr/ocr 的内部代码在 package
    # 里被 _ASR_ERROR_CODES 映射成这几个透传代码，页面只见映射结果）。
    "parse/package.py": [r'raise ValueError\("([a-z_:]+)"\)'],
    # assets.py：资源下载的错误代码（显示在顶部资源条）。
    "assets.py": [r'raise ValueError\("([a-z_]+)"\)'],
    # 引擎：入库/整理阶段的失败代码（写进台账，显示在失败笔记列表）。
    "engines/markdown/engine.py": [r'^[A-Z_]+ = "([a-z_]+)"',
                                   r'"code": "([a-z_]+)"'],
    "engines/llmwiki/engine.py": [r'raise \w+\("([a-z_]+)"\)',
                                  r'"code": "([a-z_]+)"'],
    "engines/llmwiki/compiler.py": [r'"code": "([a-z_]+)"'],
}

# store 的校验代码有一部分是 f"{field}_invalid" 动态拼出来的，正则抽不
# 到完整字面量，手工列出（新增此类字段时同样要在这里补一行）。
STORE_DYNAMIC_CODES = {
    "unknown_field", "field_not_editable", "daily_time_invalid",
    "max_new_per_run_invalid", "download_retry_every_days_invalid",
    "schedule_enabled_invalid", "headless_invalid", "cleanup_enabled_invalid",
    "setup_completed_invalid",
}

# 白名单：正则抽得到、但确认不会显示到页面上的代码（每条写明理由）。
WHITELIST = {
    # runner 的编程错误兜底（依赖没配齐），页面上只会看到 unexpected_error。
    "load_package_missing",
    # store 的内部编程错误（锁名/事项代码拼错），任何接口都不会透出。
    "unknown_lock_name", "unknown_needs_user_code",
    # runner 里正则误抓的属性名字符串（getattr(error, "code", ...)），不是错误代码。
    "code",
}


def _base_form(code: str) -> str:
    """把带动态尾巴的代码归约成查表的基础形式。

    - 尾部下划线（f"keychain_save_failed_{rc}" 抽到的字面前缀）→ 去掉；
    - 冒号后缀（"claude_model_error:xxx"）→ 只留冒号前。
    console.html 的 plainCode() 用同样的规则先归约再查表。
    """
    return code.rstrip("_").split(":")[0]


class TestCodeLabelsComplete(unittest.TestCase):
    """每个会显示到页面上的代码，CODE_LABELS 里都要有中文说明。"""

    def test_every_source_code_has_label(self):
        labels = _labels()
        missing = []
        for rel, patterns in EXTRACT_SOURCES.items():
            text = (SRC / rel).read_text(encoding="utf-8")
            found = set()
            for pattern in patterns:
                found |= set(re.findall(pattern, text, re.M))
            for code in sorted(found):
                if code in WHITELIST or _base_form(code) in WHITELIST:
                    continue
                if _base_form(code) not in labels:
                    missing.append(f"{rel}: {code}")
        for code in sorted(STORE_DYNAMIC_CODES):
            if code not in labels:
                missing.append(f"store.py(动态校验代码): {code}")
        self.assertEqual(missing, [],
                         "CODE_LABELS 缺少这些代码的中文说明：\n"
                         + "\n".join(missing))

    def test_labels_are_meaningful_chinese(self):
        """说明必须是大白话：不能是空串，也尽量给出能照着做的提示。"""
        labels = dict(re.findall(r"^\s*([a-z0-9_]+):'([^']*)'", HTML, re.M))
        # 抽查两个"能照着做"的说明。
        self.assertIn("在终端运行 claude", labels["claude_code_not_logged_in"])
        self.assertIn("codex login", labels["codex_not_logged_in"])
        for key in ("claude_code_not_logged_in", "assets_network_error"):
            self.assertTrue(labels.get(key, "").strip(), f"{key} 的说明是空的")


# ---------------------------------------------------------------------------
# 2. .hidden 的回归测试（静态检查 CSS）
# ---------------------------------------------------------------------------

def _css_rules(css: str):
    """解析出 [(选择器列表, 声明文本)]；先剥掉 CSS 注释。"""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(selector.strip(), body.strip())
            for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def _initial_hidden_elements():
    """找出初始 HTML 里就带 hidden class 的元素，返回 [(tag, class 集合)]。"""
    elements = []
    for tag, attrs in re.findall(r"<([a-z]+)([^>]*)>", HTML):
        classes = re.search(r'class="([^"]*)"', attrs)
        if classes and "hidden" in classes.group(1).split():
            elements.append((tag, set(classes.group(1).split()), attrs))
    return elements


def _selector_matches(selector: str, tag: str, classes: set) -> bool:
    """简化匹配：单类 / 多类 / 裸标签名三种选择器（够覆盖本页的 CSS）。"""
    selector = selector.split(":")[0].strip()  # 去掉 :hover 等伪类
    if not selector:
        return False
    if selector.startswith("."):
        return all(part in classes for part in selector.split(".")[1:])
    return selector == tag


class TestHiddenCss(unittest.TestCase):
    """`.hidden` 必须真的能藏住元素（base_url 框藏不住的回归测试）。"""

    def test_hidden_rule_has_important(self):
        """核心断言：.hidden 规则带 !important——label.field 等带 display
        的规则特异性更高，不加 !important 时加了 hidden 仍然显示。"""
        self.assertIn(".hidden{display:none!important}", HTML)

    def test_no_display_rule_can_beat_hidden(self):
        """任何匹配"初始 hidden 元素"的 display 规则都不能带 !important
        （否则可能盖过 .hidden）；并且这些元素不能有内联 display。"""
        css = re.search(r"<style>(.*?)</style>", HTML, re.S).group(1)
        hidden_elements = _initial_hidden_elements()
        self.assertTrue(hidden_elements, "页面里应至少有一个初始 hidden 元素")
        # 页面里必须真的存在 label.field + hidden 的组合（base_url 框），
        # 保证这条回归真的覆盖到当年的问题。
        self.assertTrue(any(
            tag == "label" and "field" in classes
            for tag, classes, _attrs in hidden_elements),
            "应存在初始就带 hidden 的 label.field 元素（base_url 输入框）")
        for selector, body in _css_rules(css):
            if "display" not in body:
                continue
            has_important = "!" in body
            for tag, classes, _attrs in hidden_elements:
                if not any(_selector_matches(part, tag, classes)
                           for part in selector.split(",")):
                    continue
                if has_important and "display:none" not in body.replace(" ", ""):
                    self.fail(
                        f"规则 `{selector}{{{body}}}` 匹配初始 hidden 元素 "
                        f"<{tag} class='{' '.join(sorted(classes))}'> 且带 "
                        f"!important 的 display，会盖住 .hidden")
        for tag, classes, attrs in hidden_elements:
            style = re.search(r'style="([^"]*)"', attrs)
            if style:
                self.assertNotRegex(
                    style.group(1), r"display",
                    f"<{tag}> 带内联 display 样式，classList 的 hidden 盖不住它")


# ---------------------------------------------------------------------------
# 3. POST /api/setup/restart
# ---------------------------------------------------------------------------

class TestSetupRestart(DaemonTestBase):

    def _complete_setup(self):
        """把设置置成"已完成首次设置"的样子（值本身是虚构路径）。"""
        self.store.update_settings({
            "setup_completed": True,
            "kb_root": str(Path(self.tmp.name) / "kb"),
        })

    def test_restart_resets_flag_keeps_settings(self):
        """重置后 /api/status 显示需要设置，原有设置全部保留。"""
        self._complete_setup()
        status, data = self.post("/api/setup/restart")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        status, body = self.request("GET", "/api/status")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertFalse(data["setup_completed"])
        # 原有设置还在（没有被清掉）。
        self.assertEqual(data["settings"]["kb_root"],
                         str(Path(self.tmp.name) / "kb"))
        self.assertEqual(data["settings"]["asr_model"], "whisper")

    def test_restart_rejected_while_running(self):
        """有运行在进行时返回 409 run_in_progress，且不改设置。"""
        self._complete_setup()
        # 塞一个"活着"的运行子进程句柄：is_running() 即为真。
        from test_daemon import FakeProcess
        self.supervisor._run_proc = FakeProcess()
        try:
            status, data = self.post("/api/setup/restart")
        finally:
            self.supervisor._run_proc = None
        self.assertEqual(status, 409)
        self.assertEqual(data["error"], "run_in_progress")
        self.assertTrue(self.store.load_settings()["setup_completed"])


# ---------------------------------------------------------------------------
# 4. first_import_limit 允许 null（"全部收藏"真的是全部）
# ---------------------------------------------------------------------------

class TestFirstImportAll(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["XHS_WIKI_HOME"] = str(Path(self.tmp.name) / "app")
        os.environ["XHS_WIKI_LOG_DIR"] = str(Path(self.tmp.name) / "logs")
        self.store = ServiceStore(Path(self.tmp.name) / "app")

    def test_validate_allows_null_and_range(self):
        settings = self.store.load_settings()
        # null = 全部收藏（runner._eligible 把空值当作不限）。
        merged = {**settings, "first_import_limit": None}
        self.assertEqual(validate_settings(merged), merged)
        # 整数仍然是 1–1000。
        self.assertEqual(
            validate_settings({**settings, "first_import_limit": 1000})[
                "first_import_limit"], 1000)
        for bad in (0, 1001, -1, True, "20", 20.5):
            with self.assertRaises(ValueError):
                validate_settings({**settings, "first_import_limit": bad})

    def test_runner_treats_null_as_unlimited(self):
        # 首次导入范围是一次性基线（不是每轮按位置过滤）；全部导入仍放行很靠后的收藏。
        from xhs_wiki.ledger import Ledger
        note = {"note_id": f"{99999:024x}", "list_position": 99999}
        with Ledger(Path(self.tmp.name) / "ledger.sqlite3") as ledger:
            ledger.upsert_discovered([note], first_import_limit=None)
            self.assertEqual(ledger.get(note["note_id"])["stage"], "discovered")


class TestFirstImportApi(DaemonTestBase):

    def test_first_import_null_saved_as_null(self):
        """选"全部收藏"：settings.json 里 first_import_limit 是 null。"""
        status, data = self.post("/api/setup/first-import", body={"limit": None})
        self.assertEqual(status, 200)
        self.assertIsNone(data["first_import_limit"])
        saved = json.loads((self.home / "settings.json").read_text())
        self.assertIsNone(saved["first_import_limit"])

    def test_first_import_rejects_out_of_range(self):
        status, data = self.post("/api/setup/first-import", body={"limit": 1001})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "first_import_limit_invalid")


# ---------------------------------------------------------------------------
# 5. 切换语音模型 → AssetManager 被唤醒并准备新模型
# ---------------------------------------------------------------------------

class TestAsrSwitchWakesAssets(DaemonTestBase):

    def test_settings_asr_change_wakes_manager(self):
        """POST /api/settings 改 asr_model 后：资源线程被立刻唤醒，
        发现新模型缺失并开始"下载"（假的 install，绝不访问网络）。"""
        installed = []
        install_event = threading.Event()
        lock = threading.Lock()

        # 假的检查：whisper 视为已装，sensevoice 视为缺失（按当前设置回答）。
        def fake_check():
            name = self.store.load_settings().get("asr_model") or "whisper"
            return ({"xhs_downloader": True, "asr_model": name == "whisper"},
                    name)

        def fake_install(kind, name, progress):
            with lock:
                installed.append((kind, name))
            if kind == "asr_model" and name == "sensevoice":
                install_event.set()

        manager = AssetManager(self.store, check=fake_check,
                               install=fake_install, interval=999)
        self.supervisor.assets = manager
        manager.start()
        self.addCleanup(manager.stop)
        # 先让初始状态收敛（whisper 已就绪）。
        for _ in range(100):
            if manager.state()["asr_model"]["state"] == "ready":
                break
            threading.Event().wait(0.05)
        self.assertEqual(manager.state()["asr_model"]["state"], "ready")

        # 在日常页切换语音模型。
        status, data = self.post("/api/settings", body={"asr_model": "sensevoice"})
        self.assertEqual(status, 200)
        self.assertEqual(data["settings"]["asr_model"], "sensevoice")

        # 资源线程被唤醒并开始准备新模型（不需要等 999 秒的复查间隔）。
        self.assertTrue(install_event.wait(timeout=10),
                        "AssetManager 没有被唤醒去下载新语音模型")
        with lock:
            self.assertIn(("asr_model", "sensevoice"), installed)
        # 已就绪的下载组件不受影响。
        self.assertTrue(manager.all_ready())


# ---------------------------------------------------------------------------
# 6. console.html 的相关改动（字符串断言，页面行为靠浏览器保证）
# ---------------------------------------------------------------------------

class TestConsoleHtmlContent(unittest.TestCase):

    def test_restart_button_copy(self):
        self.assertIn('id="restart-setup"', HTML)
        self.assertIn("重新走一遍首次设置：可以换知识库位置、引擎、整理模型和密钥。已整理的内容不会删除。", HTML)
        self.assertIn("'/api/setup/restart'", HTML)
        self.assertIn("请等当前运行结束", HTML)

    def test_key_tip_for_api_providers(self):
        self.assertIn("换成 API 方式或修改密钥，请点「重新设置」", HTML)
        self.assertIn('id="e-key-tip"', HTML)

    def test_asr_settings_present(self):
        self.assertIn('id="asr-form"', HTML)
        self.assertIn('id="asr-model"', HTML)
        self.assertIn("Whisper（默认，准确，约 1.5 GB）", HTML)
        self.assertIn("SenseVoice（快速，约 230 MB，英文效果差）", HTML)
        self.assertIn("已下载的另一个模型不会被删除", HTML)

    def test_all_favorites_copy(self):
        # 说明文字由 JS 用设置里的 max_new_per_run 动态拼出。
        self.assertIn('id="w-limit-all-desc"', HTML)
        self.assertIn("每次运行最多新增 '+maxNew+' 条（定时设置里可改），收藏多的话会分几天完成。", HTML)


if __name__ == "__main__":
    unittest.main()
