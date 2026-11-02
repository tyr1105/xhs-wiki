# -*- coding: utf-8 -*-
"""assets.py —— 安装资源的下载、校验和落地。

这个文件做什么：安装脚本（install.sh）通过命令行 ``xhs-wiki assets …``
调用这里的四类操作：
1. ``install_xhs_downloader``：下载 XHS-Downloader 压缩包 → sha256 核对 →
   解压到 ``APP_HOME/vendor/`` → 用 uv 把它自己的依赖装进 APP_HOME/venv；
2. ``install_model``：下载语音模型（whisper 逐个文件下载；sensevoice 是
   压缩包解压再补一个 VAD 文件），全部按 assets_manifest.json 里写死的
   sha256 / 大小核对；
3. ``verify_model``：检查模型文件是否齐全且未损坏（供 doctor / 安装脚本
   重复执行时跳过重复下载用）。
4. ``install_llmwiki``：按需下载 LLM Wiki 无窗口后台，核对压缩包与
   程序本体后整体替换；不启动后台、不安装桌面版。

在整体里的位置：docs/architecture.md §13 安装流程的 Python 一侧；被
cli.py（assets 子命令）调用，自己只依赖标准库和 config.py。

红线：下载内容和校验值全部来自 ``assets_manifest.json``（公开信息，
可以进仓库）；Whisper 允许替换为指定镜像的域名，但仓库、版本和文件路径
保持不变。本模块绝不下载清单之外的内容，也不把 URL 写进任何日志。

测试支持：网络（http，兼容旧的流式替身）、下载器（downloader，
默认走系统 curl）和 uv 命令都是参数注入的替身——测试用内存里的假文件
流、假 curl 和假的 uv，不访问网络、不装任何东西、不真的启动 curl。

术语说明：
- "sha256"：一种内容指纹。同一个文件算出的指纹恒定，文件改动一位指纹
  就完全不同——安装包用它来防止下载损坏或被调包。
- "原子替换"：先写同目录临时文件，全部校验通过后再 ``os.replace`` 改名
  到目标位置。中途断电只会留下一个临时文件，不会留下半成品。
- "断点续传"：下载写进固定的 ``.<文件名>.part`` 临时文件；下次
  下载同一个文件时用 HTTP Range 请求从已下载的字节数接着下，不用从头
  再拉 1.5 GB。
- "Happy Eyeballs"：IPv4 和 IPv6 同时发起连接、谁先通用谁的策略。
  浏览器和 curl 都有；Python 的 urllib 没有——这是下载改用 curl 的
  根本原因（IPv6 坏掉的网络里 urllib 会逐个地址各等 60 秒）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from . import config

# 模块级日志器：只记错误代码这类固定短语，不记 URL、路径或任何用户输入
# （HF_ENDPOINT 可能带私有参数或账号，原文绝不进日志）。
logger = logging.getLogger("xhs_wiki.assets")

# 清单文件随包分发（src/xhs_wiki/assets_manifest.json），只读、绝不改写。
MANIFEST_PATH = Path(__file__).resolve().parent / "assets_manifest.json"

# 流式下载每次读的字节数：64 KiB 足够让进度回调平滑，又不占内存。
CHUNK_BYTES = 64 * 1024

# macOS 自带的 curl 全路径。写死路径而不是依赖 PATH：安装环境里用户
# 的 PATH 可能装了别的 curl（甚至别名），系统自带的版本行为最可控。
CURL_BIN = "/usr/bin/curl"

# curl 下载进行中轮询临时文件大小的间隔（秒）。取 0.5：进度条每秒
# 刷新两次已经足够平滑，又不至于频繁 stat 打扰下载。测试里会把它
# patch 小，让"进度单调上升"能在毫秒级被观察到。
CURL_POLL_SECONDS = 0.5

# 支持的语音模型名单（与 parse/asr.py 的 ModelName 保持一致）。
MODEL_NAMES = ("whisper", "sensevoice")

# 后台本体通常较大；向导每几秒轮询时只检查文件属性，内容没有改变就
# 复用上次指纹结果。缓存键包含绝对路径，值还绑定清单指纹，不能把
# 另一个安装目录或另一个发布版本的就绪结论混用。
_LLMWIKI_VERIFY_CACHE: dict[Path, tuple[tuple, bool]] = {}
_LLMWIKI_VERIFY_LOCK = threading.Lock()

# 这些包包含需要编译的代码，只允许使用预先编译好的安装包（wheel）。
# 让 uv 按本机系统选择兼容版本，避免 Intel 或较旧 macOS 被迫源码编译。
DOWNLOADER_BINARY_PACKAGES = (
    "av,ctranslate2,onnxruntime,numpy,tokenizers,sherpa-onnx,cryptography,"
    "lxml,curl-cffi,pydantic-core,pyobjc-core"
)


def load_manifest() -> dict:
    """读取安装资源清单（每类资源的 URL、sha256、大小、解压目录）。"""
    with MANIFEST_PATH.open("r", encoding="utf-8") as source:
        return json.load(source)


# ---------------------------------------------------------------------------
# 网络访问（可注入）
# ---------------------------------------------------------------------------

def _curl_pid_path(temporary_path: Path) -> Path:
    """PID 是系统的进程编号；把写入者编号放在续传文件旁，供重启后核对。"""
    return Path(str(temporary_path) + ".pid")


def _write_curl_pid(path: Path, pid: int) -> None:
    """只原子保存进程编号，不保存命令行；临时文件从创建起就是 0600。"""
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as output:
            output.write(f"{pid}\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _curl_process_matches(pid: int, temporary_path: Path, *, timeout=2) -> bool:
    """确认编号仍属于写这个文件的 curl；查询失败时停止下载，绝不猜测归属。

    命令行只在内存里核对，不能进入日志或异常消息。每次发送信号前都
    重新查询，避免等待期间 PID 被复用时误杀其他程序。
    进程查询和清理失败统一用已有 assets_error，控制台已有对应中文说明；
    不把原始命令输出或用户路径作为错误详情透出。
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        raise ValueError("assets_error") from None
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("assets_error") from None
    if result.returncode != 0:
        # ps 查询前后进程可能恰好结束；只将确实已消失的 PID 当成过期记录。
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            pass
        raise ValueError("assets_error")
    return "curl" in result.stdout and str(temporary_path) in result.stdout


def _wait_orphan_curl(pid: int, temporary_path: Path) -> bool:
    """最多等待五秒让旧写入者退出；单调时钟不受系统时间校正影响。"""
    deadline = time.monotonic() + 5
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if not _curl_process_matches(pid, temporary_path, timeout=min(2, remaining)):
            return True
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def _cleanup_orphan_curl(temporary_path: Path) -> None:
    """先核对再结束遗留 curl；无关进程只清 PID 文件，不能给它发送信号。"""
    path = _curl_pid_path(temporary_path)
    try:
        pid = int(path.read_text(encoding="ascii").strip())
    except FileNotFoundError:
        return
    except (ValueError, UnicodeError):
        pid = 0
    except OSError:
        raise ValueError("assets_error") from None
    try:
        # 0、负数会被 kill 当成进程组，1 和当前服务自身也不可能是旧 curl。
        if pid > 1 and pid != os.getpid() and _curl_process_matches(pid, temporary_path):
            try:
                os.kill(pid, signal.SIGTERM)
                if not _wait_orphan_curl(pid, temporary_path):
                    # 等待后再次核对，PID 若已复用就不能发送 SIGKILL。
                    if _curl_process_matches(pid, temporary_path):
                        os.kill(pid, signal.SIGKILL)
                        if not _wait_orphan_curl(pid, temporary_path):
                            raise ValueError("assets_error")
            except ProcessLookupError:
                pass  # 核对后进程自行退出，已不再写文件。
        path.unlink(missing_ok=True)
    except OSError:
        # 清理失败必须保留凭据并停止；不能让新旧下载器同时写 .part。
        raise ValueError("assets_error") from None


def _terminate_curl(process) -> None:
    """回收本次创建的子进程：先温和终止，五秒不退出再强制结束。"""
    if process.poll() is not None:
        return
    try:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("assets_error") from None


class CurlDownloadManager:
    """持有当前 curl 的句柄，让 AssetManager 停止时能同步结束它。

    启动、登记 PID 和停止共用一把锁，关闭后永不再启动新进程，避免
    服务退出恰好撞上下一文件下载时漏掉 curl。网络轮询不持锁。
    """

    def __init__(self, *, popen=None):
        """进程创建器可注入；测试只运行假下载器，不执行系统 curl。"""
        self._popen = popen or subprocess.Popen
        self._lock = threading.Lock()
        self._stopped = False
        self._current = None

    def download(self, url: str, temporary_path: Path, *, on_bytes=None) -> None:
        """适配现有 downloader 参数，把整个安装过程的 curl 归到同一管理器。"""
        _curl_download(url, temporary_path, on_bytes=on_bytes, manager=self)

    def _start(self, args: list[str], temporary_path: Path):
        """锁内清旧写入者、创建新写入者并登记，stop 不会漏过启动窗口。"""
        with self._lock:
            if self._stopped:
                raise ValueError("assets_error")
            if self._current is not None:
                raise ValueError("assets_error")
            _cleanup_orphan_curl(temporary_path)
            # 先以私有权限创建续传文件，避免 curl 使用默认权限暴露模型临时文件。
            descriptor = os.open(temporary_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            os.close(descriptor)
            temporary_path.chmod(0o600)
            process = self._popen(args, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
            self._current = (process, _curl_pid_path(temporary_path))
            try:
                _write_curl_pid(self._current[1], process.pid)
            except BaseException:
                self._finish_locked(process)
                raise
            return process

    def _finish_locked(self, process) -> None:
        """确认子进程不再写入后才删 PID；失败保留记录以便下次启动核对。"""
        if self._current is None or self._current[0] is not process:
            return
        _terminate_curl(process)
        path = self._current[1]
        try:
            # 旧服务的 finally 不能误删新服务已经写入的 PID。
            if path.read_text(encoding="ascii").strip() == str(process.pid):
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError):
            raise ValueError("assets_error") from None
        self._current = None

    def _finish(self, process) -> None:
        """下载结束或回调异常时回收句柄，与退出动作串行执行。"""
        with self._lock:
            self._finish_locked(process)

    def stop(self) -> None:
        """永久关闭管理器并同步停止当前 curl；重复调用没有副作用。"""
        with self._lock:
            self._stopped = True
            if self._current is not None:
                self._finish_locked(self._current[0])


def _curl_download(url: str, temporary_path: Path, *, on_bytes=None,
                   manager=None) -> None:
    """默认的下载实现：用 macOS 自带的 curl 把 ``url`` 下到
    ``temporary_path``。

    做什么、为什么这样做：
    - 用参数列表启动 ``/usr/bin/curl``（绝不 shell=True，路径里有空格
      也安全）。curl 自带 "Happy Eyeballs"：IPv4 / IPv6 同时去连，
      谁先连上用谁。旧的 ``urllib.request.urlopen`` 会按解析顺序逐个
      地址尝试，每个最多等 60 秒——IPv6 坏掉的网络（很多家庭宽带、
      公司 VPN 都是这样）里，要等所有 IPv6 地址超时才轮到 IPv4，
      表现为下载卡在某个百分比好几分钟不动。
    - 参数逐个说明：
      ``--fail``            HTTP 4xx/5xx 时返回非 0 退出码而不是把
                            错误页当正文存下来；
      ``--location``        跟随重定向（HuggingFace 改仓库名后会跳转）；
      ``--silent``          不画进度条（我们用 on_bytes 自己算进度）；
      ``--show-error``      配合 --silent，出错时仍向 stderr 报一句；
      ``--connect-timeout 15`` 单个地址连接最多等 15 秒（urllib 是 60）；
      ``--retry 3 --retry-delay 2`` 传输中途失败自动重试 3 次、间隔 2 秒
                            （1.5 GB 的模型文件值得自动重试）；
      ``--continue-at -``   断点续传：从 ``temporary_path`` 现有大小
                            接着下，不用每次从头拉；
      ``--output <临时文件>`` 直接写进临时文件，不经过我们的内存。
    - 下载进行中每 ``CURL_POLL_SECONDS`` 秒 stat 一次临时文件大小，
      通过 ``on_bytes(done)`` 报出去（由 fetch 换算成 0–1 的进度）。
      进程退出后再补报一次最终大小，保证最后一块也计入。
    - 代理：curl 自己会读 ``HTTPS_PROXY`` 等环境变量，与 urllib 的
      行为一致，这里不需要额外处理。

    失败语义：
    - curl 退出码非 0（断网、404、写盘失败等）→ ``ValueError(
      "assets_network_error")``，错误信息里不带 URL（红线）；
    - curl 启动不起来（路径不对等 OSError）→ 同样报
      ``assets_network_error``；
    - 两种情况都**不删**临时文件——``--continue-at -`` 下次接着下。

    测试注入：``fetch(downloader=…)`` 换掉整个函数；也可以只 patch
    ``subprocess.Popen`` 造一个假 curl 子进程（本模块测试两种都用，
    绝不真的启动 curl、不访问网络）。
    """
    args = [
        CURL_BIN,
        "--fail",
        "--location",
        "--silent",
        "--show-error",
        "--connect-timeout", "15",
        "--retry", "3",
        "--retry-delay", "2",
        "--continue-at", "-",
        "--output", str(temporary_path),
        url,
    ]
    manager = manager or CurlDownloadManager()
    try:
        # stderr 丢弃：curl 的报错文本可能带 URL，不能让它流进日志。
        process = manager._start(args, temporary_path)
    except OSError:
        raise ValueError("assets_network_error") from None
    try:
        # 轮询等待：poll() 返回 None 表示还在跑。
        while process.poll() is None:
            _report_bytes(temporary_path, on_bytes)
            time.sleep(CURL_POLL_SECONDS)
        _report_bytes(temporary_path, on_bytes)
    finally:
        # 正常完成、网络失败和回调异常都要回收子进程及 PID 文件。
        manager._finish(process)
    if process.returncode != 0:
        raise ValueError("assets_network_error")


def _report_bytes(temporary_path: Path, on_bytes) -> None:
    """把临时文件的当前大小报给回调；文件还没创建时静默跳过。"""
    if on_bytes is None:
        return
    try:
        on_bytes(temporary_path.stat().st_size)
    except OSError:
        # curl 尚未创建输出文件：这一次不报，下一轮再说。
        pass


@contextmanager
def _default_http(url: str):
    """兼容保留的流式网络实现：urllib 打开 URL，返回二进制文件流。

    默认下载走 ``_curl_download``（见其文档字符串里 IPv6 的
    解释）；这个函数保留给两个场景：
    1. ``fetch(http=…)`` 注入内存流替身的旧测试路径（行为不变）；
    2. 需要流式读取（而不是落盘）的调用方。
    生产代码不再默认使用它。
    """
    import urllib.request

    with urllib.request.urlopen(url, timeout=60) as stream:
        yield stream


# ---------------------------------------------------------------------------
# fetch：通用的"下载 + 校验 + 原子落地"
# ---------------------------------------------------------------------------

def fetch(url: str, dest: Path, *, sha256: str = None, size: int = None,
          http=None, progress=None, downloader=None) -> None:
    """把 ``url`` 下载到 ``dest``，先校验再原子替换。

    做什么、为什么这样做：
    - 默认走 ``_curl_download``：curl 写进 ``dest`` 同目录固定
      名字的 ``.<文件名>.part`` 临时文件，支持断点续传；不传 ``http``
      且不传 ``downloader`` 时生效；
    - 传 ``http`` 时走旧的流式路径（``http(url)`` 返回上下文管理的
      二进制文件流，测试注入内存流替身）——对外行为与改用 curl 之前完全
      一致；
    - 下载完先核对大小和 sha256（清单里写死的值），不匹配就抛带错误
      代码的异常——绝不把损坏的内容替换到目标位置。curl 路径下校验
      失败会删掉 ``.part`` 并从头重下一次，第二次仍不符才报错；
      网络失败则保留 ``.part`` 供断点续传；
    - 全部通过才 ``os.replace`` 到 ``dest``（同目录改名是原子操作）。

    参数：
    - ``sha256`` / ``size``：期望的指纹和字节数；None 表示不校验这一项。
    - ``http``：流式替身，见上；和 ``downloader`` 二选一，``http`` 优先。
    - ``downloader``：``downloader(url, temporary_path, *, on_bytes)``
      的替身，默认 ``_curl_download``；``on_bytes(done)``
      在下载过程中被调用，``done`` 是已落盘的字节数。
    - ``progress``：``progress(fraction_or_None)`` 回调（统一约定：
      0–1 的下载比例；``size`` 未知时传 ``None``，表示"下载中、比例
      未知"），安装脚本和守护进程都用它显示进度；默认不回调。

    可能抛出的错误代码（ValueError）：
    - ``assets_network_error``：curl 退出码非 0、连接打不开或中途断流；
    - ``assets_size_mismatch`` / ``assets_sha256_mismatch``：校验不过。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if http is not None:
        _fetch_via_stream(url, dest, sha256=sha256, size=size,
                          http=http, progress=progress)
    else:
        _fetch_via_curl(url, dest, sha256=sha256, size=size,
                        progress=progress, downloader=downloader)


def _fetch_via_stream(url: str, dest: Path, *, sha256, size,
                      http, progress) -> None:
    """旧的流式下载路径（改用 curl 后保留）：从 ``http(url)`` 的文件流逐块读。

    供 ``fetch(http=…)`` 的调用方和旧测试使用，行为与改用 curl 之前完全
    一致：写随机名临时文件、边下边算指纹、失败删临时文件。
    """
    opener = http
    temporary: Path | None = None
    descriptor = -1
    try:
        with opener(url) as stream:
            descriptor, name = tempfile.mkstemp(prefix="." + dest.name + ".",
                                                dir=dest.parent)
            temporary = Path(name)
            digest = hashlib.sha256()
            done = 0
            while True:
                chunk = stream.read(CHUNK_BYTES)
                if not chunk:
                    break
                os.write(descriptor, chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress is not None:
                    # 统一约定：回调只传一个 0–1 的比例；size 未知时
                    # 传 None（调用方显示"下载中"而不是错误的 0%）。
                    progress((done / size) if size else None)
            os.close(descriptor)
            descriptor = -1
        # 连接正常结束才做校验：大小先查（便宜），指纹后查（要读全文）。
        if size is not None and done != size:
            raise ValueError("assets_size_mismatch")
        if sha256 is not None and digest.hexdigest() != sha256:
            raise ValueError("assets_sha256_mismatch")
        temporary.chmod(0o600)
        os.replace(temporary, dest)
        temporary = None
    except ValueError:
        raise
    except OSError as error:
        # 网络错误、磁盘错误统一成一个错误代码；原始异常可能带 URL，
        # 不能让它流进日志（红线），只保留原因链给调试者。
        raise ValueError("assets_network_error") from error
    finally:
        # 校验失败 / 断网：删掉临时文件，不留半成品。
        if temporary is not None:
            try:
                if descriptor > 0:
                    os.close(descriptor)
            except OSError:
                pass
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _fetch_via_curl(url: str, dest: Path, *, sha256, size,
                    progress, downloader) -> None:
    """curl 下载路径（默认）：下到固定 ``.part`` 再校验、原子替换。

    与流式路径的差别只有两点：
    1. 临时文件名固定为 ``.<目标文件名>.part``——同一个文件重试时
       curl 用 ``--continue-at -`` 接着上次的字节继续下（断点续传）；
    2. 指纹在下载完成后读文件分块计算（1.5 GB 也不占内存），而不是
       边下边算。
    """
    runner = downloader or _curl_download
    # 固定名的 .part：断点续传的前提是两次下载用同一个临时文件。
    part = dest.parent / ("." + dest.name + ".part")

    def on_bytes(done: int):
        # 统一约定：0–1 的比例；size 未知传 None（"下载中"）。
        if progress is not None:
            progress((done / size) if size else None)

    # 只给内容校验失败一次完整重下机会，网络失败仍保留断点并立即上抛。
    # 第二次调用前文件已删除，因此 --continue-at - 会从零开始。
    for attempt in range(2):
        runner(url, part, on_bytes=on_bytes if progress is not None else None)
        try:
            mismatch = None
            if size is not None and part.stat().st_size != size:
                mismatch = "assets_size_mismatch"
            elif sha256 is not None and _sha256_of(part) != sha256:
                mismatch = "assets_sha256_mismatch"
            if mismatch is not None:
                part.unlink(missing_ok=True)
                if attempt == 1:
                    raise ValueError(mismatch)
                on_bytes(0)
                continue
            part.chmod(0o600)
            os.replace(part, dest)
            return
        except OSError:
            # 读取和落盘失败不算内容损坏，不能消耗完整重下机会。
            raise ValueError("assets_network_error") from None


# ---------------------------------------------------------------------------
# 解压（tar.gz / tar.bz2）
# ---------------------------------------------------------------------------

def _extract_archive(archive: Path, dest_dir: Path) -> None:
    """把 tar 压缩包安全地解压到 ``dest_dir``。

    ``filter="data"`` 是 Python 3.12 的安全解压模式：拒绝绝对路径、
    ``..`` 逃逸和设备文件——第三方压缩包不能 blindly 信任。解压失败
    （损坏、不是 tar）抛 ``assets_extract_failed``。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:*") as tar:
            tar.extractall(dest_dir, filter="data")
    except (tarfile.TarError, OSError) as error:
        raise ValueError("assets_extract_failed") from error


# ---------------------------------------------------------------------------
# uv 命令（可注入）
# ---------------------------------------------------------------------------

def _default_uv(args: list) -> None:
    """默认的 uv 执行器：参数列表启动（绝不 shell=True），失败抛错。

    uv 是 Astral 的 Python 包管理器（安装脚本负责装好它）；这里只按
    XHS-Downloader 的 requirements.txt 把它的依赖装进我们的 venv。
    """
    try:
        result = subprocess.run(
            ["uv"] + list(args), capture_output=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as error:
        # 找不到 uv 和安装超时都属于可重试的依赖安装失败；不暴露命令输出。
        raise ValueError("assets_uv_failed") from error
    if result.returncode != 0:
        raise ValueError("assets_uv_failed")


# ---------------------------------------------------------------------------
# 三个安装动作
# ---------------------------------------------------------------------------

def xhs_downloader_ready(app_home: Path) -> bool:
    """检查下载组件的目录和依赖安装完成标记（不下载、不执行组件）。

    解压目录存在只能证明文件已展开，不能证明依赖装好。只有安装成功后
    才写入完成标记，后台复查时还要核对两样东西：
    1. 版本号：防止半成品或旧版被当成就绪；
    2. 依赖装进的那个虚拟环境（venv）的"身份"（见
       _venv_identity）。组件的依赖装在 APP_HOME/venv 里，而安装脚本遇到
       venv 损坏时会让用户删掉 venv 重建——新 venv 里只有 xhs-wiki 本体的
       依赖，组件需要的包全不在了，vendor 目录和旧标记却原样留着。以前只
       核对版本，会把这种状态判成就绪，之后每次运行都报找不到模块、而且
       没有"重试"入口。现在 venv 一换，身份就对不上，自动按"未就绪"重装。
    旧格式的标记（只有版本号一行）同样按未就绪处理，重装一次即可。
    """
    entry = load_manifest()["xhs_downloader"]
    vendor = Path(app_home) / "vendor"
    if not (vendor / entry["extract_dir"]).is_dir():
        return False
    try:
        installed = (vendor / ".xhs-downloader-ready").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return installed == downloader_marker_text(app_home, _downloader_version(entry))


def _downloader_version(entry: dict) -> str:
    """清单的目录名以版本号结尾，从中取版本，避免另加一个可能漂移的来源。"""
    return entry["extract_dir"].rsplit("-", 1)[-1]


def _venv_identity(app_home: Path) -> str:
    """APP_HOME/venv 的"身份"：venv 目录和其中 pyvenv.cfg 的磁盘编号。

    删掉 venv 再重建，得到的是全新的目录和文件：inode（文件在磁盘上的
    编号）、创建时间都会变，哪怕内容一字不差。所以记下这几个数就能分辨
    "还是不是当初装依赖的那个 venv"：
    - venv 目录本身的设备号、inode、创建时间（装包、升级都不会改变它们）；
    - pyvenv.cfg（每个虚拟环境根目录里的配置文件，建 venv 时写一次）的
      inode 和修改时间；没有这个文件时记 "no-cfg"。
    只需两次 stat，后台每 30 秒复查一次也不费事。venv 目录不存在时返回
    固定的 "venv-missing"。
    """
    venv = Path(app_home) / "venv"
    try:
        folder = os.stat(venv)
    except OSError:
        return "venv-missing"
    identity = (f"{folder.st_dev}:{folder.st_ino}:"
                f"{getattr(folder, 'st_birthtime', 0)!r}")
    try:
        cfg = os.stat(venv / "pyvenv.cfg")
    except OSError:
        return identity + "|no-cfg"
    return identity + f"|{cfg.st_ino}:{cfg.st_mtime_ns}"


def downloader_marker_text(app_home: Path, version: str) -> str:
    """完成标记应有的完整内容：第 1 行版本号，第 2 行 venv 身份。"""
    return f"{version}\n{_venv_identity(app_home)}\n"


def _write_downloader_ready(path: Path, text: str) -> None:
    """依赖成功后原子写入完成标记；中断时不留下可被误读的半份内容。"""
    descriptor, name = tempfile.mkstemp(prefix=".xhs-downloader-ready-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def model_ready(name: str, app_home: Path) -> bool:
    """检查语音模型是否齐全且未损坏（verify_model 的薄封装）。

    未知模型名返回 False（而不是抛异常）：后台线程遇到未知名字不该崩，
    状态里显示 failed + 错误代码即可，用户改回合法设置后重试就好。
    """
    if name not in MODEL_NAMES:
        return False
    ok, _missing = verify_model(name, app_home)
    return ok


def llmwiki_supported() -> bool:
    """判断是否为 Apple 芯片的 Mac，包含使用 Rosetta 的 Python。

    Rosetta 是让 Intel 程序在 Apple 芯片上运行的兼容层；此时 uname
    会显示 x86_64，所以再询问内核的 arm64 能力。检测命令只读硬件信息，
    最多等待五秒；失败、未知机器类型和非 macOS 平台都按不支持处理。
    """
    try:
        machine = os.uname()
        if machine.sysname != "Darwin":
            return False
        if machine.machine == "arm64":
            return True
        if machine.machine != "x86_64":
            return False
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.optional.arm64"],
            capture_output=True, text=True, timeout=5, check=False)
        return result.returncode == 0 and result.stdout.strip() == "1"
    except (OSError, subprocess.SubprocessError, AttributeError):
        return False


def llmwiki_binary_path(app_home: Path) -> Path:
    """从随包清单推导本体的绝对路径，供下载、状态和自动配置共同使用。"""
    entry = load_manifest()["llmwiki"]
    return (Path(app_home).resolve() / "vendor" / entry["extract_dir"]
            / entry["binary"])


def llmwiki_ready(app_home: Path) -> bool:
    """只核对普通程序文件和固定指纹，不执行程序、不触发下载。

    文件大小、修改时间或清单指纹改变才重算 SHA-256，另核对文件编号
    和属性变更时间，避免整体替换后沿用旧缓存。符号链接不算已安装
    本体；读文件期间属性变了也不缓存为就绪，下次检查再重新核对。
    """
    entry = load_manifest()["llmwiki"]
    binary = llmwiki_binary_path(app_home)
    try:
        if binary.is_symlink() or not binary.is_file():
            with _LLMWIKI_VERIFY_LOCK:
                _LLMWIKI_VERIFY_CACHE.pop(binary, None)
            return False
        identity = _llmwiki_file_identity(binary, entry["binary_sha256"])
        with _LLMWIKI_VERIFY_LOCK:
            cached = _LLMWIKI_VERIFY_CACHE.get(binary)
        if cached is not None and cached[0] == identity:
            return cached[1]
        ready = _sha256_of(binary) == entry["binary_sha256"]
        if binary.is_symlink() or _llmwiki_file_identity(binary, entry["binary_sha256"]) != identity:
            with _LLMWIKI_VERIFY_LOCK:
                _LLMWIKI_VERIFY_CACHE.pop(binary, None)
            return False
        with _LLMWIKI_VERIFY_LOCK:
            _LLMWIKI_VERIFY_CACHE[binary] = (identity, ready)
        return ready
    except OSError:
        with _LLMWIKI_VERIFY_LOCK:
            _LLMWIKI_VERIFY_CACHE.pop(binary, None)
        return False


def _llmwiki_file_identity(binary: Path, expected_sha256: str) -> tuple:
    """把文件的轻量属性和清单指纹合为缓存身份，不读取文件内容。"""
    info = binary.stat()
    return (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino,
            expected_sha256)


def _replace_llmwiki_component(component: Path, destination: Path) -> None:
    """完整目录一次改名落地，失败先还原；还原也失败时保留唯一旧备份。

    备份放在解压临时目录之外。这样双重磁盘故障时，外层清理新文件
    不会连旧目录一起删掉；留下的 .llmwiki-previous-* 只保存完整旧目录，
    不会被 llmwiki_ready 当作正式安装，也不会被下一次下载自动删除。
    """
    backup_root = None
    backup = None
    installed = False
    try:
        if destination.exists() or destination.is_symlink():
            backup_root = Path(tempfile.mkdtemp(prefix=".llmwiki-previous-", dir=destination.parent))
            backup = backup_root / "previous"
            os.replace(destination, backup)
        try:
            os.replace(component, destination)
            installed = True
        except BaseException:
            # 包括中断在内，只有还原成功才允许清除备份容器；还原抛错时
            # backup 仍在，finally 必须保留它供人工恢复。
            if backup is not None:
                os.replace(backup, destination)
            raise
    finally:
        if backup_root is not None and (installed or not (backup.exists() or backup.is_symlink())):
            shutil.rmtree(backup_root)


def install_llmwiki(app_home: Path, *, progress=None, downloader=None,
                   http=None) -> Path:
    """下载并校验无窗口后台，成功后整体安装到 vendor 下的版本目录。

    fetch 仍负责系统 curl、断点续传与一次校验重下；解压只发生在同一
    磁盘的私有临时目录。压缩包与本体都通过固定 SHA-256 校验后才把
    完整目录改名到正式位置，失败不留下半成品，也不破坏旧的完整安装。
    这里只返回绝对路径，设置由守护进程或 CLI 调用方原子写回。
    """
    if not llmwiki_supported():
        raise ValueError("llmwiki_unsupported_platform")
    app_home = Path(app_home).resolve()
    entry = load_manifest()["llmwiki"]
    if llmwiki_ready(app_home):
        # 重复安装不重复下载，同时修复误改的可执行权限。
        binary = llmwiki_binary_path(app_home)
        try:
            binary.chmod(0o755)
        except OSError:
            raise ValueError("assets_error") from None
        return binary
    vendor = app_home / "vendor"
    try:
        app_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        vendor.mkdir(exist_ok=True, mode=0o700)
    except OSError:
        raise ValueError("assets_error") from None
    archive = vendor / (entry["extract_dir"] + ".tar.gz")
    fetch(entry["url"], archive, sha256=entry["sha256"], size=entry["size"],
          http=http, progress=progress, downloader=downloader)

    # 临时目录与目标相邻，确保最后的 os.replace 不会跨磁盘变成复制。
    try:
        with tempfile.TemporaryDirectory(prefix=".llmwiki-install-", dir=vendor) as temporary:
            staging = Path(temporary)
            _extract_archive(archive, staging)
            component = staging / entry["extract_dir"]
            if component.is_symlink() or not component.is_dir():
                raise ValueError("assets_extract_dir_missing")
            binary = component / entry["binary"]
            if binary.is_symlink() or not binary.is_file():
                raise ValueError("llmwiki_binary_missing")
            if _sha256_of(binary) != entry["binary_sha256"]:
                raise ValueError("assets_sha256_mismatch")

            # 压缩包自带的权限不直接沿用：资料仅本人可读，只有本体可执行。
            component.chmod(0o700)
            for item in component.rglob("*"):
                if not item.is_symlink():
                    item.chmod(0o700 if item.is_dir() else 0o600)
            binary.chmod(0o755)
            destination = vendor / entry["extract_dir"]
            _replace_llmwiki_component(component, destination)
    except OSError:
        raise ValueError("assets_error") from None
    finally:
        # 网络失败留给 fetch 保留断点；完整包已经拿到后的失败则清理包和展开目录。
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            raise ValueError("assets_error") from None
    return llmwiki_binary_path(app_home)


def asset_total_bytes(name: str) -> "int | None":
    """按清单估算一类资源下载后占用的字节数（控制台进度条显示用）。

    - whisper / sensevoice：把清单里各文件的大小加起来（whisper 约
      1.5 GiB，sensevoice 约 227 MiB + VAD）；
    - xhs_downloader：清单没写压缩包大小（校验只靠 sha256），返回 None，
      页面对未知大小显示"下载中"而不是百分比。
    """
    manifest = load_manifest()
    if name == "xhs_downloader":
        return None
    entry = manifest.get(name)
    if not entry:
        return None
    if name == "llmwiki":
        return entry["size"]
    total = 0
    for _sha256, size in entry.get("files", {}).values():
        total += size
    vad = entry.get("vad")
    if vad:
        total += vad.get("size", 0)
    return total


def install_xhs_downloader(app_home: Path, *, http=None, uv=None,
                           progress=None, downloader=None) -> Path:
    """下载并安装下载组件 XHS-Downloader（docs/architecture.md §0 vendor/）。

    步骤：下载 tar.gz → sha256 核对 → 解压到 ``APP_HOME/vendor/`` →
    确认清单里写的顶层目录确实存在 → 用 ``uv pip install --python
    <APP_HOME/venv/bin/python> -r requirements.txt`` 装它的依赖。

    参数注入：``http`` 同 fetch（走流式路径）；``downloader`` 同 fetch
    （默认系统 curl，走断点续传路径）；``uv(args)`` 是执行
    uv 命令的替身（测试里只记录参数，不真装）；``progress(
    fraction_or_None)`` 是下载进度的回调（统一约定：0–1 的比例；
    清单没写压缩包大小，fetch 拿不到总大小时传 None，控制台显示
    "下载中"而不是百分比）。

    返回解压出来的组件目录。可能抛：``assets_size_mismatch`` /
    ``assets_sha256_mismatch`` / ``assets_extract_failed`` /
    ``assets_extract_dir_missing`` / ``assets_requirements_missing`` /
    ``assets_uv_failed`` / ``assets_network_error``。
    """
    app_home = Path(app_home)
    entry = load_manifest()["xhs_downloader"]
    vendor = app_home / "vendor"
    vendor.mkdir(parents=True, exist_ok=True, mode=0o700)

    # 1. 下载压缩包到 vendor 下（文件名取 URL 末段，固定且无路径分隔符）。
    archive = vendor / "XHS-Downloader-2.8.tar.gz"
    fetch(entry["url"], archive, sha256=entry["sha256"], http=http,
          progress=progress, downloader=downloader)

    # 2. 解压并核对顶层目录（清单写死 XHS-Downloader-2.8，防止包结构
    #    变了还静默继续）。
    component = vendor / entry["extract_dir"]
    ready_marker = vendor / ".xhs-downloader-ready"
    # 开始改动组件前废弃旧标记，失败后不会把旧标记与新半成品拼成“就绪”。
    ready_marker.unlink(missing_ok=True)
    try:
        _extract_archive(archive, vendor)
        if not component.is_dir():
            raise ValueError("assets_extract_dir_missing")

        # 3. 用 uv 装依赖；二进制包限制让不同芯片和系统选择可用的 wheel。
        requirements = component / "requirements.txt"
        if not requirements.is_file():
            raise ValueError("assets_requirements_missing")
        venv_python = app_home / "venv" / "bin" / "python"
        runner = uv or _default_uv
        runner(["pip", "install", "--python", str(venv_python),
                "--only-binary", DOWNLOADER_BINARY_PACKAGES,
                "-r", str(requirements)])
        # 标记里同时记下版本号和这次装依赖用的 venv 身份。
        _write_downloader_ready(
            ready_marker,
            downloader_marker_text(app_home, _downloader_version(entry)))
    except Exception:
        # 安装失败后的目录不可执行，删掉后用户重试才能从完整组件重新开始。
        # 原始失败继续抛出，不能把清理动作误报为安装成功。
        ready_marker.unlink(missing_ok=True)
        if component.is_dir():
            shutil.rmtree(component)
        raise

    # 4. 压缩包已解压校验完毕，删掉省 1 MiB 磁盘（失败不阻断安装）。
    try:
        archive.unlink(missing_ok=True)
    except OSError:
        pass
    return component


def _endpoint_usable(endpoint: str) -> bool:
    """一个下载站点地址能不能用：http/https 开头、带主机名、能正常解析。"""
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname
    except ValueError:
        # 比如 "https://[未闭合" 这类写法，解析时就会报错。
        return False
    return parsed.scheme in {"http", "https"} and bool(host)


def _whisper_download_bases(base: str) -> list[str]:
    """只替换下载站点，保留清单中固定的仓库、revision（版本）和文件路径。

    优先使用用户指定的 HF_ENDPOINT，再试官方站点和国内镜像。去重避免
    用户恰好指定官方站点时重复请求；每个站点得到的内容仍须核对同一摘要。

    HF_ENDPOINT 写错（比如漏了 https://）时**跳过它**，继续
    试官方站和镜像，而不是直接报网络错误。安装时终端里的 HF_ENDPOINT
    会原样写进后台服务的配置，以前写错一次，后台每一轮下载都在发请求
    之前就失败，语音模型永远装不上、整个产品都不会开跑。跳过时只记一条
    固定的错误代码日志，不记地址原文（可能带私有参数）。
    """
    path = urlsplit(base).path
    endpoints = [os.environ.get("HF_ENDPOINT", ""),
                 "https://huggingface.co", "https://hf-mirror.com"]
    bases = []
    for endpoint in endpoints:
        endpoint = endpoint.strip().rstrip("/")
        if not endpoint:
            continue
        if not _endpoint_usable(endpoint):
            logger.warning("hf_endpoint_invalid")
            continue
        candidate = endpoint + path
        if candidate not in bases:
            bases.append(candidate)
    return bases


def install_model(name: str, app_home: Path, *, http=None, progress=None,
                  downloader=None) -> Path:
    """下载并安装语音模型（whisper 或 sensevoice）到 ``APP_HOME/models/``。

    - whisper：没有统一压缩包，按清单逐个文件下载到
      ``models/whisper-large-v3-turbo/``，每个文件都核对 sha256 和大小；
    - sensevoice：下载 tar.bz2 压缩包 → 解压 → 核对里面的两个模型文件
      的指纹 → 再下载 Silero VAD 到 ``models/silero_vad.onnx``。

    ``progress(fraction_or_None)``（统一约定）收到 0–1 的整体进度，
    或 None（比例未知）：whisper 按清单里各文件的大小折算（"第几个文件
    + 当前文件内部比例"）；sensevoice 分两段（压缩包占前 90%、VAD 占后
    10%；压缩包大小未知，前一段一直是 None——控制台显示"下载中"）。
    ``downloader`` 同 fetch（测试注入假 curl 用）。

    返回模型所在目录。未知模型名抛 ``assets_model_invalid``；下载校验
    过程的错误代码同 fetch / 解压。
    """
    if name not in MODEL_NAMES:
        raise ValueError("assets_model_invalid")
    app_home = Path(app_home)
    models = app_home / "models"
    entry = load_manifest()[name]

    def report(fraction):
        # 统一在这里判空：没传回调就什么都不做。
        if progress is not None:
            progress(None if fraction is None
                     else min(1.0, max(0.0, fraction)))

    if name == "whisper":
        directory = models / entry["dir"]
        bases = _whisper_download_bases(entry["base"])
        total_files = len(entry["files"])
        for index, (filename, (sha256, size)) in enumerate(entry["files"].items()):
            # 整体进度 = 已完成的文件数占比 + 当前文件内部的占比。
            # 当前文件比例未知（清单没写大小）时整体也只能是 None。
            def file_progress(fraction, _idx=index):
                if fraction is None:
                    report(None)
                else:
                    report((_idx + fraction) / total_files)

            for base_index, base in enumerate(bases):
                try:
                    # 各站点共用同一目标路径，因此 fetch 的固定 .part 文件
                    # 也保持不变；换源后 curl 从已下载的字节数继续传输。
                    fetch(base + filename, directory / filename,
                          sha256=sha256, size=size, http=http, downloader=downloader,
                          progress=file_progress if progress is not None else None)
                    break
                except ValueError as error:
                    # 只有连接或传输失败才换源。摘要/大小错误意味着内容
                    # 不可信，必须停下报告，不能用下一站覆盖这次校验失败。
                    if (str(error) != "assets_network_error"
                            or base_index == len(bases) - 1):
                        raise
            report((index + 1) / total_files)
        return directory

    # sensevoice：压缩包 → 解压 → 核对两个文件 → VAD。
    archive = models / (entry["dir"] + ".tar.bz2")

    def archive_progress(fraction):
        # 压缩包大小未知（清单只写了 sha256）：前一段没有比例可算，
        # 保持 None（"下载中"），而不是假装 0%。
        report(None if fraction is None else fraction * 0.9)

    fetch(entry["archive"], archive, http=http, downloader=downloader,
          progress=archive_progress if progress is not None else None)
    report(0.9)
    _extract_archive(archive, models)
    directory = models / entry["dir"]
    if not directory.is_dir():
        raise ValueError("assets_extract_dir_missing")
    # 解压出来只是"结构对"，内容仍要按清单逐个核对指纹。
    for filename, (sha256, size) in entry["files"].items():
        target = directory / filename
        if not target.is_file() or target.stat().st_size != size:
            raise ValueError("assets_size_mismatch")
        if _sha256_of(target) != sha256:
            raise ValueError("assets_sha256_mismatch")
    vad = entry["vad"]

    def vad_progress(fraction):
        # VAD 占整体进度的最后 10%（VAD 的大小清单里有写）。
        report(None if fraction is None else 0.9 + fraction * 0.1)

    fetch(vad["url"], models / "silero_vad.onnx",
          sha256=vad["sha256"], size=vad["size"], http=http,
          downloader=downloader,
          progress=vad_progress if progress is not None else None)
    report(1.0)
    try:
        archive.unlink(missing_ok=True)
    except OSError:
        pass
    return directory


def _sha256_of(path: Path) -> str:
    """算一个本地文件的 sha256（核对用；1.5 GB 的模型也只需几秒）。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_model(name: str, app_home: Path) -> tuple[bool, list]:
    """核对语音模型是否齐全且未损坏，返回 ``(ok, missing)``。

    ``missing`` 是没通过检查的文件名列表（不存在、大小不对或指纹不对
    都算 missing）。安装脚本重复执行时先调它，ok 就跳过重复下载。
    未知模型名抛 ``assets_model_invalid``。
    """
    if name not in MODEL_NAMES:
        raise ValueError("assets_model_invalid")
    models = Path(app_home) / "models"
    entry = load_manifest()[name]
    missing: list = []
    if name == "whisper":
        directory = models / entry["dir"]
        for filename, (sha256, size) in entry["files"].items():
            if not _file_matches(directory / filename, sha256, size):
                missing.append(filename)
    else:
        directory = models / entry["dir"]
        for filename, (sha256, size) in entry["files"].items():
            if not _file_matches(directory / filename, sha256, size):
                missing.append(filename)
        vad = entry["vad"]
        if not _file_matches(models / "silero_vad.onnx",
                             vad["sha256"], vad["size"]):
            missing.append("silero_vad.onnx")
    return (not missing, missing)


def _file_matches(path: Path, sha256: str, size: int) -> bool:
    """一个文件是否存在、大小和指纹是否与清单一致。"""
    try:
        if path.stat().st_size != size:
            return False
    except OSError:
        return False
    return _sha256_of(path) == sha256
