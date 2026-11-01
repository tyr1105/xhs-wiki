# -*- coding: utf-8 -*-
"""test_config.py —— 路径层 config.py 的单元测试。

全部通过 monkeypatch 把 XHS_WIKI_HOME / XHS_WIKI_LOG_DIR 指到临时目录，
不碰真实的 ~/Library。权限（0700）也在这里验证。
"""

import os
import stat
from pathlib import Path

import pytest

from xhs_wiki import config


@pytest.fixture(autouse=True)
def tmp_home(tmp_path, monkeypatch):
    """每个测试都用独立的临时目录当 APP_HOME，避免互相污染。"""
    home = tmp_path / "app-home"
    monkeypatch.setenv("XHS_WIKI_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def tmp_log(tmp_path, monkeypatch):
    """每个测试都用独立的临时目录当日志目录。"""
    log = tmp_path / "logs"
    monkeypatch.setenv("XHS_WIKI_LOG_DIR", str(log))
    return log


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class TestAppHome:
    def test_env_override_creates_dir(self, tmp_home):
        root = config.app_home()
        assert root == tmp_home
        assert tmp_home.is_dir()

    def test_dir_mode_0700(self, tmp_home):
        config.app_home()
        assert mode_of(tmp_home) == 0o700

    def test_existing_dir_mode_tightened(self, tmp_home):
        # 目录已存在且权限过松（0755）时也要收紧到 0700
        tmp_home.mkdir()
        tmp_home.chmod(0o755)
        config.app_home()
        assert mode_of(tmp_home) == 0o700

    def test_creates_nested_parents(self, tmp_path, monkeypatch):
        deep = tmp_path / "a" / "b" / "c"
        monkeypatch.setenv("XHS_WIKI_HOME", str(deep))
        assert config.app_home() == deep
        assert deep.is_dir()


class TestPaths:
    def test_settings_path(self, tmp_home):
        assert config.settings_path() == tmp_home / "settings.json"

    def test_ledger_path(self, tmp_home):
        assert config.ledger_path() == tmp_home / "ledger.sqlite3"

    def test_browser_profile_dir(self, tmp_home):
        assert config.browser_profile_dir() == tmp_home / "browser-profile"

    def test_vendor_dir(self, tmp_home):
        assert config.vendor_dir() == tmp_home / "vendor"

    def test_models_dir(self, tmp_home):
        assert config.models_dir() == tmp_home / "models"

    def test_work_dir(self, tmp_home):
        note_id = "ab" * 12  # 代码生成的 24 位十六进制假编号
        assert config.work_dir(note_id) == tmp_home / "work" / note_id

    def test_work_dir_rejects_bad_note_id(self):
        for bad in ("../escape", "not-hex!", "", None, "abc"):
            with pytest.raises(ValueError):
                config.work_dir(bad)

    def test_all_paths_share_app_home(self, tmp_home):
        # 所有数据路径都应该落在 APP_HOME 下面
        for p in (
            config.settings_path(),
            config.ledger_path(),
            config.browser_profile_dir(),
            config.vendor_dir(),
            config.models_dir(),
            config.work_dir("cd" * 12),
        ):
            assert tmp_home in p.parents


class TestLogDir:
    def test_env_override_creates_dir(self, tmp_log):
        assert config.log_dir() == tmp_log
        assert tmp_log.is_dir()

    def test_dir_mode_0700(self, tmp_log):
        config.log_dir()
        assert mode_of(tmp_log) == 0o700

    def test_default_is_user_library_logs(self, tmp_path, monkeypatch):
        # 不设环境变量时用 ~/Library/Logs/xhs-wiki；这里用假 HOME 指到临时目录，
        # 避免碰真实的 ~/Library
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.delenv("XHS_WIKI_LOG_DIR", raising=False)
        monkeypatch.setenv("HOME", str(fake_home))
        assert config.log_dir() == fake_home / "Library" / "Logs" / "xhs-wiki"


class TestConstants:
    def test_default_port(self):
        assert config.DEFAULT_PORT == 18765

    def test_launch_agent_label(self):
        assert config.LAUNCH_AGENT_LABEL == "io.github.xhs-wiki.service"
