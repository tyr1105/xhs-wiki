# -*- coding: utf-8 -*-
"""test_y4_console.py —— 控制台日常页与后台镜像配置的回归测试。

这个文件检查日常页回填的接口地址会被保存表单复用，以及安装进程的
镜像设置能进入后台服务的 plist。前端采用项目已有的静态检查方式，
不启动浏览器或 HTTP 服务；plist 只在内存中生成，不写入服务目录。
"""

import plistlib
from pathlib import Path

import pytest

from xhs_wiki import cli


@pytest.fixture
def console_html():
    """读取发布所用页面，防止测试自己的副本与实际交付文件脱节。"""
    return (Path(cli.__file__).parent / "web" / "console.html").read_text(
        encoding="utf-8")


def test_daily_form_preserves_saved_base_url(console_html):
    """分别定位渲染和提交逻辑，锁住同一数据属性的写入与读取。"""
    render = console_html.split("function render(data){", 1)[1].split(
        "function updateEngineKeyTip()", 1)[0]
    submit = console_html.split(
        "$('engine-form').addEventListener('submit'", 1)[1].split(
            "$('asr-form').addEventListener", 1)[0]
    assert "const llm=(s.llm)||data.llm||{};" in render
    assert "$('e-model').dataset.base_url=llm.base_url||'';" in render
    assert "base_url:provider==='openai_compatible'?" in submit
    assert "($('e-model').dataset.base_url||null):null" in submit
    assert "post('/api/setup/llm'" in submit


def test_audio_error_does_not_require_external_ffmpeg(console_html):
    """解码失败引导检查视频本身，不再引导安装已移除的外部依赖。"""
    message = console_html.split("audio_extract_failed:'", 1)[1].split("'", 1)[0]
    assert message == "从视频提取声音失败（视频文件可能已损坏）"
    assert "ffmpeg" not in message.lower()


def test_print_launch_agent_keeps_mirror_variables(tmp_path, monkeypatch, capsys):
    """实际执行打印入口并解析 plist，确认后台获得全部镜像变量。"""
    values = {
        "UV_DEFAULT_INDEX": "https://packages.example.test/simple/",
        "UV_INDEX_URL": "https://legacy-index.example.test/simple/",
        "UV_PYTHON_INSTALL_MIRROR": "https://python.example.test/releases",
        "HF_ENDPOINT": "https://models.example.test",
        "https_proxy": "http://127.0.0.1:7890",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XHS_WIKI_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("XHS_WIKI_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("XHS_WIKI_SKILLS_ROOT", str(tmp_path / "skills"))
    assert cli.main(["print-launch-agent"]) == 0
    plist = plistlib.loads(capsys.readouterr().out.encode("utf-8"))
    for name, value in values.items():
        assert plist["EnvironmentVariables"][name] == value
    assert not (tmp_path / "home").exists()


@pytest.mark.parametrize("value", [None, "", "   ", "generated\nvalue"])
def test_launch_agent_omits_empty_or_invalid_mirror_values(value):
    """沿用代理变量的筛选规则，空值和不能写入 XML 的值都不落盘。"""
    names = ("UV_DEFAULT_INDEX", "UV_INDEX_URL", "UV_PYTHON_INSTALL_MIRROR")
    environ = {} if value is None else {name: value for name in names}
    assert cli.launch_agent_network_env(environ) == {}
