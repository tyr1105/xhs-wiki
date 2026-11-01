# -*- coding: utf-8 -*-
"""config.py —— 全项目的路径入口和全局常量。

这个文件做什么：集中给出 xhs-wiki 在磁盘上的所有路径（数据目录、设置文件、
台账数据库、浏览器资料夹、工作目录、第三方组件、模型、日志），以及两个
全局常量（控制台端口、LaunchAgent 标签）。后面所有模块（store、ledger、
runner、daemon……）都只从这里拿路径，不允许各自拼字符串——路径改动的
时候只改这一处。

在整体里的位置：最底层的基础模块，被所有其他模块 import，自己不 import
项目里的任何模块。

测试支持：环境变量 XHS_WIKI_HOME 可以把整个数据目录指到临时目录，
XHS_WIKI_LOG_DIR 可以把日志目录指到临时目录。真实默认位置是
~/Library/Application Support/xhs-wiki 和 ~/Library/Logs/xhs-wiki（macOS
的惯例位置；LaunchAgent 的日志不能放在 ~/Documents 下，否则会以
exit 78 失败，见 docs/architecture.md §0）。

术语说明（给非程序员）：
- "0700 / 0600 权限"：UNIX 的权限位。0700 的目录只有本机当前用户能进入，
  0600 的文件只有当前用户能读写。这个工具的数据（设置、台账）都是私人的，
  所以一律收紧到这个级别。
- "LaunchAgent"：macOS 里让程序常驻/定时运行的系统机制。
"""

import os
import re
from pathlib import Path

# 控制台（本机 Web 页面）监听的端口，见 docs/architecture.md §0。
DEFAULT_PORT = 18765

# LaunchAgent 的标签（唯一标识一个常驻服务），安装脚本写 plist 时用。
LAUNCH_AGENT_LABEL = "io.github.xhs-wiki.service"

# 笔记编号的格式：24 位十六进制字符串（小红书笔记/主页编号的通用格式）。
# work_dir(note_id) 要拿它拼目录名，先校验格式，防止 "../" 之类的路径逃逸。
_NOTE_ID_RE = re.compile(r"^[0-9a-f]{24}$")


def app_home() -> Path:
    """返回应用数据目录 APP_HOME，不存在时自动创建（权限 0700）。

    默认是 ~/Library/Application Support/xhs-wiki；如果设置了环境变量
    XHS_WIKI_HOME（测试用），就用它。每次调用都重新读环境变量，
    这样测试里用 monkeypatch 改环境变量后立刻生效。
    """
    env = os.environ.get("XHS_WIKI_HOME")
    if env:
        root = Path(env).expanduser()
    else:
        root = Path.home() / "Library" / "Application Support" / "xhs-wiki"
    # parents=True：连同父目录一起建；exist_ok=True：已存在不报错。
    root.mkdir(parents=True, exist_ok=True)
    # 每次都收紧一遍权限：目录可能是用户手动建的（默认 0755）。
    os.chmod(root, 0o700)
    return root


def settings_path() -> Path:
    """设置文件 APP_HOME/settings.json 的路径（store.py 读写它）。"""
    return app_home() / "settings.json"


def ledger_path() -> Path:
    """台账数据库 APP_HOME/ledger.sqlite3 的路径（ledger.py 读写它）。"""
    return app_home() / "ledger.sqlite3"


def browser_profile_dir() -> Path:
    """专用浏览器资料夹 APP_HOME/browser-profile/（保存登录状态）。

    只返回路径、不创建：由扫码登录模块在需要时创建。
    任何代码都不读其中的文件内容（见 docs/architecture.md §0）。
    """
    return app_home() / "browser-profile"


def work_dir(note_id: str) -> Path:
    """单条收藏的工作目录 APP_HOME/work/<note_id>/。

    note_id 必须是 24 位十六进制编号，否则抛 ValueError——这个值要直接
    拼进路径，先校验格式可以从源头上挡住路径注入。
    """
    if not isinstance(note_id, str) or not _NOTE_ID_RE.match(note_id):
        raise ValueError("note_id_invalid")
    return app_home() / "work" / note_id


def llm_work_dir() -> Path:
    """整理模型子进程的专用工作目录 APP_HOME/llm-workdir/。

    为什么需要它：claude / codex 这类 CLI 会把"当前工作目录"当成项目目录，
    读里面的项目说明文件（CLAUDE.md、AGENTS.md），甚至因为目录没权限
    （macOS 的 TCC 隔离，比如"文稿"文件夹）而卡住。所以整理模型的命令行
    统一在这个专用的空目录里运行：目录里没有任何说明文件，也不会碰用户
    的文档目录。每次调用都重新创建/收紧权限到 0700，并保持为空
    （代码只把它当 cwd 用，不往里写东西）。
    """
    path = app_home() / "llm-workdir"
    path.mkdir(parents=True, exist_ok=True)
    # 和 app_home 一样每次收紧一遍：用户手动建的目录默认 0755。
    os.chmod(path, 0o700)
    return path


def vendor_dir() -> Path:
    """第三方组件目录 APP_HOME/vendor/（安装脚本把 XHS-Downloader 放这里）。"""
    return app_home() / "vendor"


def models_dir() -> Path:
    """语音模型目录 APP_HOME/models/（安装脚本下载并校验后放这里）。"""
    return app_home() / "models"


def log_dir() -> Path:
    """日志目录，默认 ~/Library/Logs/xhs-wiki，可用 XHS_WIKI_LOG_DIR 覆盖。

    和 app_home 一样自动创建并收紧到 0700（日志里可能有笔记标题等
    私人内容）。
    """
    env = os.environ.get("XHS_WIKI_LOG_DIR")
    if env:
        root = Path(env).expanduser()
    else:
        root = Path.home() / "Library" / "Logs" / "xhs-wiki"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root
