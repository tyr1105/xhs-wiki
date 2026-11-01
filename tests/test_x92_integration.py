# -*- coding: utf-8 -*-
"""test_x92_integration.py —— 子进程参数、真实子进程集成和向导细节的测试。

覆盖四块：
1. cli 的 --home 写在子命令前后都能用（原来只认前面的，导致守护进程
   拉起的子进程以退出码 2 失败——真 bug）；
2. **真实子进程**集成测试：不用假的 spawn，真的用 subprocess 跑
   `python -m xhs_wiki.cli run …`。因为设置未完成，预检阶段就会停下
   （setup_incomplete），所以不访问网络、不调用模型、不启动浏览器；
3. runner.default_deps 的 _build_package 把设置里的语音模型和模型目录
   真正传给 build_package（原来两个参数都没传）；
4. 向导第 3 步的模型名预填值写进了 console.html。

红线自查：所有测试只用临时目录（XHS_WIKI_HOME / XHS_WIKI_LOG_DIR 都
指到 tmp_path），绝不在真实主目录或 ~/Library 下创建任何东西。
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from xhs_wiki import cli, config, daemon, runner
from xhs_wiki.ledger import Ledger
from xhs_wiki.store import ServiceStore


# ---------------------------------------------------------------------------
# 1. cli：--home 写在子命令前后都能用
# ---------------------------------------------------------------------------


class TestHomeArgumentPosition:
    """parser 层面：同名参数合并（SUPPRESS）之后三种写法都合法。"""

    def test_home_before_subcommand(self):
        args = cli._build_parser().parse_args(
            ["--home", "/tmp/位置A", "run", "--trigger", "manual"])
        assert args.home == "/tmp/位置A"

    def test_home_after_subcommand(self):
        """核心场景：守护进程拼的命令把 --home 写在子命令后面。"""
        args = cli._build_parser().parse_args(
            ["run", "--trigger", "manual", "--home", "/tmp/位置B"])
        assert args.home == "/tmp/位置B"
        assert args.trigger == "manual"

    def test_home_after_subcommand_on_every_subcommand(self):
        """每个子命令都认后置 --home（login/status/cleanup/doctor/serve/
        print-launch-agent——定时、扫码、状态全都靠它）。"""
        parser = cli._build_parser()
        for argv in (["login", "--home", "/tmp/位置C"],
                     ["status", "--home", "/tmp/位置C"],
                     ["cleanup", "--dry-run", "--home", "/tmp/位置C"],
                     ["doctor", "--home", "/tmp/位置C"],
                     ["serve", "--port", "1", "--home", "/tmp/位置C"],
                     ["print-launch-agent", "--home", "/tmp/位置C"]):
            assert parser.parse_args(argv).home == "/tmp/位置C", argv[0]

    def test_home_both_positions_latter_wins(self):
        """两个位置都写时，后写的（子命令后面的）生效。"""
        args = cli._build_parser().parse_args(
            ["--home", "/tmp/位置A", "run", "--home", "/tmp/位置B"])
        assert args.home == "/tmp/位置B"

    def test_home_omitted_is_none(self):
        args = cli._build_parser().parse_args(["status"])
        assert args.home is None

    def test_status_with_home_after_subcommand(self, tmp_path, monkeypatch):
        """走一遍 main：后置 --home 真的把 APP_HOME 指到了临时目录。"""
        monkeypatch.delenv("XHS_WIKI_HOME", raising=False)
        home = tmp_path / "后置-home"
        lines = []
        monkeypatch.setattr(cli, "_print", lines.append)
        assert cli.main(["status", "--home", str(home)]) == 0
        assert config.app_home() == home.resolve()
        # 目录由 config.app_home 创建在临时位置（而不是真实主目录）。
        assert home.is_dir()
        assert any("还没有收藏记录" in line for line in lines)


# ---------------------------------------------------------------------------
# 2. daemon：拼命令时 --home 放在子命令前面（双保险）
# ---------------------------------------------------------------------------


class TestChildArgsOrder:
    def _supervisor(self, home):
        return daemon.Supervisor(ServiceStore(home))

    def test_run_child_args_home_before_run(self, tmp_path):
        args = self._supervisor(tmp_path).run_child_args("manual")
        assert args[args.index("--home") + 1] == str(tmp_path)
        # --home 出现在子命令 run 之前（统一放前面）。
        assert args.index("--home") < args.index("run")
        assert args[args.index("--trigger") + 1] == "manual"

    def test_login_child_args_home_before_login(self, tmp_path):
        args = self._supervisor(tmp_path).login_child_args()
        assert args.index("--home") < args.index("login")
        assert args[args.index("--home") + 1] == str(tmp_path)


# ---------------------------------------------------------------------------
# 3. 真实子进程测试（禁止用假 spawn）
# ---------------------------------------------------------------------------

# 子进程最长等 120 秒：真实解释器冷启动 + 建目录，正常只要几秒。
SUBPROCESS_TIMEOUT = 120


def _child_env(home: Path, log_dir: Path) -> dict:
    """子进程的环境变量：继承当前环境，但两个目录变量钉死在临时目录里。

    XHS_WIKI_LOG_DIR 必须设置——子进程和守护进程的日志都写到它，绝不
    能落到真实的 ~/Library/Logs（红线）。
    """
    env = dict(os.environ)
    env["XHS_WIKI_HOME"] = str(home)
    env["XHS_WIKI_LOG_DIR"] = str(log_dir)
    return env


def test_real_run_subprocess_stops_at_setup(tmp_path):
    """真实跑一次 `python -m xhs_wiki.cli run --trigger manual --home <tmp>`。

    设置未完成 → 预检停下（needs_user / setup_incomplete），退出码 0，
    runs.jsonl 里恰好一条记录。修复前这一命令会以退出码 2 结束
    （unrecognized arguments: --home）。
    """
    home = tmp_path / "app"
    log_dir = tmp_path / "logs"
    proc = subprocess.run(
        [sys.executable, "-m", "xhs_wiki.cli",
         "run", "--trigger", "manual", "--home", str(home)],
        env=_child_env(home, log_dir),
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT)
    assert proc.returncode == 0, f"子进程输出：\n{proc.stdout}\n{proc.stderr}"
    lines = (home / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["result"] == "needs_user"
    assert record["stop_reason"] == "setup_incomplete"
    assert record["trigger"] == "manual"


def test_real_service_run_now_spawns_real_child(tmp_path, monkeypatch):
    """真实 ServiceServer（端口 0、默认真实 spawn）+ POST /api/run-now。

    等子进程结束（最多 30 秒），断言 runs.jsonl 出现本次运行记录，且
    不是 runner_crashed（修复前子进程因参数错误退出码 2，没写记录，
    会被补一条 runner_crashed）。
    """
    home = tmp_path / "app"
    log_dir = tmp_path / "logs"
    # 守护进程（本进程）和它拉起的子进程都只用临时目录。
    monkeypatch.setenv("XHS_WIKI_HOME", str(home))
    monkeypatch.setenv("XHS_WIKI_LOG_DIR", str(log_dir))

    store = ServiceStore(home)
    # 不注入 spawn：用 Supervisor 默认的真实子进程实现。
    supervisor = daemon.Supervisor(store)
    server = daemon.ServiceServer(
        ("127.0.0.1", 0), supervisor=supervisor,
        ledger_factory=lambda: Ledger(home / "ledger.sqlite3"))
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # 先经真实 /auth 用访问令牌换 Cookie。
        from test_daemon import server_cookie
        cookie = server_cookie(server)
        # 带合法令牌 POST /api/run-now（五道校验全过才执行）。
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/api/run-now", body="{}", headers={
            "Host": f"127.0.0.1:{port}",
            "Origin": f"http://127.0.0.1:{port}",
            "Cookie": cookie,
            "X-CSRF-Token": server.csrf_token,
            "Content-Type": "application/json",
        })
        response = conn.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        conn.close()
        assert response.status == 200
        assert body.get("ok") is True

        # 轮询等子进程写运行记录（最多 30 秒）。
        runs_path = home / "runs.jsonl"
        record = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if runs_path.exists():
                lines = runs_path.read_text(encoding="utf-8").splitlines()
                if lines:
                    record = json.loads(lines[-1])
                    break
            time.sleep(0.2)
        assert record is not None, "30 秒内 runs.jsonl 没有出现运行记录"
        assert record["stop_reason"] != "runner_crashed"
        assert record["result"] == "needs_user"
        assert record["stop_reason"] == "setup_incomplete"
        # 子进程日志写进了 XHS_WIKI_LOG_DIR 指定的临时目录。
        assert (log_dir / "daemon.log").exists()
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# 4. runner.default_deps：解析环节用上设置里的语音模型
# ---------------------------------------------------------------------------


def test_default_deps_build_package_passes_models(tmp_path, monkeypatch):
    """_build_package 要把 models_dir=config.models_dir() 和设置里的
    asr_model 传给 parse.package 的 build_package（原来都没传，设置不生效）。"""
    monkeypatch.setenv("XHS_WIKI_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("XHS_WIKI_LOG_DIR", str(tmp_path / "logs"))

    from xhs_wiki.parse import package as package_module
    captured = {}

    def fake_build(note_id, dest_dir, download_result, *,
                   asr=None, ocr=None, models_dir=None, asr_model="whisper"):
        captured["models_dir"] = models_dir
        captured["asr_model"] = asr_model
        return {"package_sha256": "0" * 64, "note_id": note_id}

    monkeypatch.setattr(package_module, "build_package", fake_build)

    store = ServiceStore(config.app_home())
    # 默认值就是 sensevoice；这里故意改成另一个，证明读的是设置而不是写死。
    store.update_settings({"asr_model": "whisper"})

    deps = runner.default_deps()
    outcome = deps.build_package("a" * 24, tmp_path / "work",
                                 {"status": "ok"})
    assert captured["models_dir"] == config.models_dir()
    assert captured["asr_model"] == "whisper"
    # 返回值按 RunnerDeps 的约定包了一层。
    assert outcome == {"package": {"package_sha256": "0" * 64,
                                   "note_id": "a" * 24},
                       "package_sha256": "0" * 64}


def test_default_deps_build_package_follows_settings_change(
        tmp_path, monkeypatch):
    """改了设置之后，下一次解析就用新的语音模型（设置是每次现读的）。"""
    monkeypatch.setenv("XHS_WIKI_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("XHS_WIKI_LOG_DIR", str(tmp_path / "logs"))

    from xhs_wiki.parse import package as package_module
    seen = []

    def fake_build(note_id, dest_dir, download_result, *,
                   asr=None, ocr=None, models_dir=None, asr_model="whisper"):
        seen.append(asr_model)
        return {"package_sha256": "0" * 64}

    monkeypatch.setattr(package_module, "build_package", fake_build)
    store = ServiceStore(config.app_home())
    deps = runner.default_deps()
    deps.build_package("b" * 24, tmp_path, {"status": "ok"})
    store.update_settings({"asr_model": "whisper"})
    deps.build_package("b" * 24, tmp_path, {"status": "ok"})
    # 默认 asr_model 已从 sensevoice 改成 whisper（与安装脚本只下载
    # whisper 的做法一致），第一次调用用的是当前默认值，不再写死旧默认。
    from xhs_wiki import store as store_module
    assert seen == [store_module.DEFAULT_SETTINGS["asr_model"], "whisper"]


# ---------------------------------------------------------------------------
# 5. 向导第 3 步：模型名预填建议值（console.html 内容检查）
# ---------------------------------------------------------------------------


def test_console_html_prefills_model_defaults():
    """向导第 3 步：claude_code/anthropic 预填 claude-haiku-5-5；codex 和
    openai_compatible 留空，提示分别写明"默认模型"和"填服务商模型名"。"""
    html = (Path(runner.__file__).resolve().parent
            / "web" / "console.html").read_text(encoding="utf-8")
    assert "MODEL_DEFAULTS" in html
    # 建议值直接出现在预填表里（不再只写在说明文字里）。
    assert "claude_code:'claude-haiku-5-5'" in html.replace(" ", "")
    assert "anthropic:'claude-haiku-5-5'" in html.replace(" ", "")
    assert "留空 = 使用 Codex 的默认模型" in html
    assert "填写服务商的模型名" in html
