# -*- coding: utf-8 -*-
"""安全与健壮性修复的回归测试（Python 部分）。

每个测试类对应一条（或一组）已确认的问题：
1. 换知识库位置：新位置用大小写 / 固件链接 / Unicode 组合写法指向旧库内部时
   必须拒绝；只在跨磁盘时复制，复制核对完才删原件；改名报其他错误绝不删除；
2. 旧目录里有用户自己的文件（"文稿"、Obsidian 库）时只搬五类生成内容；
   "文稿""桌面"等系统文件夹本身不能当知识库；
3. xhs-wiki open 的身份证明和一次性登录码都绑定端口（转发挑战骗不过去）；
   提示页写出带 --port 的命令；跳转页里没有主令牌、用过即删、不能重放；
4. 首次导入范围：首扫没看全前 N 条时不建基线；基线之后比所有已知笔记都靠后的
   新笔记按旧收藏跳过；取消再收藏的旧笔记不影响当天的新收藏；
5. 私有权限：向导新建的知识库根目录 0700，页面文件 0600；
6. 下载组件就绪判断：完成标记绑定 venv，venv 重建后判为未就绪；
7. 低优先级：kb_root 不可访问时报错而不是断开连接、Unicode 控制/格式字符、
   数据卷根目录、LaunchAgent 的 PATH、连接超时、整理时单个主题超时。

红线自查：全部用临时目录（XHS_WIKI_HOME / XHS_WIKI_LOG_DIR / Skill 目录 /
知识库都在 tempfile 下，需要"主目录"的地方把 HOME 指到临时目录）；
只连测试自己在 127.0.0.1 上起的服务；模型、opener、spawn、钥匙串全是假的。
"""

import errno
import http.client
import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
import unicodedata
import unittest
import unittest.mock
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

from test_markdown_engine import (
    FakeProvider, assign_new, card_resp, make_pkg, page_from_prompt, parse_note_ids,
)
from test_runner import Rig, fake_items
from test_y2_console_security import RecordingOpener, Y2HttpBase

from xhs_wiki import assets, cli, config, console_auth, daemon, skill
from xhs_wiki.engines.markdown import MarkdownEngine
from xhs_wiki.engines.markdown import pages
from xhs_wiki.ledger import Ledger
from xhs_wiki.llm.base import LLMError
from xhs_wiki.store import is_same_or_inside, validate_kb_root

# 引擎测试用的固定时间（和 test_markdown_engine 一致）。
FIXED_TIME = datetime(2026, 2, 15, 10, 0, 0)

# 轻量引擎生成过的内容（虚构）：标记目录 + 索引 + 暂缓 + 主题 + 来源。
GENERATED_FILES = {
    ".xhs-wiki/engine.sqlite3": b"generated-db",
    "索引.md": "# 索引\n".encode(),
    "暂缓.md": "# 暂缓\n".encode(),
    "主题/虚构主题.md": "主题内容\n".encode(),
    "来源/虚构来源.md": "来源内容\n".encode(),
}


def write_files(root: Path, files: dict) -> None:
    """按 {相对路径: 字节} 写一批文件（目录自动建）。"""
    for relative, data in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def tree(root: Path) -> dict:
    """目录下所有普通文件的 {相对路径: 内容}（目录不存在时为空）。"""
    if not root.exists():
        return {}
    return {str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


def same_dir(a: Path, b: Path) -> bool:
    """两个写法是不是磁盘上的同一个目录（不存在时为否）。"""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 1. 换位置时新位置用别的写法指向旧库内部，必须拒绝、绝不删除
# ---------------------------------------------------------------------------

class KbMigrationBase(Y2HttpBase):
    """先把知识库设到一个临时位置，并写入生成内容。"""

    old_name = "Case-KB"

    def setUp(self):
        super().setUp()
        self.tmp_path = Path(self.tmp.name)
        self.old = self.tmp_path / self.old_name
        status, data = self.post("/api/setup/kb-root", body={"path": str(self.old)})
        self.assertEqual(status, 200, data)
        write_files(self.old, GENERATED_FILES)

    def move_to(self, new):
        return self.post("/api/setup/kb-root", body={"path": str(new)})

    def assert_untouched(self):
        self.assertEqual(tree(self.old), GENERATED_FILES)
        self.assertEqual(self.store.load_settings()["kb_root"], str(self.old))


class TestMigrationAliasInsideOldKb(KbMigrationBase):

    def test_case_variant_inside_old_rejected(self):
        alias_parent = self.tmp_path / self.old_name.lower()
        if not same_dir(alias_parent, self.old):
            self.skipTest("文件系统区分大小写")
        status, data = self.move_to(alias_parent / "sub")
        self.assertEqual(status, 400, data)
        self.assertEqual(data["error"], "kb_root_inside_old_kb")
        self.assert_untouched()

    def test_data_volume_alias_inside_old_rejected(self):
        alias_parent = Path("/System/Volumes/Data" + str(self.old.resolve()))
        if not same_dir(alias_parent, self.old):
            self.skipTest("这台机器没有 /System/Volumes/Data 别名")
        status, data = self.move_to(alias_parent / "sub")
        self.assertEqual(status, 400, data)
        self.assertEqual(data["error"], "kb_root_inside_old_kb")
        self.assert_untouched()

    def test_rename_einval_never_falls_back_to_copy_and_delete(self):
        """改名报 EINVAL（把目录挪进自己里面）：直接失败，原件一个不少。"""
        def into_itself(*_args, **_kwargs):
            raise OSError(errno.EINVAL, "generated: into itself")

        with unittest.mock.patch.object(daemon.os, "rename", side_effect=into_itself):
            status, data = self.move_to(self.tmp_path / "elsewhere" / "kb")
        self.assertEqual(status, 500)
        self.assertEqual(data["error"], "kb_root_move_failed")
        self.assert_untouched()
        self.assertFalse((self.tmp_path / "elsewhere" / "kb").exists())

    def test_cross_device_copy_mismatch_keeps_original(self):
        """跨磁盘时复制后核对不一致：删掉半份副本，原件不动，设置不变。"""
        def cross_device(*_args, **_kwargs):
            raise OSError(errno.EXDEV, "generated: cross-device")

        new = self.tmp_path / "other-disk" / "kb"
        with unittest.mock.patch.object(daemon.os, "rename", side_effect=cross_device), \
                unittest.mock.patch.object(daemon.filecmp, "cmp", return_value=False):
            status, data = self.move_to(new)
        self.assertEqual(status, 500)
        self.assertEqual(data["error"], "kb_root_move_failed")
        self.assert_untouched()
        self.assertFalse(new.exists())

    def test_cross_device_copy_verified_then_original_removed(self):
        """跨磁盘正常情况：完整复制、核对通过后才删原件。"""
        def cross_device(*_args, **_kwargs):
            raise OSError(errno.EXDEV, "generated: cross-device")

        new = self.tmp_path / "other-disk" / "kb"
        with unittest.mock.patch.object(daemon.os, "rename", side_effect=cross_device):
            status, data = self.move_to(new)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["moved"], "all")
        self.assertEqual(tree(new), GENERATED_FILES)
        self.assertFalse(self.old.exists())

    def test_remove_guard_refuses_alias_of_protected_copy(self):
        """删除保险：要删的目录和要保留的目录互相包含（哪怕写法不同）就拒绝。"""
        alias = self.tmp_path / self.old_name.lower()
        if not same_dir(alias, self.old):
            self.skipTest("文件系统区分大小写")
        with self.assertRaises(ValueError):
            daemon._remove_tree_guarded(alias, must_not_touch=self.old / "主题")
        self.assert_untouched()


class TestMigrationUnicodeAlias(KbMigrationBase):
    """旧位置名字是 NFC 编码，新位置用 NFD 编码的同名目录。"""

    old_name = unicodedata.normalize("NFC", "Café-知识库")

    def test_nfd_variant_inside_old_rejected(self):
        alias_parent = self.tmp_path / unicodedata.normalize("NFD", self.old_name)
        self.assertNotEqual(str(alias_parent), str(self.old))
        if not same_dir(alias_parent, self.old):
            self.skipTest("文件系统区分 Unicode 组合方式")
        self.assertTrue(is_same_or_inside(alias_parent / "sub", self.old))
        status, data = self.move_to(alias_parent / "sub")
        self.assertEqual(status, 400, data)
        self.assertEqual(data["error"], "kb_root_inside_old_kb")
        self.assert_untouched()


# ---------------------------------------------------------------------------
# 2. 旧目录里有用户自己的文件时，只搬生成内容
# ---------------------------------------------------------------------------

class TestMigrationKeepsUserFiles(KbMigrationBase):

    old_name = "My Notes Vault"

    def test_only_generated_entries_move(self):
        user_files = {"工作/虚构合同.txt": "合同".encode(), "虚构日记.md": "日记".encode(),
                      ".obsidian/app.json": b"{}"}
        write_files(self.old, user_files)
        new = self.tmp_path / "new-kb"
        status, data = self.move_to(new)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["migrated"])
        self.assertEqual(data["moved"], "generated")
        self.assertEqual(tree(new), GENERATED_FILES)          # 生成内容全到了新位置
        self.assertEqual(tree(self.old), user_files)           # 用户文件原地不动
        self.assertEqual(stat.S_IMODE(new.stat().st_mode), 0o700)
        self.assertEqual(self.store.load_settings()["kb_root"], str(new))

    def test_only_generated_entries_move_across_devices(self):
        write_files(self.old, {"我的笔记.md": "我的".encode()})

        def cross_device(*_args, **_kwargs):
            raise OSError(errno.EXDEV, "generated: cross-device")

        new = self.tmp_path / "other-disk" / "kb"
        with unittest.mock.patch.object(daemon.os, "rename", side_effect=cross_device):
            status, data = self.move_to(new)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["moved"], "generated")
        self.assertEqual(tree(new), GENERATED_FILES)
        self.assertEqual(tree(self.old), {"我的笔记.md": "我的".encode()})

    def test_failure_midway_rolls_back_moved_entries(self):
        """逐个改名时第 3 个失败：前面改过去的改回来，用户文件和原件都在。"""
        write_files(self.old, {"我的笔记.md": "我的".encode()})
        real_rename = os.rename
        calls = []

        def flaky(source, target):
            calls.append(source)
            if len(calls) == 3:
                raise OSError(errno.EIO, "generated disk error")
            return real_rename(source, target)

        with unittest.mock.patch.object(daemon.os, "rename", side_effect=flaky):
            status, data = self.move_to(self.tmp_path / "new-kb")
        self.assertEqual(status, 500)
        self.assertEqual(data["error"], "kb_root_move_failed")
        self.assertEqual(tree(self.old), {**GENERATED_FILES, "我的笔记.md": "我的".encode()})
        self.assertEqual(self.store.load_settings()["kb_root"], str(self.old))
        self.assertFalse((self.tmp_path / "new-kb").exists())  # 这次新建的空目录已清掉


class TestHomeStandardFoldersRejected(unittest.TestCase):
    """"文稿""桌面""下载"等系统文件夹本身不能当知识库（子文件夹可以）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        patcher = unittest.mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_standard_folders_rejected_children_allowed(self):
        for name in ("Documents", "Desktop", "Downloads", "Public"):
            (self.home / name).mkdir()
            with self.assertRaises(ValueError, msg=name):
                validate_kb_root(f"~/{name}")
            self.assertEqual(validate_kb_root(f"~/{name}/My Wiki"),
                             self.home / name / "My Wiki")

    def test_case_variant_of_documents_rejected(self):
        (self.home / "Documents").mkdir()
        if not same_dir(self.home / "documents", self.home / "Documents"):
            self.skipTest("文件系统区分大小写")
        with self.assertRaises(ValueError):
            validate_kb_root("~/documents")


# ---------------------------------------------------------------------------
# 3. xhs-wiki open 的转发挑战、一次性登录码、提示页端口
# ---------------------------------------------------------------------------

class RelayHandler(BaseHTTPRequestHandler):
    """占着另一个端口的"中间人"：/healthz 原样转发给真服务，/auth 记下表单。"""

    target_port = None
    captured = []

    def log_message(self, *_):
        pass

    def do_GET(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.target_port, timeout=5)
        try:
            conn.request("GET", self.path,
                         headers={"Host": f"127.0.0.1:{self.target_port}"})
            response = conn.getresponse()
            body = response.read()
        finally:
            conn.close()
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        type(self).captured.append(self.rfile.read(length))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class TestOpenRelayAndLoginCode(Y2HttpBase):

    def start_relay(self):
        handler = type("Relay", (RelayHandler,), {"target_port": self.port,
                                                  "captured": []})
        relay = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=relay.serve_forever, daemon=True).start()
        self.addCleanup(relay.server_close)
        self.addCleanup(relay.shutdown)
        return relay, handler

    def test_relayed_challenge_is_refused(self):
        """转发挑战给真服务拿到的答案按真服务的端口算，open 核对失败、不打开。"""
        relay, handler = self.start_relay()
        opener = RecordingOpener()
        code = cli.main(["--home", str(self.home), "open", "--port",
                         str(relay.server_port)], opener=opener)
        self.assertEqual(code, 1)
        self.assertEqual(opener.calls, [])
        self.assertFalse((self.home / "open-console.html").exists())
        self.assertEqual(handler.captured, [])

    def test_hint_page_shows_port_when_not_default(self):
        status, _, body = self.raw("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(f"xhs-wiki open --port {self.port}", body.decode("utf-8"))
        self.assertIn("xhs-wiki open</code>",
                      daemon.no_auth_page(config.DEFAULT_PORT).decode("utf-8"))

    def post_code(self, fields):
        return self.raw("POST", "/auth", body=urlencode(fields),
                        headers={"Content-Type": "application/x-www-form-urlencoded",
                                 "Origin": "null"})

    def test_code_for_other_port_rejected(self):
        fields = console_auth.mint_login(self.token(), self.port + 1)
        status, headers, _ = self.post_code(fields)
        self.assertEqual(status, 403)
        self.assertNotIn("set-cookie", headers)

    def test_expired_code_rejected(self):
        fields = console_auth.mint_login(
            self.token(), self.port,
            now=time.time() - console_auth.LOGIN_CODE_TTL_SECONDS - 10)
        status, _, _ = self.post_code(fields)
        self.assertEqual(status, 403)

    def test_code_single_use(self):
        fields = console_auth.mint_login(self.token(), self.port)
        first, headers, _ = self.post_code(fields)
        self.assertEqual(first, 303)
        self.assertEqual(headers["set-cookie"].split(";")[0].split("=", 1)[1],
                         self.token())
        second, _, _ = self.post_code(fields)
        self.assertEqual(second, 403)

    def test_open_failed_message_no_longer_points_to_html_file(self):
        self.assertNotIn("open-console.html", cli.OPEN_MESSAGES["open_failed"])
        self.assertIn("xhs-wiki open", cli.OPEN_MESSAGES["open_failed"])


# ---------------------------------------------------------------------------
# 4. 首次范围基线
# ---------------------------------------------------------------------------

def scan_items(order):
    """把一串笔记序号排成一次扫描结果（位置从 0 开始，内容全是虚构的）。"""
    items = fake_items(max(order) + 1)
    return [{**items[index], "list_position": position}
            for position, index in enumerate(order)]


class TestFirstImportBaseline(unittest.TestCase):
    """真实 runner + Ledger；扫描、下载、解析、引擎都是假的。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.patcher = unittest.mock.patch.dict(os.environ, {
            "XHS_WIKI_HOME": str(self.tmp_path / "app"),
            "XHS_WIKI_LOG_DIR": str(self.tmp_path / "logs"),
            "XHS_WIKI_SKILLS_ROOT": str(self.tmp_path / "skills")})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def make_rig(self, items, **settings):
        import pytest
        # Rig 要一个 pytest 风格的 monkeypatch 对象（用来设环境变量），结束时撤销。
        monkeypatch = pytest.MonkeyPatch()
        self.addCleanup(monkeypatch.undo)
        rig = Rig(self.tmp_path, monkeypatch, scan_items=items,
                  settings={"first_import_limit": 20, "max_new_per_run": 200, **settings})
        self.addCleanup(rig.ledger.close)
        return rig

    def scan(self, rig, items, *, incomplete=False, nominal=None):
        rig.scan_result.update(status="ok", items=items, visible=len(items),
                               nominal=nominal or len(items),
                               reason="scan_incomplete" if incomplete else None)

    def processed(self, rig):
        return sum(1 for note in rig.ledger.all_notes() if note["stage"] == "distilled")

    def test_incomplete_first_scan_does_not_consume_baseline(self):
        """首扫只拿到首屏 10/300：之后完整扫描，总共只导入最近 20 条。"""
        rig = self.make_rig(scan_items(range(10)))
        self.scan(rig, scan_items(range(10)), incomplete=True, nominal=300)
        first = rig.run()
        self.assertEqual(first["counts"], {"distilled": 10})
        self.scan(rig, scan_items(range(300)))
        rig.run()
        self.assertEqual(self.processed(rig), 20)
        self.assertEqual(rig.ledger.counts().get("skipped"), 280)
        self.assertEqual(rig.ledger.next_batch("discovered", 1000), [])

    def test_partial_first_scan_then_deeper_notes_are_skipped(self):
        """首扫 200/300（没标不完整）：之后冒出来的 100 条更早的收藏全部跳过。"""
        rig = self.make_rig(scan_items(range(200)))
        first = rig.run()
        self.assertEqual(first["counts"], {"distilled": 20, "skipped": 180})
        self.scan(rig, scan_items(range(300)))
        second = rig.run()
        self.assertEqual(second["processed"]["download"]["ok"], 0)
        self.assertEqual(self.processed(rig), 20)
        self.assertEqual(rig.ledger.counts().get("skipped"), 280)

    def test_new_favorites_after_refavorited_old_note_still_imported(self):
        """取消再收藏一条旧笔记（它跳到最前）：当天真正的新收藏照常导入。"""
        rig = self.make_rig(scan_items(range(30)))
        rig.run()
        # 新收藏 30、31 之后，用户又把旧笔记 25 重新收藏了一次（跳到最前）。
        order = [25, 31, 30] + [i for i in range(30) if i != 25]
        self.scan(rig, scan_items(order))
        second = rig.run()
        self.assertEqual(second["processed"]["download"]["ok"], 2)
        self.assertEqual(self.processed(rig), 22)

    def test_switch_to_all_restores_positionally_skipped(self):
        """改选"全部"后，按位置判定跳过的旧收藏也会恢复并处理。"""
        rig = self.make_rig(scan_items(range(30)))
        rig.run()
        self.scan(rig, scan_items(range(40)))
        rig.run()
        self.assertEqual(rig.ledger.counts().get("skipped"), 20)
        rig.store.update_settings({"first_import_limit": None})
        rig.run()
        self.assertEqual(self.processed(rig), 40)

    def test_ledger_rules_directly(self):
        with Ledger(self.tmp_path / "ledger.sqlite3") as ledger:
            # 不完整、又没看全前 N 条（5 < 10）：不建基线、不跳过。
            ledger.upsert_discovered(fake_items(5), first_import_limit=10,
                                     scan_complete=False)
            self.assertEqual(ledger.counts(), {"discovered": 5})
            # 之后完整扫描到 30 条：这时才建基线，位置 ≥ 10 的跳过。
            ledger.upsert_discovered(fake_items(30), first_import_limit=10)
            self.assertEqual(ledger.counts(), {"discovered": 10, "skipped": 20})
        with Ledger(self.tmp_path / "ledger2.sqlite3") as ledger:
            # 不完整、但看到的条数 ≥ N：前 N 条已看全，可以建基线。
            ledger.upsert_discovered(fake_items(5), first_import_limit=3,
                                     scan_complete=False)
            self.assertEqual(ledger.counts(), {"discovered": 3, "skipped": 2})


# ---------------------------------------------------------------------------
# 5. 新建知识库根目录 0700、页面 0600
# ---------------------------------------------------------------------------

class TestKnowledgeBasePermissions(Y2HttpBase):

    def test_wizard_creates_private_root_and_keeps_existing_mode(self):
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        new = Path(self.tmp.name) / "fresh" / "kb"
        status, data = self.post("/api/setup/kb-root", body={"path": str(new)})
        self.assertEqual(status, 200, data)
        self.assertEqual(stat.S_IMODE(new.stat().st_mode), 0o700)
        existing = Path(self.tmp.name) / "shared-kb"
        existing.mkdir(mode=0o755)
        existing.chmod(0o755)
        status, data = self.post("/api/setup/kb-root", body={"path": str(existing)})
        self.assertEqual(status, 200, data)
        self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o755)

    def test_pages_are_written_private(self):
        target = Path(self.tmp.name) / "kb" / "索引.md"
        pages.atomic_write(target, "# 索引\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.read_text(encoding="utf-8"), "# 索引\n")
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["索引.md"])


# ---------------------------------------------------------------------------
# 6. 完成标记绑定 venv
# ---------------------------------------------------------------------------

class TestDownloaderMarkerBoundToVenv(unittest.TestCase):

    def setUp(self):
        from test_assets import fake_manifest
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "app"
        manifest = fake_manifest(Path(self.tmp.name))
        patcher = unittest.mock.patch.object(assets, "load_manifest", return_value=manifest)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.component = self.home / "vendor" / manifest["xhs_downloader"]["extract_dir"]
        self.component.mkdir(parents=True)
        self.make_venv()

    def make_venv(self):
        venv = self.home / "venv"
        venv.mkdir(parents=True, exist_ok=True)
        (venv / "pyvenv.cfg").write_text("home = /generated/python\n", encoding="utf-8")

    def mark_ready(self):
        assets._write_downloader_ready(self.home / "vendor" / ".xhs-downloader-ready",
                                       assets.downloader_marker_text(self.home, "1.0"))

    def test_ready_until_venv_rebuilt(self):
        self.mark_ready()
        self.assertTrue(assets.xhs_downloader_ready(self.home))
        shutil.rmtree(self.home / "venv")      # 按 install.sh 的提示删掉 venv 重建
        self.make_venv()
        self.assertFalse(assets.xhs_downloader_ready(self.home))

    def test_ready_false_when_venv_dir_recreated_without_cfg(self):
        """venv 里没有 pyvenv.cfg 时，也能靠 venv 目录本身的身份认出"换了一个"。"""
        (self.home / "venv" / "pyvenv.cfg").unlink()
        self.mark_ready()
        self.assertTrue(assets.xhs_downloader_ready(self.home))
        shutil.rmtree(self.home / "venv")
        (self.home / "venv" / "bin").mkdir(parents=True)
        self.assertFalse(assets.xhs_downloader_ready(self.home))

    def test_old_version_only_marker_not_ready(self):
        (self.home / "vendor" / ".xhs-downloader-ready").write_text("1.0", encoding="utf-8")
        self.assertFalse(assets.xhs_downloader_ready(self.home))


# ---------------------------------------------------------------------------
# 7. 低优先级
# ---------------------------------------------------------------------------

class TestKbRootInaccessible(Y2HttpBase):
    """kb_root 所在目录没有访问权限：明确报错，不再让请求连接直接断开。"""

    def setUp(self):
        super().setUp()
        self.locked = Path(self.tmp.name) / "locked"
        (self.locked / "kb").mkdir(parents=True)
        status, _ = self.post("/api/setup/kb-root", body={"path": str(self.locked / "kb")})
        self.assertEqual(status, 200)
        self.locked.chmod(0)
        self.addCleanup(self.locked.chmod, 0o700)

    def test_other_settings_and_status_still_work(self):
        status, data = self.post("/api/settings", body={"daily_time": "04:30"})
        self.assertEqual(status, 200, data)
        status, _ = self.request("GET", "/api/status")
        self.assertEqual(status, 200)

    def test_kb_endpoints_report_inaccessible(self):
        status, data = self.post("/api/open-kb")
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "kb_root_inaccessible")
        status, data = self.post("/api/setup/kb-root",
                                 body={"path": str(self.locked / "kb" / "x")})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "kb_root_inaccessible")
        self.assertEqual(self.opener_calls, [])


class TestKbRootCharactersAndVolumes(unittest.TestCase):

    def test_unicode_control_format_and_separator_chars_rejected(self):
        base = tempfile.gettempdir()
        for ch in ("\x85", "\x9b", " ", " ", "‮", "​"):
            value = f"{base}/kb{ch}## INJECTED"
            with self.assertRaises(ValueError, msg=repr(ch)):
                validate_kb_root(value)
            with self.assertRaises(ValueError, msg=repr(ch)):
                skill.render({"kb_root": value, "engine": "markdown",
                              "setup_completed": True})

    def test_data_volume_root_rejected(self):
        if not Path("/System/Volumes/Data").is_dir():
            self.skipTest("没有数据卷挂载点")
        with self.assertRaises(ValueError):
            validate_kb_root("/System/Volumes/Data")


class TestLaunchAgentPathHardening(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.python = tmp / "venv" / "bin" / "python"
        self.python.parent.mkdir(parents=True)
        self.safe = tmp / "safe-bin"
        self.world = tmp / "world-writable-bin"
        self.other_owner = tmp / "other-owner-bin"
        for directory in (self.safe, self.world, self.other_owner):
            directory.mkdir()
        self.world.chmod(0o777)
        self.control = tmp / "bad\x1bname"
        self.control.mkdir()

    def path_entries(self):
        real_stat = os.stat

        def fake_stat(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if str(path) == str(self.other_owner):
                # 模拟"属主是另一个普通用户、组可写"的目录。
                values = list(info)
                values[stat.ST_UID] = os.getuid() + 12345
                values[stat.ST_MODE] = stat.S_IFDIR | 0o775
                return os.stat_result(values)
            return info

        environ = {"PATH": ":".join([str(self.world), str(self.other_owner),
                                     str(self.control), str(self.safe)])}
        return cli.launch_agent_path(str(self.python), environ,
                                     stat_fn=fake_stat).split(":")

    def test_writable_and_control_char_dirs_skipped(self):
        entries = self.path_entries()
        self.assertNotIn(str(self.world), entries)
        self.assertNotIn(str(self.other_owner), entries)
        self.assertNotIn(str(self.control), entries)
        self.assertIn(str(self.safe), entries)

    def test_system_dirs_before_user_dirs(self):
        entries = self.path_entries()
        self.assertEqual(entries[:5], [str(self.python.parent), "/usr/bin", "/bin",
                                       "/usr/sbin", "/sbin"])
        self.assertLess(entries.index("/usr/bin"), entries.index(str(self.safe)))

    def test_print_launch_agent_errors_go_to_stderr(self):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict(os.environ, {"HOME": "/generated\x1bhome"}):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["print-launch-agent", "--python", str(self.python)])
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("错误", err.getvalue())


class TestConnectionLimits(unittest.TestCase):
    """空闲连接和只发一半的请求在超时后被关掉，服务不会被占死。"""

    def setUp(self):
        from test_daemon import FakeEngine, FakeKeychain, FakeProvider, FakeSpawner
        from xhs_wiki.daemon import ServiceServer, Supervisor
        from xhs_wiki.store import ServiceStore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name) / "app"
        patcher = unittest.mock.patch.dict(os.environ, {
            "XHS_WIKI_HOME": str(home), "XHS_WIKI_LOG_DIR": str(Path(self.tmp.name) / "logs")})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("timeout", 1),):
            patch = unittest.mock.patch.object(daemon.ServiceHandler, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        cap = unittest.mock.patch.object(daemon, "MAX_CONCURRENT_CONNECTIONS", 3)
        cap.start()
        self.addCleanup(cap.stop)
        store = ServiceStore(home)
        store.update_settings({"kb_root": str(Path(self.tmp.name) / "kb")})
        self.server = ServiceServer(
            ("127.0.0.1", 0), supervisor=Supervisor(store, spawn=FakeSpawner()),
            keychain=lambda: FakeKeychain(), make_provider=lambda s: FakeProvider(),
            make_engine=lambda s: FakeEngine(), opener=lambda args: None,
            skills_root=Path(self.tmp.name) / "skills")
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def healthz(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/healthz", headers={"Host": f"127.0.0.1:{self.port}"})
            return conn.getresponse().status
        finally:
            conn.close()

    def test_idle_and_half_sent_connections_are_closed(self):
        idle = socket.create_connection(("127.0.0.1", self.port))
        half = socket.create_connection(("127.0.0.1", self.port))
        half.sendall(b"POST /auth HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                     b"Content-Length: 100\r\n\r\npartial" % self.port)
        for sock in (idle, half):
            sock.settimeout(5)
            self.assertEqual(sock.recv(1024), b"")   # 服务端超时后主动关闭
            sock.close()
        self.assertEqual(self.healthz(), 200)

    def test_connection_cap_recovers_after_timeout(self):
        sockets = [socket.create_connection(("127.0.0.1", self.port)) for _ in range(6)]
        try:
            time.sleep(2.5)   # 超过 1 秒超时，占着名额的连接都已被关掉
            self.assertEqual(self.healthz(), 200)
        finally:
            for sock in sockets:
                sock.close()


class TestDistillTopicTimeout(unittest.TestCase):
    """某个主题页超时：只有这个主题失败，其他主题的笔记照常记成功。"""

    def test_one_topic_timeout_does_not_fail_others(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p1, p2 = make_pkg(1), make_pkg(2)

        def assign_two(prompt):
            ids = parse_note_ids(prompt)
            first = json.loads(assign_new([ids[0]], name="虚构主题甲"))
            second = json.loads(assign_new([ids[1]], name="虚构主题乙"))
            return json.dumps({"assignments": first["assignments"] + second["assignments"]},
                              ensure_ascii=False)

        def page_or_timeout(prompt):
            if "虚构主题乙" in prompt:
                raise LLMError("timeout")
            return page_from_prompt(prompt)

        provider = FakeProvider([card_resp(1), card_resp(2), assign_two,
                                 page_or_timeout, page_or_timeout])
        engine = MarkdownEngine(Path(tmp.name) / "kb", provider,
                                clock=lambda: FIXED_TIME, concurrency=1)
        self.addCleanup(lambda: engine.store.close())
        engine.ingest([p1, p2])
        result = engine.distill([p1.note_id, p2.note_id])
        self.assertEqual(result.per_note[p1.note_id], {"status": "ok", "code": None})
        self.assertEqual(result.per_note[p2.note_id], {"status": "failed", "code": "timeout"})
        self.assertEqual(engine.store.pending_page_ids(), [2])

    def test_breaker_errors_still_propagate(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        def quota(prompt):
            raise LLMError("quota_or_rate_limit")

        provider = FakeProvider([card_resp(1), lambda p: assign_new(parse_note_ids(p)), quota])
        engine = MarkdownEngine(Path(tmp.name) / "kb", provider, clock=lambda: FIXED_TIME)
        self.addCleanup(lambda: engine.store.close())
        engine.ingest([make_pkg(1)])
        with self.assertRaises(LLMError):
            engine.distill([make_pkg(1).note_id])


class TestDocsMatchBehavior(unittest.TestCase):
    """README 的说明和新行为一致（卸载要同时带 --yes --purge-kb、换端口要带 --port）。"""

    def test_readme(self):
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(
            encoding="utf-8")
        self.assertIn("bash uninstall.sh --yes --purge-kb", readme)
        self.assertNotIn("不要随意加 `--yes`", readme)
        self.assertIn("xhs-wiki open --port 18767", readme)
        self.assertNotIn("然后打开 `http://127.0.0.1:18767`", readme)
        self.assertIn("console-token", readme)
