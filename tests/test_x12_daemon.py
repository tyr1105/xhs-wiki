"""X12 守护进程衔接回归：直接调用真实处理器，所有外部环节用替身。

这里不绑定端口、不启动资源线程或真实引擎；用临时设置文件验证保存
时点、健康检查收尾和引擎选择代次，避免只检查函数调用次数。

v0.4.0 起 LLM Wiki 引擎改为对接 nashsu/llm_wiki 桌面应用，不再有
「下载后台程序」的资源种类，本文件相应只保留引擎选择的衔接行为。
"""

from contextlib import contextmanager
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from xhs_wiki import daemon, engines, runner
from xhs_wiki.ledger import Ledger
from xhs_wiki.store import SKILL_TARGETS, ServiceStore


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """创建完整临时服务上下文；注入进程和 Skill 替身。"""
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("XHS_WIKI_HOME", str(tmp_path / "app"))
    store = ServiceStore(tmp_path / "app")
    kb = tmp_path / "kb"
    kb.mkdir()
    store.update_settings({"kb_root": str(kb)})
    manager = daemon.AssetManager(store,
                                  check=lambda: (
                                      {"xhs_downloader": True,
                                       "asr_model": True}, "whisper"),
                                  install=Mock(), curl=Mock())
    manager.start = Mock()
    spawn = Mock(return_value=SimpleNamespace(poll=lambda: None))
    supervisor = daemon.Supervisor(store, assets=manager, spawn=spawn,
                                   notify=lambda code: None)
    engine = Mock()
    engine.health.return_value = (True, "ok")
    server = SimpleNamespace(supervisor=supervisor,
                             make_engine=Mock(return_value=engine),
                             sync_skill=Mock(), keychain=Mock(),
                             _engine_selection_lock=threading.Lock(),
                             _engine_selection_serial=0,
                             skill_target_roots=lambda: {
                                 target: tmp_path / "tool-skills" / target
                                 for target in SKILL_TARGETS},
                             tool_home=tmp_path / "tool-home",
                             _skill_last_results={})
    handler = object.__new__(daemon.ServiceHandler)
    handler.server = server
    handler._send_json = Mock()
    return SimpleNamespace(store=store, manager=manager,
                           supervisor=supervisor, spawn=spawn,
                           engine=engine, server=server, handler=handler)


def response(setup):
    """读取处理器最后一次响应，保留真实 HTTP 状态码和响应正文断言。"""
    return setup.handler._send_json.call_args.args


def test_asset_manager_no_longer_tracks_llmwiki(setup):
    """资源线程只管下载组件和语音模型；LLM Wiki 不再是资源种类。"""
    assert "llmwiki" not in daemon.AssetManager.KINDS
    setup.manager._prepare()
    assert setup.manager.all_ready()
    assert setup.supervisor.start_run("manual")
    assert setup.spawn.call_count == 1
    assert "llmwiki" not in setup.manager.state()
    # 没有资源管理器时，兜底状态也不包含 llmwiki。
    setup.supervisor.assets = None
    assert "llmwiki" not in setup.supervisor.assets_state()


@pytest.mark.parametrize("healthy,reason", [(True, "ok"),
                                            (False, "llmwiki_model_missing")])
def test_engine_health_closes_and_only_saves_success(setup, healthy, reason):
    """健康检查无论成败都关闭引擎；失败原因原样返回、不保存设置。"""
    setup.engine.health.return_value = healthy, reason
    setup.handler._post_setup_engine({"engine": "llmwiki"})
    code, payload = response(setup)
    setup.engine.close.assert_called_once_with()
    assert setup.store.load_settings()["engine"] == ("llmwiki" if healthy
                                                     else "markdown")
    assert code == (200 if healthy else 400)
    if not healthy:
        assert payload["reason"] == reason


@pytest.mark.parametrize("error", [ValueError("llm_model_invalid"),
                                   RuntimeError("synthetic_unexpected")])
def test_health_exceptions_still_close(setup, error):
    """预期错误和意外异常均进入 finally；意外异常仍向上报告。"""
    setup.engine.health.side_effect = error
    if type(error) is RuntimeError:
        with pytest.raises(RuntimeError, match="synthetic_unexpected"):
            setup.handler._post_setup_engine({"engine": "llmwiki"})
    else:
        setup.handler._post_setup_engine({"engine": "llmwiki"})
        assert response(setup)[1]["reason"] == str(error)
    setup.engine.close.assert_called_once_with()
    assert setup.store.load_settings()["engine"] == "markdown"


@pytest.mark.parametrize("provider", ["claude_code", "anthropic"])
def test_setup_llm_saves_any_provider_for_llmwiki(setup, provider):
    """LLM Wiki 引擎不再限制提供方组合（模型在 LLM Wiki 里自己选）。"""
    body = {"provider": provider, "model": "fake-model"}
    if provider == "anthropic":
        body["base_url"] = None
    setup.store.update_settings({"engine": "llmwiki"})
    setup.handler._post_setup_llm(body)
    assert response(setup)[0] == 200
    assert setup.store.load_settings()["llm"]["provider"] == provider


def test_finish_allows_llmwiki_with_api_provider(setup):
    """完成向导不再按引擎拒绝 API 提供方组合。"""
    setup.store.update_settings({"engine": "llmwiki", "llm": {
        "provider": "anthropic", "model": "fake-model", "base_url": None}})
    setup.handler._post_setup_finish()
    code, payload = response(setup)
    assert code == 400
    assert "llmwiki_provider_unsupported" not in payload
    # 检查继续往后走（llm_key / login 缺项），而不是被组合挡住。
    assert "login" in payload["missing"]


def test_finish_health_is_closed_even_if_login_missing(setup):
    """finish 也会做健康检查，后续检查失败时不能留下未关闭的引擎。"""
    setup.store.update_settings({"engine": "llmwiki"})
    setup.handler._post_setup_finish()
    assert response(setup)[0] == 400
    assert "login" in response(setup)[1]["missing"]
    setup.engine.close.assert_called_once_with()


def test_daemon_and_runner_delegate_to_same_factory(setup, monkeypatch):
    """替换唯一公共入口后两个调用方都到达它，而不是各维护一份分支。"""
    factory = Mock(return_value=setup.engine)
    monkeypatch.setattr(engines, "make_engine", factory)
    settings = setup.store.load_settings()
    settings["engine"] = "llmwiki"
    daemon.ServiceServer._default_make_engine(setup.server, settings)
    factory.assert_called_with(settings, None, setup.store.root)
    factory.reset_mock()
    runner.default_deps().make_engine(settings)
    assert factory.call_count == 1
    assert factory.call_args.args[0] == settings


def test_status_has_no_llmwiki_resource_or_platform_flag(setup):
    """实际组装 status：LLM Wiki 不再是下载资源，也不再报告平台能力。"""
    @contextmanager
    def ledger_scope():
        """只连接临时台账，退出时关闭句柄。"""
        ledger = Ledger(setup.store.root / "ledger.sqlite3")
        try:
            yield ledger
        finally:
            ledger.close()
    setup.server.ledger_scope = ledger_scope
    setup.server.disk_usage = lambda path: SimpleNamespace(free=100, total=200)
    setup.server.cached_kb_stats = lambda path, counts, engine=None: {}
    result = daemon.build_status(setup.server)
    assert "llmwiki_supported" not in result
    assert "llmwiki" not in result["assets"]


def test_slow_health_cannot_overwrite_new_engine_choice(setup):
    """旧引擎检查返回前的新轻量选择拥有保存权，两个引擎都被关闭。"""
    old_engine = setup.engine
    new_engine = Mock()
    new_engine.health.return_value = True, "ok"

    def old_health():
        """在旧检查中插入一个完整的新请求，不靠睡眠制造不稳定竞态。"""
        setup.server.make_engine.return_value = new_engine
        setup.handler._post_setup_engine({"engine": "markdown"})
        return True, "ok"
    old_engine.health.side_effect = old_health
    setup.handler._post_setup_engine({"engine": "llmwiki"})
    assert response(setup) == (409, {"error": "engine_selection_changed"})
    assert setup.store.load_settings()["engine"] == "markdown"
    old_engine.close.assert_called_once_with()
    new_engine.close.assert_called_once_with()
    assert setup.server.sync_skill.call_count == 1
