# -*- coding: utf-8 -*-
"""engines/llmwiki/backend.py —— LLM Wiki 无窗口后台进程的起停。

这个文件做什么：负责把 LLM Wiki Desktop 以 `--serve-api --headless`
方式拉起来、确认它就绪、安全地停掉，以及处理 429 API_RETRY_CACHE_FULL
需要的定向重启。

在整体里的位置：LLM Wiki 引擎的第一块地基。引擎的导入/整理流程、
project.open_project 都假设"后台已经在运行且无窗口"。

安全规则：
- 令牌用 secrets.token_hex(32) 生成（64 个十六进制字符），只放在
  Backend 对象的内存里，通过环境变量 LLM_WIKI_API_TOKEN 传给子进程；
  绝不进日志、异常、repr 或任何文件；
- 子进程环境只给 HOME、USER、PATH（agent_bin_dir 在最前，让后台找到
  我们的 claude/codex 包装脚本，见 agents.py）和令牌这四项；
- 后台和桌面版共用单实例锁：桌面版开着时我们启动的进程会马上退出，
  这时要抛 EngineUnavailable("llmwiki_desktop_running") 而不是瞎等。
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import time
from pathlib import Path

from .client import ApiError, Client


class EngineUnavailable(RuntimeError):
    """LLM Wiki 引擎当前用不了（环境问题，不是程序 bug）。

    code 说明原因，例如 llmwiki_desktop_running（桌面版占着单实例锁）。
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# 启动就绪的最长等待时间（秒）。
START_TIMEOUT_S = 120

# 停止/重启时等进程退出的最长时间（秒）。
STOP_TIMEOUT_S = 60

# 健康检查单次请求的超时（秒）：就绪前接口可能还没监听，失败要快。
HEALTH_TIMEOUT_S = 2.0

# 视为"进行中"的任务状态：stop / 重启前必须确认一个都没有
# （绝不打断正在生成内容的整理）。
_ACTIVE_TASK_STATUSES = {"running", "queued", "cancelling"}

# 后台子进程的 PATH：agent_bin_dir 排最前，后面接 macOS 常用系统目录。
# 故意不继承用户 PATH——后台环境保持最小，避免用户 shell 配置干扰。
_COMMON_PATH_DIRS = ("/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


def _default_spawn(argv: list[str], env: dict) -> subprocess.Popen:
    """默认的子进程启动方式：参数列表（禁止 shell=True）、新会话、
    标准输出/错误丢弃（后台自己的日志不落到我们这边，也避免令牌
    意外出现在输出里）。测试可以注入假替身。"""

    return subprocess.Popen(
        [str(a) for a in argv],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )


def _default_ps(pid: int) -> str | None:
    """默认的 PID 命令行查询：ps -p <pid> -o command=（参数列表方式）。

    返回 None 表示查不到（进程已退出）。测试注入假替身时不用真的跑 ps。
    """

    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return None
    return result.stdout


class Backend:
    """一个由我们启动、只属于我们的无窗口后台进程。

    参数：
    - binary：LLM Wiki Desktop 可执行文件路径；
    - project_root / import_root：知识库目录和导入包目录（必须已存在）；
    - port：本地 API 端口；
    - agent_bin_dir：包装脚本目录（agents.py 生成的 llmwiki-bin），
      放在子进程 PATH 最前面；
    - spawn / clock：可注入的子进程启动器和单调时钟（测试用替身）。
    """

    def __init__(
        self,
        binary,
        project_root,
        import_root,
        port: int,
        *,
        agent_bin_dir,
        spawn=None,
        clock=None,
        ps=None,
    ) -> None:
        self._binary = Path(binary)
        self._project_root = Path(project_root)
        self._import_root = Path(import_root)
        self._port = int(port)
        self._agent_bin_dir = Path(agent_bin_dir)
        self._spawn = spawn or _default_spawn
        self._clock = clock or time.monotonic
        self._ps = ps or _default_ps
        self._process: subprocess.Popen | None = None
        self._token: str | None = None
        # 本实例为"重试回执缓存满"重启过几次（每轮最多 2 次）。
        self._cache_restarts = 0

    # ---- 令牌：只在内存里，绝不外泄 ------------------------------------

    def token_provider(self):
        """给 Client 用的令牌回调：每次请求现取，令牌不落第二份。"""

        token = self._token
        if token is None:
            raise ApiError(0, "backend_not_running", False)
        return token

    def __repr__(self) -> str:  # 令牌不进 repr
        return (
            f"Backend(port={self._port}, project_root={self._project_root!s}, "
            f"running={self._process is not None and self._process.poll() is None})"
        )

    # ---- 基本属性 -------------------------------------------------------

    @property
    def pid(self) -> int | None:
        """我们启动的进程号；没启动过就是 None。"""

        return self._process.pid if self._process is not None else None

    def is_running(self) -> bool:
        """进程是否还活着（不保证接口已就绪）。"""

        return self._process is not None and self._process.poll() is None

    def client(self) -> Client:
        """一个连到本后台的客户端（令牌从内存现取）。"""

        return Client(self._port, self.token_provider)

    # ---- 启动 -----------------------------------------------------------

    def _child_env(self, token: str) -> dict:
        """构造子进程环境：只有 HOME、USER、PATH、LLM_WIKI_API_TOKEN。

        令牌通过环境变量传给后台；后台读到后会从自己的环境里删掉，
        不写钥匙串。PATH 里 agent_bin_dir 在最前，后台找
        claude/codex 时会先命中我们的包装脚本（agents.py）。
        """

        return {
            "HOME": os.environ.get("HOME", ""),
            "USER": os.environ.get("USER", ""),
            "PATH": ":".join(
                [str(self._agent_bin_dir), *_COMMON_PATH_DIRS]
            ),
            "LLM_WIKI_API_TOKEN": token,
        }

    def _expected_argv(self) -> list[str]:
        """后台的完整启动参数（全部是字符串）。"""

        return [
            str(self._binary),
            "--serve-api",
            "--headless",
            "--api-port",
            str(self._port),
            "--project-root",
            str(self._project_root),
            "--import-root",
            str(self._import_root),
        ]

    def start(self) -> None:
        """启动后台并等到它就绪（ready 且无窗口）。

        - 目录不存在：ValueError（两个目录都必须已经存在）；
        - 进程启动后马上退出：桌面版占着单实例锁，
          EngineUnavailable("llmwiki_desktop_running")；
        - 120 秒仍不就绪：ValueError("backend_start_timeout")。
        """

        if self.is_running():
            raise ValueError("backend_already_running")
        if not self._project_root.is_dir():
            raise ValueError("project_root_missing")
        if not self._import_root.is_dir():
            raise ValueError("import_root_missing")
        token = secrets.token_hex(32)  # 64 个十六进制字符，只进内存
        self._token = token
        self._process = self._spawn(self._expected_argv(), self._child_env(token))
        api = self.client()
        deadline = self._clock() + START_TIMEOUT_S
        while self._clock() < deadline:
            if self._process.poll() is not None:
                # 单实例锁冲突的表现就是进程立刻退出（后台和
                # 桌面版共用单实例锁）。清掉令牌再抛，异常文本只有错误码。
                self._token = None
                raise EngineUnavailable("llmwiki_desktop_running")
            try:
                status, payload = api.request("GET", "/health", timeout=HEALTH_TIMEOUT_S)
            except ApiError:
                # 后台还没开始监听，属于正常启动过程，继续等。
                time.sleep(0.2)
                continue
            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            if status == 200 and data.get("ready") is True and data.get("webviewWindowCount") == 0:
                return  # 就绪且无窗口，启动完成
            time.sleep(0.2)
        self._token = None
        raise ValueError("backend_start_timeout")

    # ---- 停止 -----------------------------------------------------------

    def _active_task_count(self) -> int:
        """数知识库 `.app/tasks`（或兼容布局 `.app/compat/tasks`）里
        仍处于 running / queued / cancelling 的任务数。

        任务文件是后台落盘的权威记录；
        stop / 重启前必须确认为 0，否则可能打断正在生成内容的整理。
        """

        count = 0
        for name in (".app/tasks", ".app/compat/tasks"):
            task_root = self._project_root / name
            if not task_root.is_dir():
                continue
            for task_file in task_root.glob("*.json"):
                try:
                    record = json.loads(task_file.read_text())
                except (OSError, ValueError):
                    continue  # 损坏的任务文件按"未知"处理，不阻塞统计
                task = record.get("task", record)
                if isinstance(task, dict) and task.get("status") in _ACTIVE_TASK_STATUSES:
                    count += 1
        return count

    def _wait_exit(self, timeout_s: float) -> bool:
        """等进程退出；在时限内退出返回 True。"""

        deadline = self._clock() + timeout_s
        while self._clock() < deadline:
            if self._process.poll() is not None:
                return True
            time.sleep(0.1)
        return self._process.poll() is not None

    def _verify_our_process(self) -> None:
        """核对存活的子进程确实是我们启动的那个后台（核对 PID 的命令行）。

        命令行由 self._ps(pid) 获取（默认用 ps -p <pid> -o command=，
        参数列表方式调用，没有 shell=True）；路径带空格所以只做子串
        判断。PID 被别的程序复用时绝不能误杀——直接抛错。
        """

        pid = self.pid
        if pid is None or self._process.poll() is not None:
            return  # 进程已退出，无需核对
        command = self._ps(pid)
        if command is None:
            return  # 查不到＝进程刚好退出了
        command = command.strip()
        expected_markers = (
            str(self._binary),
            "--serve-api",
            "--headless",
            f"--api-port {self._port}",
        )
        if not all(marker in command for marker in expected_markers):
            raise ValueError("backend_pid_mismatch")

    def stop(self) -> None:
        """安全停止后台：先确认没有进行中的任务，再 SIGTERM 等它退出。

        有 running / queued / cancelling 任务时抛 ValueError
        ("backend_busy")，一个字节都不发地放弃停止。
        """

        if self._process is None or self._process.poll() is not None:
            self._token = None
            return  # 没在运行，幂等返回
        if self._active_task_count() > 0:
            raise ValueError("backend_busy")
        self._process.terminate()  # SIGTERM，与桌面版正常退出同款信号
        if not self._wait_exit(STOP_TIMEOUT_S):
            raise ValueError("backend_stop_timeout")
        self._token = None

    # ---- 定向重启 ------------------------------------------------------

    def restart_for_retry_cache(self) -> None:
        """后台报 429 API_RETRY_CACHE_FULL 后的定向重启。

        背景（retry.rs）：后台把每个写请求的回执存在内存里防重复执行，
        上限 4096 条且进程活着时从不清理；满了只能重启清空。规则：
        - 重启前确认没有 running / queued / cancelling 任务（等待确认的
          任务会按落盘记录恢复，不受影响）；
        - 只重启我们自己启动的那个进程（核对 PID 的命令行）；
        - 每轮最多重启 2 次。
        """

        if self._cache_restarts >= 2:
            raise ValueError("retry_cache_restart_limit")
        if self._process is not None and self._process.poll() is None:
            if self._active_task_count() > 0:
                raise ValueError("backend_busy")
            self._verify_our_process()
            self._process.terminate()
            if not self._wait_exit(STOP_TIMEOUT_S):
                raise ValueError("backend_stop_timeout")
        self._token = None
        self._cache_restarts += 1
        self.start()
