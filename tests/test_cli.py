# -*- coding: utf-8 -*-
"""test_cli.py —— cli.py 的测试。

run/cleanup/doctor 用注入的假依赖（deps_factory），login/status 用
monkeypatch 换假实现；不碰真实 APP_HOME、不启动浏览器、不发网络请求。
"""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from xhs_wiki import cli, config
from xhs_wiki.ledger import Ledger
from xhs_wiki.store import BEIJING_TZ, ServiceStore

FIXED_NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=BEIJING_TZ)


def nid(i: int) -> str:
    return f"{i + 1:024x}"


class FakeProvider:
    def check(self):
        return (True, "ok")


class FakeEngine:
    name = "fake"

    def health(self):
        return (True, "ok")

    def source_exists(self, note_id, engine_ref):
        return True


def make_fake_deps(tmp_path, monkeypatch, home):
    """构造一套最小的假 RunnerDeps（cli 的 run/cleanup/doctor 用）。

    home 是 cli 的 --home 参数同一个目录，保证 deps 和 cli 写同一处。
    """
    from xhs_wiki import runner
    monkeypatch.setenv("XHS_WIKI_HOME", str(home))
    store = ServiceStore(config.app_home())
    ledger = Ledger(config.ledger_path())
    # kb_root 指到临时目录：doctor 的"知识库可写"检查绝不碰真实的 ~ 目录。
    store.update_settings({"setup_completed": True,
                           "kb_root": str(tmp_path / "kb"),
                           "profile_url": "https://www.xiaohongshu.com/user/"
                                          "profile/" + "d" * 24})

    def fake_scan(profile_url, *, headless):
        return {"status": "ok", "visible": 0, "nominal": 0, "reason": None,
                "items": []}

    return runner.RunnerDeps(
        store=store, ledger=ledger, now=lambda: FIXED_NOW,
        sleep=lambda s: None, notify=lambda code: True,
        disk_free_bytes=lambda path: 500 * 1024 ** 3,
        scan_favorites=fake_scan,
        detect_profile_url=lambda: None,
        download_note=lambda *a, **k: {"status": "failed", "code": "x"},
        build_package=lambda *a: {"package": None, "package_sha256": ""},
        make_engine=lambda settings: FakeEngine(),
        make_provider=lambda settings: FakeProvider(),
        key_exists=lambda name: True,
        cleanup_apply=lambda ledger, engine, store: {"notes": 0,
                                                     "freed_bytes": 0,
                                                     "skipped": []},
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    """空的临时 APP_HOME（cli 的 --home 会指向它）。"""
    return tmp_path / "cli-home"


@pytest.fixture
def output(monkeypatch):
    """收集 cli 的全部打印（_print 集中在一处，直接换掉）。"""
    lines = []
    monkeypatch.setattr(cli, "_print", lines.append)
    return lines


class TestRun:
    def test_run_manual(self, tmp_path, monkeypatch, home, output):
        """run 子命令：空扫描正常跑完，退出码 0，打印结果摘要。"""
        deps = make_fake_deps(tmp_path, monkeypatch, home)
        code = cli.main(["--home", str(home), "run", "--trigger", "manual"],
                        deps_factory=lambda: deps)
        assert code == 0
        assert any("completed" in line for line in output)
        assert any("扫描" in line for line in output)
        # 运行记录真的写进了 --home 指定的目录。
        assert (home / "runs.jsonl").exists()

    def test_run_default_trigger_manual(self, tmp_path, monkeypatch, home,
                                        output):
        deps = make_fake_deps(tmp_path, monkeypatch, home)
        cli.main(["--home", str(home), "run"], deps_factory=lambda: deps)
        record = json.loads((home / "runs.jsonl").read_text().splitlines()[0])
        assert record["trigger"] == "manual"

    def test_run_rejects_bad_trigger(self, tmp_path, monkeypatch, home):
        with pytest.raises(SystemExit):
            cli.main(["--home", str(home), "run", "--trigger", "bogus"])


class TestLogin:
    def test_login_success_writes_profile(self, tmp_path, monkeypatch, home,
                                          output):
        """login 成功：写回 profile_url 并清掉 xhs_login 事项。"""
        monkeypatch.setenv("XHS_WIKI_HOME", str(home))
        profile = "https://www.xiaohongshu.com/user/profile/" + "c" * 24
        from xhs_wiki import scan
        monkeypatch.setattr(
            scan, "login_flow",
            lambda user_data_dir, qr_path: {"result": "logged_in",
                                            "profile_url": profile})
        code = cli.main(["--home", str(home), "login"])
        assert code == 0
        store = ServiceStore(config.app_home())
        assert store.load_settings()["profile_url"] == profile
        assert not any(i["code"] == "xhs_login"
                       for i in store.read_status()["needs_user"])

    def test_login_timeout(self, tmp_path, monkeypatch, home, output):
        monkeypatch.setenv("XHS_WIKI_HOME", str(home))
        from xhs_wiki import scan
        monkeypatch.setattr(
            scan, "login_flow",
            lambda user_data_dir, qr_path: {"result": "timeout",
                                            "profile_url": None})
        code = cli.main(["--home", str(home), "login"])
        assert code == 1
        assert any("超时" in line for line in output)


class TestStatus:
    def test_status_empty(self, tmp_path, monkeypatch, home, output):
        monkeypatch.setenv("XHS_WIKI_HOME", str(home))
        code = cli.main(["--home", str(home), "status"])
        assert code == 0
        assert any("还没有收藏记录" in line for line in output)
        assert any("还没有运行记录" in line for line in output)

    def test_status_with_data(self, tmp_path, monkeypatch, home, output):
        monkeypatch.setenv("XHS_WIKI_HOME", str(home))
        deps = make_fake_deps(tmp_path, monkeypatch, home)
        cli.main(["--home", str(home), "run"], deps_factory=lambda: deps)
        output.clear()
        code = cli.main(["--home", str(home), "status"])
        assert code == 0
        assert any("阶段统计" in line for line in output)
        assert any("run-" in line for line in output)


class TestCleanup:
    def _prepare_distilled(self, home):
        """造一条 distilled、带真实媒体文件和解析包的笔记。"""
        import hashlib
        store = ServiceStore(home)
        ledger = Ledger(home / "ledger.sqlite3")
        note_id = nid(0)
        work = home / "work" / note_id
        media = work / "media"
        media.mkdir(parents=True, exist_ok=True)
        data = b"fake-media"
        (media / f"{note_id}.mp4").write_bytes(data)
        package = {"note_id": note_id,
                   "original_media": [
                       {"path": f"media/{note_id}.mp4", "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()}]}
        raw = json.dumps(package, ensure_ascii=False).encode("utf-8")
        (work / "package.json").write_bytes(raw)
        ledger.upsert_discovered([{
            "note_id": note_id, "title": "虚构", "url": "u",
            "signed_url": None, "list_position": 0}])
        ledger.advance(note_id, "downloaded")
        ledger.advance(note_id, "parsed",
                       package_sha256=hashlib.sha256(raw).hexdigest())
        ledger.advance(note_id, "ingested", engine="fake", engine_ref="{}")
        ledger.advance(note_id, "distilled")
        ledger.close()
        return note_id, work / "media" / f"{note_id}.mp4"

    def test_dry_run_deletes_nothing(self, tmp_path, monkeypatch, home, output):
        monkeypatch.setenv("XHS_WIKI_HOME", str(home))
        note_id, media_file = self._prepare_distilled(home)
        code = cli.main(["--home", str(home), "cleanup", "--dry-run"])
        assert code == 0
        assert media_file.exists()
        assert any("可清理" in line for line in output)
        assert any("--apply" in line for line in output)

    def test_apply_deletes(self, tmp_path, monkeypatch, home, output):
        monkeypatch.setenv("XHS_WIKI_HOME", str(home))
        note_id, media_file = self._prepare_distilled(home)
        deps = make_fake_deps(tmp_path, monkeypatch, home)
        monkeypatch.setenv("XHS_WIKI_HOME", str(home))
        code = cli.main(["--home", str(home), "cleanup", "--apply"],
                        deps_factory=lambda: deps)
        assert code == 0
        assert not media_file.exists()
        assert any("已清理" in line for line in output)


class TestDoctor:
    def test_doctor_prints_all_items(self, tmp_path, monkeypatch, home, output):
        """doctor：逐项打印通过或不通过（有不通过的项没关系）。"""
        deps = make_fake_deps(tmp_path, monkeypatch, home)
        code = cli.main(["--home", str(home), "doctor"],
                        deps_factory=lambda: deps)
        assert code == 0
        text = "\n".join(output)
        for label in ("macOS 版本", "Google Chrome", "PyAV",
                      "XHS-Downloader", "语音模型文件", "整理模型", "知识库目录可写"):
            assert label in text, f"缺少检查项：{label}"
        # 每项前面都有 ✓ 或 ✗。
        assert sum(1 for line in output if line[:1] in ("✓", "✗")) >= 7

    def test_doctor_imports_pyav(self, tmp_path, monkeypatch, home, output):
        """用假导入器验证 PyAV 真正加载成功才报通过，无需外部程序。"""
        deps = make_fake_deps(tmp_path, monkeypatch, home)
        imported = []

        # 只记录目标包，不加载模型或触发任何安装动作。
        def fake_import(name):
            imported.append(name)
            return SimpleNamespace()

        monkeypatch.setattr(cli.importlib, "import_module", fake_import)
        assert cli.main(["--home", str(home), "doctor"],
                        deps_factory=lambda: deps) == 0
        assert imported == ["av"]
        assert "✓ PyAV：音视频解析组件可用" in output
        assert not any("ffmpeg" in line for line in output)

    @pytest.mark.parametrize("error_type", [ImportError, OSError])
    def test_doctor_pyav_import_failure_is_safe(
            self, tmp_path, monkeypatch, home, output, error_type):
        """包缺失和二进制加载失败都显示固定提示，继续检查且不泄露异常。"""
        deps = make_fake_deps(tmp_path, monkeypatch, home)

        # 异常文本只用虚构标记；诊断输出必须丢弃原始异常细节。
        def failed_import(name):
            assert name == "av"
            raise error_type("generated-private-diagnostic")

        monkeypatch.setattr(cli.importlib, "import_module", failed_import)
        assert cli.main(["--home", str(home), "doctor"],
                        deps_factory=lambda: deps) == 0
        text = "\n".join(output)
        assert "✗ PyAV：无法加载音视频解析组件" in text
        assert "generated-private-diagnostic" not in text
        assert "知识库目录可写" in text

    def test_doctor_home_env_isolated(self, tmp_path, monkeypatch, home):
        """doctor 的一切读写都发生在 --home 指定的目录里，不碰真实 APP_HOME。"""
        deps = make_fake_deps(tmp_path, monkeypatch, home)
        cli.main(["--home", str(home), "doctor"], deps_factory=lambda: deps)
        # --home 生效：设置文件写进了临时目录，config 也指向它。
        assert (home / "settings.json").exists()
        assert config.app_home() == home.resolve()


class TestServe:
    def test_serve_dispatches_to_daemon(self, home, monkeypatch):
        # serve 由 daemon.serve 实现：monkeypatch 掉它，测试不真启动服务。
        from xhs_wiki import daemon as daemon_mod

        called = {}
        monkeypatch.setattr(daemon_mod, "serve",
                            lambda port: called.update(port=port))
        assert cli.main(["--home", str(home), "serve", "--port", "18999"]) == 0
        assert called["port"] == 18999
