"""X12 守护进程衔接回归：直接调用真实处理器，所有外部环节用替身。

这里不绑定端口、不启动资源线程或真实引擎；用临时设置文件验证下载
状态、保存时点、健康检查收尾和提供方限制，避免只检查函数调用次数。
"""

from contextlib import contextmanager
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from xhs_wiki import assets, daemon, engines, runner
from xhs_wiki.engines.llmwiki.backend import EngineUnavailable
from xhs_wiki.ledger import Ledger
from xhs_wiki.store import ServiceStore


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """创建完整临时服务上下文；注入网络、进程、平台和 Skill 替身。"""
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("XHS_WIKI_HOME", str(tmp_path / "app"))
    monkeypatch.setattr(assets, "llmwiki_supported", lambda: True)
    monkeypatch.setattr(assets, "llmwiki_ready", lambda home: False)
    monkeypatch.setattr(assets, "llmwiki_binary_path",
                        lambda home: Path(home) / "vendor" / "fake-backend")
    monkeypatch.setattr(assets, "asset_total_bytes", lambda kind: 5_000_000)
    store = ServiceStore(tmp_path / "app")
    kb = tmp_path / "kb"
    kb.mkdir()
    store.update_settings({"kb_root": str(kb)})
    ready = {"xhs_downloader": True, "asr_model": True, "llmwiki": False}
    install = Mock(return_value=store.root / "vendor" / "fake-backend")
    manager = daemon.AssetManager(store, check=lambda: (dict(ready), "whisper"),
                                  install=install, curl=Mock())
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
                             _engine_selection_serial=0)
    handler = object.__new__(daemon.ServiceHandler)
    handler.server = server
    handler._send_json = Mock()
    return SimpleNamespace(store=store, manager=manager, ready=ready,
                           install=install, supervisor=supervisor, spawn=spawn,
                           engine=engine, server=server, handler=handler)


def response(setup):
    """读取处理器最后一次响应，保留真实 HTTP 状态码和响应正文断言。"""
    return setup.handler._send_json.call_args.args


def test_optional_resource_not_needed_and_does_not_block_run(setup):
    """轻量引擎资源就绪后可以运行，不因尚未下载的可选后台而等待。"""
    setup.manager._prepare()
    assert setup.manager.state()["llmwiki"]["state"] == "not_needed"
    assert setup.manager.all_ready()
    assert setup.supervisor.start_run("manual")
    assert setup.spawn.call_count == 1
    setup.install.assert_not_called()


def test_lightweight_default_check_never_hashes_llmwiki(setup, monkeypatch):
    """真实默认检查不触碰未选择的后台文件，避免额外 I/O 和下载。"""
    monkeypatch.setattr(assets, "xhs_downloader_ready", lambda home: True)
    setup.manager._model_ready_cached = lambda name, home: True
    probe = Mock(side_effect=AssertionError("不应检查可选后台"))
    monkeypatch.setattr(assets, "llmwiki_ready", probe)
    result, _ = setup.manager._default_check()
    assert "llmwiki" not in result
    probe.assert_not_called()


def test_selected_engine_requests_resource_without_wizard(setup):
    """已保存 LLM Wiki 的用户重新启动服务时仍会准备该后台。"""
    setup.store.update_settings({"engine": "llmwiki"})
    setup.manager._prepare()
    assert setup.install.call_args.args[:2] == ("llmwiki", None)
    assert setup.store.load_settings()["llmwiki_binary"] == str(
        setup.store.root / "vendor" / "fake-backend")


def test_wizard_request_downloads_and_saves_only_binary(setup):
    """向导请求可绕过当前 markdown 设置启动下载，但不提前保存引擎。"""
    setup.manager.request_llmwiki()
    setup.manager._prepare()
    saved = setup.store.load_settings()
    assert saved["engine"] == "markdown"
    assert saved["llmwiki_binary"] == str(setup.install.return_value)
    assert setup.manager.state()["llmwiki"]["state"] == "ready"


def test_resource_progress_and_manifest_size_are_exposed(setup):
    """安装器的进度原样进入 API 所用快照，大小来自当前清单。"""
    seen = []
    def install(kind, model, progress):
        """在下载尚未完成时采样，防止只验证最终 100%。"""
        progress(0.45)
        seen.append(setup.manager.state()[kind])
        return setup.store.root / "vendor" / "fake-backend"
    setup.manager._install = install
    setup.manager.request_llmwiki()
    setup.manager._prepare()
    assert seen[0]["progress"] == 0.45
    assert seen[0]["total_bytes"] == 5_000_000
    assert seen[0]["state"] == "downloading"


def test_failed_download_retries_only_after_request(setup):
    """失败不会每轮重下；用户再次点击后才允许一次重试。"""
    setup.install.side_effect = ValueError("assets_sha256_mismatch")
    setup.manager.request_llmwiki()
    setup.manager._prepare()
    setup.manager._prepare()
    assert setup.install.call_count == 1
    assert setup.manager.state()["llmwiki"]["error"] == "assets_sha256_mismatch"
    assert setup.store.load_settings()["llmwiki_binary"] is None
    setup.install.side_effect = None
    setup.manager.request_llmwiki()
    setup.manager._prepare()
    assert setup.install.call_count == 2
    assert setup.manager.all_ready()


def test_switching_back_during_failed_download_does_not_block(setup):
    """正在下载时切回轻量，即使随后安装失败也不重新成为运行依赖。"""
    def fail_after_switch(*args):
        """模拟用户操作与后台下载完成交错的时序。"""
        setup.manager.release_llmwiki_request()
        raise ValueError("assets_network_error")
    setup.install.side_effect = fail_after_switch
    setup.manager.request_llmwiki()
    setup.manager._prepare()
    assert setup.manager.state()["llmwiki"]["state"] == "not_needed"
    assert setup.manager.all_ready()


def test_deleted_ready_binary_is_installed_again(setup):
    """已就绪的文件随后缺失不能沿用旧 ready 状态短路。"""
    setup.store.update_settings({"engine": "llmwiki"})
    setup.ready["llmwiki"] = True
    setup.manager._prepare()
    setup.ready["llmwiki"] = False
    setup.manager._prepare()
    assert setup.install.call_count == 1


def test_download_error_does_not_expose_arbitrary_message(setup):
    """下载器意外携带地址或内部路径时，公开状态只保留稳定错误码。"""
    setup.install.side_effect = ValueError("失败 https://example.test/private?value=synthetic")
    setup.manager.request_llmwiki()
    setup.manager._prepare()
    assert setup.manager.state()["llmwiki"]["error"] == "assets_error"


def test_setup_engine_unsupported_platform_is_400(setup, monkeypatch):
    """平台拒绝先于下载和健康检查，设置保持原值。"""
    monkeypatch.setattr(assets, "llmwiki_supported", lambda: False)
    setup.handler._post_setup_engine({"engine": "llmwiki"})
    code, payload = response(setup)
    assert code == 400
    assert payload["reason"] == "llmwiki_unsupported_platform"
    assert not setup.manager._llmwiki_requested
    setup.server.make_engine.assert_not_called()


def test_setup_engine_download_returns_202_without_save(setup):
    """202 只表示已经请求资源，不表示引擎配置成功。"""
    setup.handler._post_setup_engine({"engine": "llmwiki"})
    code, payload = response(setup)
    assert code == 202 and payload["state"] == "downloading"
    assert payload["progress"]["state"] == "downloading"
    assert setup.manager._llmwiki_requested
    assert setup.store.load_settings()["engine"] == "markdown"
    setup.engine.health.assert_not_called()


@pytest.mark.parametrize("healthy,reason", [(True, "ok"),
                                            (False, "llmwiki_desktop_running")])
def test_ready_engine_health_always_closes_and_only_saves_success(
        setup, monkeypatch, healthy, reason):
    """健康和桌面版占用两条路径都关闭引擎；失败原因必须原样返回。"""
    monkeypatch.setattr(assets, "llmwiki_ready", lambda home: True)
    setup.engine.health.return_value = healthy, reason
    setup.handler._post_setup_engine({"engine": "llmwiki"})
    code, payload = response(setup)
    setup.engine.close.assert_called_once_with()
    assert setup.store.load_settings()["engine"] == ("llmwiki" if healthy else "markdown")
    assert code == (200 if healthy else 400)
    if not healthy:
        assert payload["reason"] == reason
        assert setup.store.load_settings()["llmwiki_binary"] is None


@pytest.mark.parametrize("error", [ValueError("llm_model_invalid"),
                                    EngineUnavailable("llmwiki_agent_missing"),
                                    RuntimeError("synthetic_unexpected")])
def test_health_exceptions_still_close(setup, monkeypatch, error):
    """预期错误和意外异常均进入 finally；意外异常仍向上报告。"""
    monkeypatch.setattr(assets, "llmwiki_ready", lambda home: True)
    setup.engine.health.side_effect = error
    if type(error) is RuntimeError:
        with pytest.raises(RuntimeError, match="synthetic_unexpected"):
            setup.handler._post_setup_engine({"engine": "llmwiki"})
    else:
        setup.handler._post_setup_engine({"engine": "llmwiki"})
        assert response(setup)[1]["reason"] == str(error)
    setup.engine.close.assert_called_once_with()
    assert setup.store.load_settings()["engine"] == "markdown"


@pytest.mark.parametrize("healthy", [True, False])
def test_api_provider_switch_is_saved_only_after_health(setup, monkeypatch, healthy):
    """从 API 引擎切换时用 Claude 候选检查，失败不覆盖原模型配置。"""
    previous = {"provider": "anthropic", "model": "fake-api-model", "base_url": None}
    setup.store.update_settings({"llm": previous})
    monkeypatch.setattr(assets, "llmwiki_ready", lambda home: True)
    setup.engine.health.return_value = healthy, "llmwiki_agent_missing"
    setup.handler._post_setup_engine({"engine": "llmwiki"})
    candidate = setup.server.make_engine.call_args.args[0]
    assert candidate["llm"]["provider"] == "claude_code"
    saved = setup.store.load_settings()["llm"]
    assert saved == (daemon.DEFAULT_SETTINGS["llm"] if healthy else previous)


def test_selecting_markdown_releases_pending_download(setup):
    """向导切回轻量后解除可选依赖，后续运行只等必需资源。"""
    setup.manager.request_llmwiki()
    setup.handler._post_setup_engine({"engine": "markdown"})
    assert response(setup)[0] == 200
    assert not setup.manager._llmwiki_requested
    assert setup.manager.state()["llmwiki"]["state"] == "not_needed"


@pytest.mark.parametrize("provider", ["anthropic", "openai_compatible"])
def test_setup_llm_rejects_api_for_llmwiki(setup, provider):
    """绕过页面直接调用接口也不能保存不支持的组合。"""
    setup.store.update_settings({"engine": "llmwiki"})
    setup.handler._post_setup_llm({"provider": provider, "model": "fake-model"})
    assert response(setup) == (400, {"error": "llmwiki_provider_unsupported"})
    assert setup.store.load_settings()["llm"]["provider"] == "claude_code"


@pytest.mark.parametrize("provider", ["claude_code", "codex"])
def test_setup_llm_accepts_supported_provider(setup, provider):
    """两种受支持提供方都可由真实设置校验通过。"""
    setup.store.update_settings({"engine": "llmwiki"})
    setup.handler._post_setup_llm({"provider": provider, "model": "fake-model"})
    assert response(setup)[0] == 200
    assert setup.store.load_settings()["llm"]["provider"] == provider


@pytest.mark.parametrize("provider", ["anthropic", "openai_compatible"])
def test_finish_checks_combination_before_keychain_or_backend(setup, provider):
    """完成向导二次校验历史组合；拒绝时不访问密钥或启动后台。"""
    setup.store.update_settings({"engine": "llmwiki", "llm": {
        "provider": provider, "model": "fake-model",
        "base_url": "https://api.example.test/v1" if provider == "openai_compatible" else None}})
    setup.handler._post_setup_finish()
    assert response(setup) == (400, {"error": "llmwiki_provider_unsupported"})
    setup.server.make_engine.assert_not_called()
    setup.server.keychain.assert_not_called()
    assert not setup.store.load_settings()["setup_completed"]


def test_finish_health_is_closed_even_if_login_missing(setup):
    """finish 也会做健康检查，后续检查失败时不能留下后台。"""
    setup.store.update_settings({"engine": "llmwiki"})
    setup.handler._post_setup_finish()
    assert response(setup)[0] == 400
    assert "login" in response(setup)[1]["missing"]
    setup.engine.close.assert_called_once_with()


def test_default_install_wires_controller_and_returns_path(setup, monkeypatch):
    """第三类资源复用原 curl 生命周期控制器，禁止另开下载通路。"""
    install = Mock(return_value=setup.install.return_value)
    monkeypatch.setattr(assets, "install_llmwiki", install)
    progress = Mock()
    assert setup.manager._default_install("llmwiki", None, progress) == install.return_value
    install.assert_called_once_with(setup.store.root, progress=progress,
                                    downloader=setup.manager._curl.download)


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


def test_status_contains_optional_resource_and_platform(setup, monkeypatch):
    """实际组装 status，确认页面拿到资源快照与平台能力而不依赖端口。"""
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
    assert result["llmwiki_supported"] is True
    assert result["assets"]["llmwiki"]["state"] == "not_needed"
    assert result["assets"]["llmwiki"]["total_bytes"] == 5_000_000


def test_no_manager_status_still_marks_llmwiki_not_needed(setup):
    """兼容旧嵌入方式也不能把未安装后台报告成可用。"""
    setup.supervisor.assets = None
    assert setup.supervisor.assets_state()["llmwiki"]["state"] == "not_needed"


def test_release_between_check_and_download_keeps_not_needed(setup):
    """旧检查快照与切换交错时，下载前的复核必须修复状态。"""
    setup.manager.request_llmwiki()
    original = setup.manager._llmwiki_needed
    def interrupted_check():
        """仅第一次返回旧快照，模拟用户恰在读取后释放请求。"""
        previous = original()
        setup.manager.release_llmwiki_request()
        setup.manager._llmwiki_needed = original
        return previous
    setup.manager._llmwiki_needed = interrupted_check
    setup.manager._prepare()
    setup.install.assert_not_called()
    assert setup.manager.state()["llmwiki"]["state"] == "not_needed"
    assert setup.manager.all_ready()


def test_slow_health_cannot_overwrite_new_engine_choice(setup, monkeypatch):
    """旧 LLM Wiki 检查返回前的新轻量选择拥有保存权，旧后台仍需关闭。"""
    monkeypatch.setattr(assets, "llmwiki_ready", lambda home: True)
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
