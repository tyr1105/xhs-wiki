# -*- coding: utf-8 -*-
"""下载组件与模型资源的回归测试：完成标记、安装失败清理、Whisper 换源与断点续传。

这些测试位于安装资源层；用生成的压缩包和下载替身执行真实校验逻辑，
不访问网络、不启动 uv 或 curl，全部目录位于 pytest 的临时目录中。
"""

import hashlib
import stat
import subprocess
import uuid

import pytest

from xhs_wiki import assets
from test_assets import FakeHTTP, fake_manifest, make_downloader_archive


@pytest.fixture(autouse=True)
def isolated_asset_environment(tmp_path, monkeypatch):
    """每条测试隔离所有运行目录，并去掉外部镜像设置以确保请求顺序确定。"""
    monkeypatch.setenv("XHS_WIKI_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("XHS_WIKI_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("XHS_WIKI_SKILLS_ROOT", str(tmp_path / "skills"))
    monkeypatch.delenv("HF_ENDPOINT", raising=False)


@pytest.fixture
def downloader_install(tmp_path, monkeypatch):
    """生成可解压的小组件，替换清单和网络，同时保留真实安装步骤。"""
    manifest = fake_manifest(tmp_path)
    archive = make_downloader_archive()
    entry = manifest["xhs_downloader"]
    entry["sha256"] = hashlib.sha256(archive).hexdigest()
    http = FakeHTTP()
    http.serve(entry["url"], archive)
    monkeypatch.setattr(assets, "load_manifest", lambda: manifest)
    home = tmp_path / "app"
    return home, http, home / "vendor" / entry["extract_dir"]


def test_downloader_marker_written_only_after_dependencies(downloader_install):
    """假 uv 运行期间不能就绪；成功后标记为版本号且权限仅限当前用户。"""
    home, http, component = downloader_install
    marker = component.parent / ".xhs-downloader-ready"
    calls = []

    def install_dependencies(args):
        """记录命令，并在依赖尚未完成的时刻检查就绪状态。"""
        calls.append(args)
        assert component.is_dir()
        assert not marker.exists()
        assert not assets.xhs_downloader_ready(home)

    assert assets.install_xhs_downloader(home, http=http, uv=install_dependencies) == component
    # 标记第 1 行是版本号，第 2 行是装依赖时 venv 的身份。
    lines = marker.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "1.0"
    assert lines[1] == assets._venv_identity(home)
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert stat.S_IMODE(marker.parent.stat().st_mode) == 0o700
    assert assets.xhs_downloader_ready(home)
    assert len(calls) == 1
    assert calls[0][calls[0].index("--only-binary") + 1] == (
        "av,ctranslate2,onnxruntime,numpy,tokenizers,sherpa-onnx,cryptography,"
        "lxml,curl-cffi,pydantic-core,pyobjc-core"
    )
    assert not list(marker.parent.glob(".xhs-downloader-ready-*"))


@pytest.mark.parametrize("contents", [None, "0.9", "", "1.0-extra"])
def test_downloader_ready_requires_matching_marker(downloader_install, contents):
    """只有目录或旧版标记都不能通过复查，防止后台提前开始处理笔记。"""
    home, _http, component = downloader_install
    component.mkdir(parents=True)
    if contents is not None:
        (component.parent / ".xhs-downloader-ready").write_text(contents, encoding="utf-8")
    assert not assets.xhs_downloader_ready(home)


@pytest.mark.parametrize("failure", [ValueError("assets_uv_failed"),
                                     FileNotFoundError("generated uv failure"),
                                     subprocess.TimeoutExpired(["uv"], 1)])
def test_downloader_dependency_failure_cleans_and_retry_recovers(downloader_install, failure):
    """各种依赖失败都清掉半成品与旧标记，随后重新安装仍能成功。"""
    home, http, component = downloader_install
    component.mkdir(parents=True)
    marker = component.parent / ".xhs-downloader-ready"
    marker.write_text("1.0", encoding="utf-8")

    def fail_dependencies(_args):
        """在已解压后失败，覆盖旧代码最容易误判就绪的时间窗口。"""
        assert component.is_dir()
        raise failure

    with pytest.raises(type(failure)):
        assets.install_xhs_downloader(home, http=http, uv=fail_dependencies)
    assert not component.exists()
    assert not marker.exists()
    assert not assets.xhs_downloader_ready(home)
    assert not assets.xhs_downloader_ready(home)
    assets.install_xhs_downloader(home, http=http, uv=lambda _args: None)
    assert assets.xhs_downloader_ready(home)


@pytest.mark.parametrize("failure", [FileNotFoundError("generated uv failure"),
                                     subprocess.TimeoutExpired(["uv"], 1)])
def test_default_uv_maps_start_and_timeout_errors(monkeypatch, failure):
    """实际执行器启动失败或超时统一给错误码，不能泄露底层命令输出。"""
    def fake_run(*_args, **_kwargs):
        """替代子进程入口，确保本测试不会真的执行安装工具。"""
        raise failure

    monkeypatch.setattr(assets.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="^assets_uv_failed$"):
        assets._default_uv(["pip", "install"])


def _whisper_fixture(tmp_path, monkeypatch):
    """模型数据由代码生成，清单大小和摘要对应这些字节，便于真实验签。"""
    manifest = fake_manifest(tmp_path)
    monkeypatch.setattr(assets, "load_manifest", lambda: manifest)
    return manifest["whisper"]


def test_whisper_endpoint_order_and_resume_preserve_repository_path(tmp_path, monkeypatch):
    """自定义站点→官方→镜像：换源继续使用同一 .part，每个文件都验摘要。"""
    entry = _whisper_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("HF_ENDPOINT", "https://mirror.example.invalid/")
    requested = []
    parts = {}

    def download(url, temporary_path, *, on_bytes=None):
        """第一站写一部分后断开、第二站连接失败、第三站从断点接着写。"""
        requested.append(url)
        filename = url.rsplit("/", 1)[-1]
        payload = entry["_bytes"][filename]
        if url.startswith("https://mirror.example.invalid/"):
            parts[filename] = temporary_path
            temporary_path.write_bytes(payload[:5])
            raise ValueError("assets_network_error")
        assert temporary_path == parts[filename]
        assert temporary_path.read_bytes() == payload[:5]
        if url.startswith("https://huggingface.co/"):
            raise ValueError("assets_network_error")
        assert url.startswith("https://hf-mirror.com/")
        with temporary_path.open("ab") as output:
            output.write(payload[5:])
        if on_bytes is not None:
            on_bytes(len(payload))

    directory = assets.install_model("whisper", tmp_path / "app", downloader=download)
    expected = []
    for filename, payload in entry["_bytes"].items():
        for endpoint in ("https://mirror.example.invalid", "https://huggingface.co", "https://hf-mirror.com"):
            expected.append(endpoint + "/generated/model/resolve/fake-revision/" + filename)
        assert (directory / filename).read_bytes() == payload
        assert not (directory / ("." + filename + ".part")).exists()
    assert requested == expected
    assert assets.verify_model("whisper", tmp_path / "app") == (True, [])


@pytest.mark.parametrize("endpoint", [None, "https://huggingface.co/"])
def test_whisper_official_then_mirror_without_duplicate_requests(tmp_path, monkeypatch, endpoint):
    """没有自定义地址时从官方开始；自定义恰好等于官方时同一地址只试一次。"""
    entry = _whisper_fixture(tmp_path, monkeypatch)
    if endpoint is not None:
        monkeypatch.setenv("HF_ENDPOINT", endpoint)
    seen = []

    def download(url, temporary_path, *, on_bytes=None):
        """官方不可用时由镜像交出清单对应的数据，全过程不访问网络。"""
        seen.append(url)
        if url.startswith("https://huggingface.co/"):
            raise ValueError("assets_network_error")
        temporary_path.write_bytes(entry["_bytes"][url.rsplit("/", 1)[-1]])

    assets.install_model("whisper", tmp_path / "app", downloader=download)
    assert len(seen) == 2 * len(entry["files"])
    assert all(url.startswith("https://huggingface.co/") for url in seen[::2])
    assert all(url.startswith("https://hf-mirror.com/") for url in seen[1::2])


def test_whisper_all_endpoints_fail_keeps_partial_file(tmp_path, monkeypatch):
    """所有站点断开后仍保存断点，让后续用户重试接着下载。"""
    _whisper_fixture(tmp_path, monkeypatch)
    paths = []

    def download(_url, temporary_path, *, on_bytes=None):
        """模拟连到各站点都只拿到最初几字节。"""
        paths.append(temporary_path)
        if not temporary_path.exists():
            temporary_path.write_bytes(b"part")
        raise ValueError("assets_network_error")

    with pytest.raises(ValueError, match="^assets_network_error$"):
        assets.install_model("whisper", tmp_path / "app", downloader=download)
    assert len(paths) == 2
    assert paths[0] == paths[1]
    assert paths[0].read_bytes() == b"part"


def test_whisper_mirror_sha_mismatch_stops_and_removes_partial_file(tmp_path, monkeypatch):
    """镜像返回同长度但不同内容也必须失败，不能把未验证数据当成模型。"""
    entry = _whisper_fixture(tmp_path, monkeypatch)
    seen = []
    paths = []

    def download(url, temporary_path, *, on_bytes=None):
        """官方断开后镜像交出伪造数据，由真实 sha256 核对拒绝。"""
        seen.append(url)
        paths.append(temporary_path)
        if url.startswith("https://huggingface.co/"):
            raise ValueError("assets_network_error")
        payload = entry["_bytes"][url.rsplit("/", 1)[-1]]
        temporary_path.write_bytes(bytes(value ^ 0xFF for value in payload))

    with pytest.raises(ValueError, match="^assets_sha256_mismatch$"):
        assets.install_model("whisper", tmp_path / "app", downloader=download)
    # 官方断开后，镜像校验失败只允许在原站完整重下一次。
    assert len(seen) == 3
    assert seen[1] == seen[2]
    assert not paths[-1].exists()


@pytest.mark.parametrize("invalid_kind", ["scheme", "host", "malformed", "no_scheme"])
def test_whisper_invalid_endpoint_is_skipped_without_leaking(tmp_path, monkeypatch, caplog,
                                                            invalid_kind):
    """写错的 HF_ENDPOINT 被跳过，继续试官方站和镜像。

    以前直接报网络错误、一个请求都不发，后台每一轮都失败，语音模型永远
    装不上。现在跳过它，只记一条固定的错误代码日志；日志、请求地址里都
    不能出现地址原文（可能带私有参数）。
    """
    entry = _whisper_fixture(tmp_path, monkeypatch)
    private_text = "generated-" + uuid.uuid4().hex
    endpoint = {
        "scheme": "ftp://" + private_text + "@example.invalid",
        "host": "https:///" + private_text,
        "malformed": "https://[" + private_text,
        "no_scheme": private_text + ".example.invalid",
    }[invalid_kind]
    monkeypatch.setenv("HF_ENDPOINT", endpoint)
    seen = []

    def download(url, temporary_path, *, on_bytes=None):
        """官方站不可用、镜像可用；全程不访问网络。"""
        seen.append(url)
        if url.startswith("https://huggingface.co/"):
            raise ValueError("assets_network_error")
        temporary_path.write_bytes(entry["_bytes"][url.rsplit("/", 1)[-1]])

    with caplog.at_level("WARNING", logger="xhs_wiki.assets"):
        directory = assets.install_model("whisper", tmp_path / "app", downloader=download)
    assert assets.verify_model("whisper", tmp_path / "app") == (True, [])
    assert directory.is_dir()
    assert all(url.startswith(("https://huggingface.co/", "https://hf-mirror.com/"))
               for url in seen)
    assert not any(private_text in url for url in seen)
    assert "hf_endpoint_invalid" in caplog.text
    assert private_text not in caplog.text
