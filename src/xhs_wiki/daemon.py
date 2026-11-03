# -*- coding: utf-8 -*-
"""daemon.py —— 守护进程：本机控制台 HTTP 服务 + 定时器 + 子进程管理。

这个文件做什么：整个常驻服务的"总管家"，同时干三件事：
1. 提供本机网页控制台（web/console.html）和 JSON 接口（127.0.0.1:18765），
   包括首次设置向导的各接口；
2. 跑一个定时线程（每 30 秒一次 tick），判断要不要开跑每日运行 / 追加运行，
   并在登录过期时自动拉起扫码登录子进程；
3. 管理两类子进程：运行子进程（`python -m xhs_wiki.cli run --trigger …
   --home <APP_HOME>`）和扫码子进程（`python -m xhs_wiki.cli login --home …`）。

在整体里的位置：docs/architecture.md §11、§1。被 cli（serve 子命令）调用；
自己调用 store / ledger / config / llm / engines（全部通过可注入的工厂）。

安全规矩：
- 访问令牌：除 /healthz 和 /auth 外，所有页面和接口都要带
  POST /auth 用令牌换来的 Cookie（令牌见 console_auth.py）；
- /api/settings 在路由层另设白名单，可执行文件路径、知识库
  位置、引擎、完成标记等一律不能经它修改；
- "打开知识库文件夹"只用 /usr/bin/open -R 在访达里选中；
- 向导里换知识库位置时搬移旧内容：按实际目录判断新旧位置的包含关系；
  只在跨磁盘时复制、核对完才删原件；旧目录里有用户自己的文件时只搬
  本工具生成的五类内容；
- /healthz 的身份证明和跳转页的一次性登录码都绑定端口；每个连接有
  读写超时、同时处理的连接数有上限。

经过真实环境验证的基础行为：
- 只监听 127.0.0.1，检查 Host 和 Origin；
- CSRF 令牌用 secrets.compare_digest 比较；POST 校验 Content-Type 和请求体大小（4 KB）；
- 接口结果缓存 30 秒（知识库统计）；
- 定时、补跑、追加运行（store.due_daily / due_followup）；
- 子进程沿用守护进程的数据目录（--home），关闭父进程手里的日志句柄，
  子进程崩溃没写记录时补一条 error 记录；
- 启动时清理"假运行中"状态和残留的二维码；
- 扫码前删除旧二维码，扫码成功后只有确实需要登录时才自动开跑。

红线：任何响应、日志、状态里都不出现签名链接（xsec_token）和 API Key。
status 里的 settings.profile_url 是规范主页链接（不带签名），允许出现。
"""

import errno
import fcntl
import filecmp
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import config
from . import console_auth
from .ledger import Ledger, FAILED_PREFIX
from .llm.base import LLMError
from .store import (
    DEFAULT_SETTINGS,
    ServiceStore,
    LLM_PROVIDERS,
    due_daily,
    due_followup,
    is_same_or_inside,
    next_run_at,
    now_beijing,
    validate_kb_root,
    validate_settings,
)

# ---------------- 固定参数 ----------------

# 模块级日志器：只记极简的兜底信息（如异常类型名），不打印 URL、
# 提示词或任何可能带敏感内容的文本。
logger = logging.getLogger("xhs_wiki.daemon")

DEFAULT_PORT = config.DEFAULT_PORT      # 18765，见 docs/architecture.md §0
# POST 请求体上限 4 KB：足够放设置修改，又防止恶意大包。
MAX_BODY_BYTES = 4096
# 定时线程每 30 秒检查一次要不要开跑。
TICK_SECONDS = 30
# 慢接口（知识库统计）缓存的秒数：目录遍历不便宜，控制台却每 5 秒轮询。
CACHE_SECONDS = 30
# 登录过期时自动拉起扫码的间隔（6 小时内不重复）。
LOGIN_AUTORETRY_HOURS = 6
# 判断"子进程没写最终记录"的宽限秒数。
CRASH_GRACE_SECONDS = 5
# 失败列表最多返回的条数（页面够用，又不会把响应撑大）。
FAILED_NOTES_LIMIT = 50

# 给用户看的固定中文文案，集中定义。
MESSAGE_STALE_RUNNING = "上次运行意外中断（服务重启或断电），可点「立即运行」重新开始"
MESSAGE_RUNNER_CRASHED = "运行子进程异常退出，没有留下完整记录"
# 服务启动时打印到日志的一行（控制台要带访问令牌才能打开，不再
# 把地址当成"直接打开就能用"的入口写出来）。
MESSAGE_SERVING = ("xhs-wiki 控制台服务已在 127.0.0.1:{port} 启动；"
                   "在终端运行 xhs-wiki open 打开控制台")

# "打开知识库文件夹"用的系统命令，写绝对路径，不按 PATH 查找。
OPEN_BIN = console_auth.OPEN_BIN
# 知识库目录里的引擎标记目录（轻量引擎的状态库在 .xhs-wiki/engine.sqlite3）。
# 旧位置有它，才说明那里是本工具生成过内容的知识库，换位置时要搬过去。
KB_MARKER_DIR = ".xhs-wiki"
# 判断"新位置是不是空的"时可以忽略的文件：访达打开文件夹就会自动生成
# .DS_Store（只记图标排列方式，删了会自动重建），不能因为它就说"不是空的"。
KB_IGNORABLE_NAMES = (".DS_Store",)
# 本工具在知识库根目录里生成的五类内容（和 uninstall.sh --purge-kb 的
# 白名单一致）。换位置时只认它们是"本工具的东西"；旧目录里
# 还有别的条目（用户自己的文件、Obsidian 的 .obsidian 等），就只搬这五类，
# 用户的文件原地不动，绝不把整个"文稿"或笔记库改名搬走。
KB_GENERATED_NAMES = ("索引.md", "暂缓.md", "主题", "来源", KB_MARKER_DIR)
# 知识库新建时的目录权限：只有本人能进。已存在的用户目录不改权限。
KB_DIR_MODE = 0o700

# 每个连接的读写超时秒数：以前不设超时，同机任何程序
# 只连不发、或者声明了请求体长度却不发完，都会让处理线程和文件句柄一直
# 被占着；攒到几百个就能把控制台占死（连 /healthz 都连不上）。
REQUEST_TIMEOUT_SECONDS = 10
# 同时处理的连接数上限：超过时新连接直接关掉，避免线程和句柄无限增长。
# 控制台只有本人一个页面在用（每 5 秒轮询一次），50 足够宽裕。
MAX_CONCURRENT_CONNECTIONS = 50


def no_auth_page(port: int) -> bytes:
    """没有访问 Cookie 时 GET / 返回的提示页。

    不含任何数据，也不含 CSRF 令牌——令牌只有带着合法 Cookie 的请求才能
    拿到。服务不在默认端口时，提示里写出带 ``--port`` 的完整
    命令。以前只写"运行 xhs-wiki open"，用户照做就会去默认端口——而默认
    端口此时可能正被别的程序占着（README 教用户端口被占时换端口运行），
    令牌会被送到那个程序手里。
    """
    command = "xhs-wiki open"
    if int(port) != config.DEFAULT_PORT:
        command += f" --port {int(port)}"
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n<head><meta charset="utf-8">\n'
        "<title>xhs-wiki 控制台</title></head>\n"
        "<body>\n<h1>xhs-wiki 控制台</h1>\n"
        "<p>这个页面需要验证身份后才能查看。</p>\n"
        f"<p>请在终端运行 <code>{command}</code> 打开控制台。</p>\n"
        "</body>\n</html>\n"
    ).encode("utf-8")


# 兼容旧名字：默认端口下的提示页（测试和旧代码引用）。
NO_AUTH_PAGE = no_auth_page(config.DEFAULT_PORT)

# /api/settings（日常页的设置表单）允许修改的字段白名单。
# 只收日常页真正会发的字段；kb_root / engine / llmwiki_binary /
# setup_completed 等走各自的专用接口（/api/setup/*），不经这里——
# 以前这里把请求体原样交给 store，任何字段都能写，可执行文件路径
# 也能写进去并被守护进程执行。llm 单独处理：只允许改 model 一个子键。
# 注意：不能直接缩小 store.EDITABLE_SETTINGS_FIELDS——向导的 setup 接口、
# cli login 和 runner 还要靠 store.update_settings 写那些字段。
CONSOLE_SETTINGS_FIELDS = (
    "schedule_enabled",
    "daily_time",
    "max_new_per_run",
    "cleanup_enabled",
    "asr_model",
)
# llm 里日常页能改的子键（模型名；提供方和接口地址只能走向导）。
CONSOLE_LLM_FIELDS = ("model",)


def console_settings_changes(body: dict, current: dict) -> dict:
    """把 /api/settings 的请求体过滤成可以交给 store 的修改。

    - 只收 CONSOLE_SETTINGS_FIELDS 里的字段，值原样交给 store 校验；
    - llm 必须是只含 model 一个键的对象，合并进当前的 llm（提供方和
      base_url 保持不变），模型名的合法性同样由 store 校验；
    - 出现任何别的字段（包括 llm.claude_path 之类的子键）：抛
      ``ValueError("field_not_editable:<字段名>")``，整个请求不生效。
    """
    changes = {}
    for key, value in body.items():
        if key in CONSOLE_SETTINGS_FIELDS:
            changes[key] = value
        elif key == "llm":
            if not isinstance(value, dict) or not value:
                raise ValueError("field_not_editable:llm")
            for sub_key in value:
                if sub_key not in CONSOLE_LLM_FIELDS:
                    raise ValueError(f"field_not_editable:llm.{sub_key}")
            changes["llm"] = {**current.get("llm", {}), **value}
        else:
            raise ValueError(f"field_not_editable:{key}")
    return changes


def _kb_dir_is_empty(path: Path) -> bool:
    """新位置算不算"空"：不存在，或者是只有可忽略文件（.DS_Store）的目录。

    读不了（没权限）时抛 OSError，由调用方转成错误代码。
    """
    if not os.path.lexists(path):
        return True
    if path.is_symlink() or not path.is_dir():
        return False
    return all(entry.name in KB_IGNORABLE_NAMES for entry in path.iterdir())


def kb_migration_needed(old: Path, new: Path, *, app_home: Path) -> bool:
    """换知识库位置时，旧位置的内容要不要搬过去。

    要搬的条件（全部满足）：
    - 新旧不是同一个目录（按实际目录比较，大小写、Unicode 写法不同的
      同一个目录也算同一个）；
    - 旧位置有 ``.xhs-wiki/`` 标记（说明本工具在那里生成过内容）；
    - 旧位置本身仍能通过 kb_root 校验——修复前存进来的危险值（比如
      主目录本身）绝不搬移，只切换设置、旧内容原地不动。

    旧位置读不了（没权限、磁盘拔了）时同样只切换设置：看不到内容就
    无从搬起，旧内容原地保留，权限恢复后用户还能手动取回；不能因为
    旧位置坏了就让用户永远换不了位置。
    """
    try:
        if old.exists() and new.exists() and os.path.samefile(old, new):
            return False
        if old.resolve() == new.resolve():
            return False
        if not (old / KB_MARKER_DIR).is_dir():
            return False
    except OSError:
        return False
    try:
        validate_kb_root(str(old), app_home=app_home)
    except ValueError:
        return False
    return True


def _remove_tree_guarded(target: Path, *, must_not_touch: Path) -> None:
    """删除 target（目录整棵删，文件/链接直接删），但带一道保险。

    保险：target 和 must_not_touch 只要有一方在另一方里面（按实际目录
    比较），就拒绝删除并抛 ValueError——绝不能因为路径别名把要保留的
    那一份也删了（曾经出过"删副本时连原件一起删"的问题）。
    """
    if (is_same_or_inside(target, must_not_touch)
            or is_same_or_inside(must_not_touch, target)):
        raise ValueError("kb_root_move_unsafe")
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    elif os.path.lexists(target):
        target.unlink()


def _verify_copy(source: Path, copy: Path) -> None:
    """逐项核对复制结果：目录结构、符号链接目标、每个文件的完整内容。

    filecmp.cmp(shallow=False) 会真正逐字节比较两份文件（不只看大小和
    修改时间）。知识库是 Markdown 和一个小数据库，逐字节比较很快。
    任何一处对不上就抛 ValueError，调用方不会删原件。
    """
    if source.is_symlink():
        if not copy.is_symlink() or os.readlink(source) != os.readlink(copy):
            raise ValueError("kb_root_copy_mismatch")
        return
    if source.is_file():
        if not copy.is_file() or not filecmp.cmp(source, copy, shallow=False):
            raise ValueError("kb_root_copy_mismatch")
        return
    if not copy.is_dir() or copy.is_symlink():
        raise ValueError("kb_root_copy_mismatch")
    for dirpath, dirnames, filenames in os.walk(source):
        relative = Path(dirpath).relative_to(source)
        for name in dirnames + filenames:
            _verify_copy_entry(Path(dirpath) / name, copy / relative / name)


def _verify_copy_entry(source: Path, copy: Path) -> None:
    """_verify_copy 的单项核对（目录只核对"存在且是目录"，内容由 walk 继续）。"""
    if source.is_symlink():
        if not copy.is_symlink() or os.readlink(source) != os.readlink(copy):
            raise ValueError("kb_root_copy_mismatch")
    elif source.is_dir():
        if not copy.is_dir() or copy.is_symlink():
            raise ValueError("kb_root_copy_mismatch")
    elif source.is_file():
        if not copy.is_file() or not filecmp.cmp(source, copy, shallow=False):
            raise ValueError("kb_root_copy_mismatch")


def _copy_verified(source: Path, target: Path, *, old: Path) -> None:
    """跨磁盘时的"复制"一步：完整复制到 target 并核对，失败就删掉半份副本。

    这里只复制、绝不删除原件（原件在全部条目都复制核对完后才删，见
    _relocate）。target 不能已经存在，也不能在旧知识库里面。
    """
    if os.path.lexists(target) or is_same_or_inside(target, old):
        raise ValueError("kb_root_move_unsafe")
    try:
        if source.is_dir() and not source.is_symlink():
            # symlinks=True：符号链接按链接本身复制，不顺着链接复制
            # 链接指向的（可能在知识库之外的）内容。
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target, follow_symlinks=False)
        _verify_copy(source, target)
    except BaseException:
        # 半份副本删掉（保险：绝不碰旧知识库），原样抛出让调用方回滚。
        try:
            _remove_tree_guarded(target, must_not_touch=old)
        except (OSError, ValueError):
            pass
        raise


def _relocate(pairs: list, *, old: Path) -> None:
    """把若干条目从旧位置挪到新位置（pairs 是 [(原路径, 新路径), …]）。

    规则（替换原来的 shutil.move）：
    1. 先尝试 os.rename——同一块磁盘上是瞬间完成的改名，原件不经过复制；
    2. 只有 os.rename 报 EXDEV（"跨磁盘，不能直接改名"）时才走复制：
       先完整复制并逐字节核对；**所有条目都复制核对完**之后，才删除原件；
    3. 其他任何错误（包括 EINVAL"把目录挪进它自己里面"）一律回滚已经
       做过的改名、删掉已经做好的副本，报 kb_root_move_failed——绝不删除
       原件。以前 shutil.move 遇到 EINVAL 会退回"复制 + 删除"，而它判断
       "目标在不在源里面"也是字符串比较，结果把原件和副本一起删光；
    4. 删原件前再确认一次：新位置不在旧知识库里面、副本确实存在。
       删原件时出错不再回滚（新位置已有核对过的完整副本，数据安全），
       旧位置残留的文件留给用户自己处理。
    """
    done = []  # [(原路径, 新路径, "renamed" 或 "copied")]
    try:
        for source, target in pairs:
            try:
                os.rename(source, target)
                done.append((source, target, "renamed"))
                continue
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
            _copy_verified(source, target, old=old)
            done.append((source, target, "copied"))
    except (OSError, shutil.Error, ValueError):
        # 回滚：改名的改回去；复制出来的副本删掉（原件从没动过）。
        for source, target, how in reversed(done):
            try:
                if how == "renamed":
                    os.rename(target, source)
                else:
                    _remove_tree_guarded(target, must_not_touch=old)
            except (OSError, ValueError):
                pass
        raise ValueError("kb_root_move_failed") from None
    for source, target, how in done:
        if how != "copied":
            continue
        # 删原件前的最后一道核对（任何一条不满足就保留原件）。
        if (not os.path.lexists(target) or is_same_or_inside(target, source)
                or is_same_or_inside(target, old)):
            continue
        try:
            _remove_tree_guarded(source, must_not_touch=target)
        except (OSError, ValueError):
            logger.warning("kb_root_old_copy_not_removed")


def migrate_kb_root(old: Path, new: Path) -> str:
    """把旧知识库搬到新位置，返回搬法；失败抛带错误代码的 ValueError。

    前置检查（不满足就什么都不动）：
    - 新位置就是旧知识库、或在它里面 → kb_root_inside_old_kb。
      按**实际目录**比较（store.is_same_or_inside，逐级 samefile），大小写、
      Unicode 组合方式、/System/Volumes/Data 别名不同的写法都认得出来；
      以前是字符串比较，这些写法会被放过，最后把整个知识库删光；
    - 新位置不是空的 → kb_root_target_not_empty（绝不合并、绝不覆盖用户
      已有的文件；包含旧知识库的上级目录自然也"不是空的"）；
    - 新位置的上级目录建不出来或不可写 → kb_root_not_writable；
    - 旧位置或新位置读不了 → kb_root_inaccessible。

    两种搬法：
    - "all"：旧目录里只有本工具生成的五类内容（加 .DS_Store）时，整个
      目录改名搬过去（新位置若是只剩 .DS_Store 的空目录，先把它删掉，
      否则会搬进它**里面**）；
    - "generated"：旧目录里还有用户自己的文件（比如知识库
      设在一个已有的笔记库里），只把五类生成内容逐个搬到新目录，用户的
      文件和旧目录本身都原地不动。
    具体挪动交给 _relocate：只在跨磁盘时复制，复制核对完才删原件。
    """
    if is_same_or_inside(new, old):
        raise ValueError("kb_root_inside_old_kb")
    try:
        if not _kb_dir_is_empty(new):
            raise ValueError("kb_root_target_not_empty")
        names = os.listdir(old)
    except OSError:
        raise ValueError("kb_root_inaccessible") from None
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        if not os.access(new.parent, os.W_OK):
            raise OSError("parent_not_writable")
    except OSError:
        raise ValueError("kb_root_not_writable") from None
    user_names = [name for name in names
                  if name not in KB_GENERATED_NAMES
                  and name not in KB_IGNORABLE_NAMES]
    if user_names:
        created_new = not os.path.lexists(new)
        try:
            # 新目录用私有权限新建；已存在的空目录保持原样。
            new.mkdir(mode=KB_DIR_MODE, exist_ok=True)
        except OSError:
            raise ValueError("kb_root_not_writable") from None
        pairs = [(old / name, new / name) for name in KB_GENERATED_NAMES
                 if os.path.lexists(old / name)]
        try:
            _relocate(pairs, old=old)
        except ValueError:
            # 搬失败已回滚；这次新建的目录如果还空着，顺手删掉（rmdir 只删空目录）。
            if created_new:
                try:
                    new.rmdir()
                except OSError:
                    pass
            raise
        return "generated"
    try:
        if os.path.lexists(new):
            for name in KB_IGNORABLE_NAMES:
                (new / name).unlink(missing_ok=True)
            new.rmdir()
    except OSError:
        raise ValueError("kb_root_move_failed") from None
    _relocate([(old, new)], old=old)
    return "all"

# 组件（XHS-Downloader）或语音模型还在后台下载时，阻止开跑的
# "需要用户处理"事项。文案写成大白话：用户什么都不用做，
# 下载完成后会自动开始。
ASSETS_NOT_READY_TITLE = "组件还在下载，完成后会自动开始"


def _register_extra_titles() -> None:
    """把 assets_not_ready 的文案合并进 store 的文案表。

    沿用 runner.py 的做法（见那边的注释）：先 import notify 让它的
    TEMPLATES 拍下快照，再往 store.NEEDS_USER_TITLES 里 setdefault——
    保证无论谁先加载，notify 的表都不会多出这个代码，tests/test_notify.py
    锁定的内容不被破坏；setdefault 保证 store 将来自己定义时以它为准。
    """
    from . import notify as notify_module  # noqa: F401  先触发 notify 的快照
    from . import store as store_module
    store_module.NEEDS_USER_TITLES.setdefault("assets_not_ready",
                                              ASSETS_NOT_READY_TITLE)


_register_extra_titles()

# POST 路由表：do_POST 里按这个名字分发（集中定义，方便对照和测试）。
POST_ROUTES = (
    "/api/run-now",
    "/api/pause",
    "/api/resume",
    "/api/settings",
    "/api/login",
    "/api/assets/retry",
    "/api/setup/kb-root",
    "/api/setup/engine",
    "/api/setup/llm",
    "/api/setup/key",
    "/api/setup/test-llm",
    "/api/setup/first-import",
    "/api/setup/finish",
    "/api/setup/restart",
    "/api/notes/retry",
    "/api/open-kb",
    "/api/skill-targets",
)


def _manifest_size(entry: dict, path: Path) -> int:
    """从资源清单里查一个文件应有的字节数（轻量复查用）。

    清单结构：``files`` 是 {文件名: (sha256, 大小)}；sensevoice 额外有
    一个公共的 ``vad``（silero_vad.onnx）。查不到返回 -1（表示"清单没
    写"），调用方把它当成"大小对不上"处理，触发一次完整校验。
    """
    files = entry.get("files", {})
    name = path.name
    if name in files:
        return files[name][1]
    vad = entry.get("vad")
    if vad and name == "silero_vad.onnx":
        return vad.get("size", -1)
    return -1


def remove_stale_login_qr(store: ServiceStore) -> None:
    """删除服务目录里残留的二维码截图（login-qr.png），不存在时忽略。

    二维码本来该由扫码流程在结束时删掉，但扫码子进程被强制结束时来不及删，
    旧文件留在原地会让下一次扫码刚启动就显示过期的旧码。三个时机删除：
    守护进程启动时、每次拉起扫码子进程之前、扫码子进程退出之后。
    """
    try:
        os.unlink(Path(store.root) / "login-qr.png")
    except OSError:
        pass  # 文件不存在（正常情况）或删不掉，都不影响主流程。


# ---------------------------------------------------------------------------
# AssetManager：后台资源准备线程
# ---------------------------------------------------------------------------

class AssetManager:
    """后台资源准备线程：检查下载组件、语音模型及按需启用的 LLM Wiki。
    缺什么就用 assets 模块下载，进度写进内存状态供 /api/status 展示；
    未选 LLM Wiki 且向导未请求时，不检查或下载这个可选后台。

    为什么放在服务端：安装脚本不再等待约 1.5 GB
    的模型下载完，而是先把控制台打开；下载在守护进程的后台线程里做，
    期间首次向导照常可以操作，资源就绪后导入自动开始。

    对外状态（每类资源一个 dict，/api/status 的 assets 字段原样给出）：
    ``{"state": "ready"|"downloading"|"failed"|"not_needed", "progress": 0–1 或 None
       （None = 下载中、比例未知，页面显示"下载中"）,
       "error": 错误代码或 None, "total_bytes": 预计字节数或 None}``。

    依赖全部可注入（测试不访问网络、不真的下载）：
    - ``check()`` → ({"xhs_downloader": bool, "asr_model": bool,
      按需包含 "llmwiki": bool}, 模型名)；
    - ``install(kind, 模型名, progress)`` → 实际下载（LLM Wiki 返回程序路径；progress 回调收
      一个 0–1 的比例或 None，统一约定；抛出的任何异常都会把
      这一项标成 failed，ValueError 的消息就是错误代码）；
    - ``interval``：下载都结束后的复查间隔秒数（设置里换语音模型后，
      下一轮复查会发现新模型缺失并自动补下载）。
    - ``curl``：可注入的下载控制器，提供 download 和 stop；默认控制器
      记住当前 curl，服务退出时同步结束它并禁止后续启动。
    """

    KINDS = ("xhs_downloader", "asr_model", "llmwiki")

    def __init__(self, store: ServiceStore, *, check=None, install=None,
                 interval: int = 30, curl=None):
        # 下载控制器保存当前 curl 句柄，并让“停止”和“启动下一次下载”互斥；
        # 不能只停 Python 线程，否则 curl 会继续往相同的 .part 文件里写。
        from .assets import CurlDownloadManager
        self.store = store
        self._curl = curl if curl is not None else CurlDownloadManager()
        self._check = check or self._default_check
        self._install = install or self._default_install
        self.interval = interval
        self._lock = threading.Lock()
        # 向导下载尚未通过健康检查时不能提前保存 engine；用内存标记
        # 记录这次明确的下载请求。切回轻量引擎后立即解除依赖。
        self._llmwiki_requested = False
        # 内部状态带下划线开头的私有键（_tried），state() 输出时会剥掉。
        self._state = {kind: {"state": "downloading", "progress": 0.0,
                              "error": None, "total_bytes": None,
                              "model": None, "_tried": False}
                       for kind in self.KINDS}
        from .assets import asset_total_bytes
        self._state["llmwiki"].update(
            state=("downloading" if store.load_settings().get("engine")
                   == "llmwiki" else "not_needed"),
            total_bytes=asset_total_bytes("llmwiki"))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        # 全部就绪后的回调（Supervisor 接上：清事项 + 自动开跑一次）。
        self.on_ready = None
        self._ready_fired = False
        # 语音模型名每次现读设置：向导/日常页改了模型，复查时会跟上。
        self.asr_model_getter = lambda: self.store.load_settings().get(
            "asr_model") or "whisper"
        # 模型文件完整校验（sha256）结果的缓存，
        # {文件路径: (大小, 修改时间纳秒, 是否校验通过)}。30 秒一次的
        # 复查只比对文件存在和大小（stat，微秒级），完整 sha256 只在
        # 服务启动后第一次检查、下载完成后、或文件的 mtime/大小变化时
        # 才重算——1.6 GB 的模型每 30 秒读一遍磁盘算哈希太费资源。
        self._verify_cache = {}

    # ---------------- 对外查询 ----------------

    def state(self) -> dict:
        """返回去掉私有键的状态快照（线程安全）。"""
        with self._lock:
            return {kind: {k: v for k, v in entry.items()
                           if not k.startswith("_")}
                    for kind, entry in self._state.items()}

    def all_ready(self) -> bool:
        """所需资源是否都就绪；未选择的可选后台不挡轻量引擎运行。"""
        with self._lock:
            return all(self._state[kind]["state"] in ("ready", "not_needed")
                       for kind in self.KINDS)

    # ---------------- 对外动作 ----------------

    def _llmwiki_needed(self) -> bool:
        """现读引擎设置，再合并向导请求，避免为轻量用户校验或下载后台。"""
        selected = self.store.load_settings().get("engine") == "llmwiki"
        with self._lock:
            return selected or self._llmwiki_requested

    def request_llmwiki(self) -> dict:
        """记录向导请求并唤醒下载；重复点击不会重置正在进行的下载。"""
        with self._lock:
            self._llmwiki_requested = True
            entry = self._state["llmwiki"]
            if entry["state"] in ("not_needed", "failed", "ready"):
                entry.update(state="downloading", progress=0.0,
                             error=None, _tried=False)
        self._wake.set()
        return self.state()["llmwiki"]

    def release_llmwiki_request(self) -> None:
        """切回轻量引擎时释放向导依赖；已下载的文件仍保留供下次使用。"""
        with self._lock:
            self._llmwiki_requested = False
            self._state["llmwiki"].update(
                state="not_needed", progress=0.0, error=None, _tried=False)
        self._wake.set()

    def start(self) -> None:
        """启动后台线程（幂等：已经启动过就不再启动）。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run,
                                        name="assets-prepare", daemon=True)
        self._thread.start()

    def retry(self) -> None:
        """把 failed 的资源重置为待下载并立刻唤醒线程（重试按钮）。"""
        with self._lock:
            for entry in self._state.values():
                if entry["state"] == "failed":
                    entry["state"] = "downloading"
                    entry["progress"] = 0.0
                    entry["error"] = None
                    entry["_tried"] = False
        self._wake.set()

    def wake_asr(self) -> None:
        """立即唤醒后台线程去复查语音模型（控制台切换语音模型后调用）。

        两件事：
        1. 把 asr_model 这一项重置成"待检查"（_tried=False、state=downloading、
           进度清零）——用户换成了另一个模型时，旧模型已经下载成功
           （state=ready），只重置 _tried 的话 _prepare 里 all_ready 仍按
           旧的 ready 短路返回，新模型缺了也不会触发下载；
        2. set 唤醒事件，打断 interval 睡眠，让复查马上发生。

        新模型其实已下载的情况也没有副作用：_prepare 里 check 通过会把
        state 直接标回 ready。已下载的另一个模型不会被删除。
        """
        with self._lock:
            self._state["asr_model"].update(state="downloading", progress=0.0,
                                            error=None, _tried=False)
        self._wake.set()

    def stop(self) -> None:
        """停止资源准备并结束当前 curl；保留 .part 给下次服务安全续传。

        先置停止事件，避免下载结束后继续下一资源或触发自动开跑；控制器
        同步终止 curl，也会拒绝与退出恰好同时发生的下一次下载启动。
        """
        self._stop.set()
        self._wake.set()
        self._curl.stop()

    # ---------------- 线程主体 ----------------

    def _run(self):
        while not self._stop.is_set():
            try:
                self._prepare()
            except Exception as error:
                # 后台线程绝不能因为一次检查/下载出错就死掉。兜底
                # 之前先记一条日志，内容只有异常类型名——异常文本可能带
                # URL（红线：签名链接等绝不进日志），不能原样打印。
                logger.warning("assets_prepare_unexpected:%s",
                               type(error).__name__)
                continue
            # 睡到下一次复查，retry() 会提前唤醒。
            self._wake.wait(self.interval)
            self._wake.clear()

    def _prepare(self):
        """一轮"检查 → 下载缺的 → 就绪回调"。"""
        if self._stop.is_set():
            return
        try:
            results, model_name = self._check()
        except Exception:
            return  # 检查本身出错（磁盘等）：等下一轮再看。
        model_name = model_name or "whisper"
        llmwiki_needed = self._llmwiki_needed()
        with self._lock:
            to_download = []
            for kind in self.KINDS:
                entry = self._state[kind]
                entry["model"] = model_name if kind == "asr_model" else None
                if kind == "llmwiki" and not llmwiki_needed:
                    entry.update(state="not_needed", progress=0.0,
                                 error=None, _tried=False)
                    continue
                if results.get(kind):
                    entry.update(state="ready", progress=1.0, error=None,
                                 _tried=False)
                elif kind == "llmwiki" and entry["state"] in ("ready", "not_needed"):
                    # 文件被删除或校验不符时，不能沿用上次 ready 而提前返回。
                    entry.update(state="downloading", progress=0.0,
                                 error=None, _tried=False)
                    to_download.append(kind)
                elif not entry["_tried"]:
                    # 还没试过（或 retry 重置过）：本轮去下载。
                    to_download.append(kind)
            all_ready = all(self._state[k]["state"] in ("ready", "not_needed")
                            for k in self.KINDS)
        if all_ready:
            self._fire_ready()
            return
        self._ready_fired = False  # 有资源没就绪：允许下次再回调一次。
        for kind in to_download:
            # stop 可能发生在上一个资源下载期间；不能再开始下一个资源。
            if self._stop.is_set():
                return
            if kind == "llmwiki" and not self._llmwiki_needed():
                # 用户可能在本轮检查后、下载开始前切回轻量。必须一并
                # 恢复状态，否则旧检查会暂时把 not_needed 改回 downloading。
                with self._lock:
                    self._state[kind].update(state="not_needed", progress=0.0,
                                             error=None, _tried=False)
                continue
            name = model_name if kind == "asr_model" else None
            with self._lock:
                entry = self._state[kind]
                entry.update(state="downloading", progress=0.0, error=None,
                             _tried=True)
                if kind == "asr_model":
                    from . import assets as assets_module
                    entry["total_bytes"] = assets_module.asset_total_bytes(name)
            try:
                installed = self._install(kind, name, self._make_progress(kind))
                if kind == "llmwiki":
                    # 路径只取可信安装器的返回值，不接受页面提交；沿用
                    # store 的原子更新，下载过程中不会写入半成品路径。
                    from .assets import llmwiki_binary_path
                    binary = installed or llmwiki_binary_path(self.store.root)
                    self.store.update_settings({
                        "llmwiki_binary": str(Path(binary).resolve())})
            except ValueError as error:
                # 只带错误代码（如 assets_network_error），不带 URL 等敏感内容。
                needed = kind != "llmwiki" or self._llmwiki_needed()
                code = str(error)
                if not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", code):
                    code = "assets_error"
                with self._lock:
                    self._state[kind].update(
                        state="failed" if needed else "not_needed",
                        error=code if needed else None)
                continue
            except Exception:
                # 安装抛出的任何异常都要把这一项标成 failed——
                # 否则 _tried 已是 True、状态永远停在 downloading 0%
                # （回调签名不匹配的 TypeError 就是这样卡住的）。错误代码
                # 统一 assets_error，不带原始异常文本（可能含 URL）。
                needed = kind != "llmwiki" or self._llmwiki_needed()
                with self._lock:
                    self._state[kind].update(
                        state="failed" if needed else "not_needed",
                        error="assets_error" if needed else None)
                continue
            if self._stop.is_set():
                return
            still_needed = kind != "llmwiki" or self._llmwiki_needed()
            with self._lock:
                self._state[kind].update(state="ready" if still_needed
                                        else "not_needed", progress=1.0,
                                         error=None)
                if kind == "asr_model":
                    # 刚下载/替换过文件，缓存里的 (mtime, sha) 对不上
                    # 了，清掉让下一轮复查重做一次完整校验。
                    self._verify_cache.clear()
        if self.all_ready():
            self._fire_ready()

    def _make_progress(self, kind: str):
        """构造进度回调：接收一个参数（统一约定），写进状态。

        - ``fraction`` 是 0–1 的比例，收紧到 [0, 1] 后存进 progress；
        - ``None`` 表示"下载中、比例未知"（清单没写压缩包大小等），
          状态里的 progress 也存 None，控制台显示"下载中"而不是 0%。
        """
        def callback(fraction):
            with self._lock:
                entry = self._state[kind]
                if entry["state"] != "downloading":
                    return
                entry["progress"] = (
                    None if fraction is None
                    else min(1.0, max(0.0, fraction)))
        return callback

    def _fire_ready(self):
        """全部就绪时回调一次（Supervisor：清事项 + 自动开跑一次）。"""
        if self._ready_fired or self._stop.is_set():
            return
        self._ready_fired = True
        if self.on_ready is not None:
            try:
                self.on_ready()
            except Exception:
                pass  # 回调失败不影响资源状态（下一轮还会再试）。

    # ---------------- 默认实现（真实环境） ----------------

    def _default_check(self):
        from . import assets as assets_module
        home = self.store.root
        name = self.asr_model_getter()
        results = {"xhs_downloader": assets_module.xhs_downloader_ready(home),
                   "asr_model": self._model_ready_cached(name, home)}
        if self._llmwiki_needed():
            results["llmwiki"] = assets_module.llmwiki_ready(home)
        return results, name

    def _model_ready_cached(self, name: str, home) -> bool:
        """带缓存的语音模型就绪检查。

        每 30 秒的复查只做"轻量检查"：对每个文件 stat，核对**存在**和
        **大小**（和清单一致）。任何一个文件缺失或大小不对，直接判
        "未就绪"——不算 sha256（文件明摆着不完整，没必要把其余 1.6 GB
        再读一遍；后台线程随后会去下载）。

        完整 sha256（调 assets.verify_model，每个文件从头读一遍）只在
        这些情况才做，结果按 (大小, 修改时间) 缓存：
        - 服务启动后第一次检查（缓存为空）；
        - 刚下载/重装过（下载成功后缓存被清空）；
        - 某个文件的修改时间变了（被替换或改动过）。
        所以实际节奏是：启动 → 全量 sha 一次 → 之后只 stat；换模型或
        重新下载后再全量一次。校验没通过（内容坏了但大小对）的结论也
        会被缓存，不会每 30 秒重算。未知模型名返回 False（和
        assets.model_ready 的约定一致，后台线程不崩）。
        """
        from . import assets as assets_module
        if name not in assets_module.MODEL_NAMES:
            return False
        # 从清单取文件清单（模型目录 + 各文件；sensevoice 还有公共 VAD）。
        entry = assets_module.load_manifest()[name]
        models = Path(home) / "models"
        targets = [models / entry["dir"] / filename
                   for filename in entry.get("files", {})]
        if entry.get("vad"):
            targets.append(models / "silero_vad.onnx")
        need_full_verify = False
        for path in targets:
            try:
                stat = path.stat()
            except OSError:
                # 文件不存在：轻量检查就已经不通过，直接判未就绪。
                self._verify_cache.pop(str(path), None)
                return False
            expected_size = _manifest_size(entry, path)
            if expected_size >= 0 and stat.st_size != expected_size:
                # 大小和清单对不上（下载不完整/被截断）：同样直接判未就绪。
                self._verify_cache.pop(str(path), None)
                return False
            cached = self._verify_cache.get(str(path))
            if cached is None or cached[0] != stat.st_size \
                    or cached[1] != stat.st_mtime_ns:
                # 还没完整校验过，或者文件被动过：需要算一次 sha256。
                need_full_verify = True
        if not need_full_verify:
            # 本模型全部文件的 (大小, mtime) 都和缓存一致：沿用上次结论。
            # 只看本模型的文件——缓存里可能还留着另一个模型的条目。
            return all(self._verify_cache[str(path)][2] for path in targets)
        # 做一次完整校验（每个文件从头读一遍算 sha256），按文件记缓存。
        ok, missing = assets_module.verify_model(name, home)
        missing_set = set(missing)
        for path in targets:
            try:
                stat = path.stat()
            except OSError:
                self._verify_cache.pop(str(path), None)
                continue
            self._verify_cache[str(path)] = (
                stat.st_size, stat.st_mtime_ns, path.name not in missing_set)
        return ok

    def _default_install(self, kind, name, progress):
        from . import assets as assets_module
        if kind == "xhs_downloader":
            assets_module.install_xhs_downloader(self.store.root,
                                                 progress=progress,
                                                 downloader=self._curl.download)
        elif kind == "llmwiki":
            return assets_module.install_llmwiki(
                self.store.root, progress=progress,
                downloader=self._curl.download)
        else:
            assets_module.install_model(name, self.store.root,
                                        progress=progress,
                                        downloader=self._curl.download)


# ---------------------------------------------------------------------------
# Supervisor：定时线程 + 子进程管理
# ---------------------------------------------------------------------------

class Supervisor:
    """守护进程的"调度员"：决定什么时候开跑、拉起子进程、收子进程的尾。

    依赖全部可注入：spawn（拉子进程）、now（时钟）、notify（通知）换成
    假的就能不启动任何真实子进程地测完所有判断逻辑（见 tests/test_daemon.py）。
    """

    def __init__(self, store: ServiceStore, *, python_bin=None, spawn=None,
                 now=None, notify=None, assets=None):
        self.store = store
        # 子进程用的解释器：默认跟守护进程同一个（uv 环境下就是 .venv 的 python）。
        self.python_bin = python_bin or sys.executable
        # spawn(args)：拉起一个子进程，返回带 poll() 的句柄。
        self.spawn = spawn or self._default_spawn
        # now()：当前北京时间，测试时可传固定时间。
        self.now = now or now_beijing
        # notify(code)：发一条系统通知（内部自带去重）。
        self.notify = notify or (lambda code: None)
        # 后台资源准备线程（None = 没启用，运行不再被资源状态拦）。
        self.assets = assets

        # 运行子进程的句柄 / 拉起时间 / 触发方式（补 crash 记录用）。
        self._run_proc = None
        self._run_spawned_at = None
        self._run_trigger = None
        # 资源未就绪时被拦下的那次运行：记录触发方式，就绪后自动补跑。
        self._pending_run_trigger = None
        # 扫码子进程句柄 / 上次自动拉起扫码的时间。
        self._login_proc = None
        self._last_login_spawn_at = None
        # 拉起扫码时 needs_user 里有没有 xhs_login：只有"当时有、现在没了"
        # （真的是扫码把登录修好了）才自动开跑。
        self._login_had_xhs_login = False

    # ---------------- 子进程拉起 ----------------

    def _default_spawn(self, args):
        """真实的子进程启动：参数列表（绝不经过 shell），输出追加到守护日志。

        子进程的数据目录由命令行参数 --home 指定（沿用守护进程自己的目录），
        不依赖环境变量，防止被外层环境带偏。日志写在 config.log_dir()：
        测试用 XHS_WIKI_LOG_DIR 指到临时目录，绝不写真实的 ~/Library/Logs。
        """
        log = open(config.log_dir() / "daemon.log", "a", encoding="utf-8")
        proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
        # 子进程已经继承了这个句柄，父进程自己这份立刻关掉：
        # 守护进程常驻几个月，每拉起一个子进程就漏一个句柄。
        log.close()
        return proc

    def run_child_args(self, trigger: str) -> list:
        """运行子进程的完整命令行。

        --home 统一放在子命令前面。放在后面虽然 cli 也已
        兼容（见 cli._build_parser 里关于 --home 的注释），但写在前面是 argparse
        最保险的位置，两处互为双保险：
        [sys.executable, "-I", "-m", "xhs_wiki.cli", "--home", APP_HOME,
         "run", "--trigger", …]

        加 ``-I``（隔离模式）。``python -m`` 默认把当前目录放在
        模块搜索路径最前面，当前目录里与标准库同名的 .py 文件（比如
        secrets.py）会被优先加载；-I 不把当前目录加进去，也忽略
        PYTHONPATH 等环境变量。本项目装在 venv 的 site-packages 里，
        不受影响。
        """
        return [self.python_bin, "-I", "-m", "xhs_wiki.cli",
                "--home", str(self.store.root),
                "run", "--trigger", trigger]

    def login_child_args(self) -> list:
        """扫码子进程的完整命令行：[python, "-I", "-m", "xhs_wiki.cli",
        "--home", APP_HOME, "login"]（--home 放在子命令前；-I 见
        run_child_args 的说明）。"""
        return [self.python_bin, "-I", "-m", "xhs_wiki.cli",
                "--home", str(self.store.root), "login"]

    # ---------------- 状态判断 ----------------

    def is_running(self) -> bool:
        """是否有运行子进程在进行：句柄活着，或状态文件写着 running/pausing。"""
        if self._run_proc is not None and self._run_proc.poll() is None:
            return True
        return self.store.read_status().get("state") in ("running", "pausing")

    def login_active(self) -> bool:
        """扫码子进程是否还在进行。"""
        return self._login_proc is not None and self._login_proc.poll() is None

    def qr_available(self) -> bool:
        """二维码截图文件当前是否存在（扫码流程会原子替换/结束时删除它）。"""
        return (self.store.root / "login-qr.png").exists()

    # ---------------- 动作（HTTP 接口和定时线程共用） ----------------

    def start_run(self, trigger: str) -> bool:
        """拉起一次运行子进程。已在运行就返回 False（不重复拉）。

        启用了资源线程且组件/模型还没就绪时不拉子进程——运行子
        进程一进去也会在下载器上失败——而是登记 needs_user 事项
        assets_not_ready（文案"组件还在下载，完成后会自动开始"），记下
        这次想要的触发方式，资源就绪时由 on_assets_ready 自动补跑一次。
        返回 True 表示"这次运行已受理（等资源就绪后开跑）"。
        """
        if self.is_running():
            return False
        if self.assets is not None and not self.assets.all_ready():
            self.store.add_needs_user("assets_not_ready", "", now=self.now())
            self._pending_run_trigger = trigger
            self.store.write_status(now=self.now(), state="needs_user",
                                    step=None, message=ASSETS_NOT_READY_TITLE)
            return True
        self._run_proc = self.spawn(self.run_child_args(trigger))
        self._run_spawned_at = self.now()
        self._run_trigger = trigger
        return True

    def on_assets_ready(self) -> None:
        """资源全部就绪后的动作（AssetManager.on_ready 回调）：
        清掉 assets_not_ready 事项，把之前被拦下的那次运行自动补跑；
        没有待补跑的运行时，把残留的 needs_user 状态复位成空闲。"""
        self.store.clear_needs_user("assets_not_ready")
        trigger = self._pending_run_trigger
        self._pending_run_trigger = None
        if trigger and not self.is_running():
            self.start_run(trigger)
        elif self.store.read_status().get("state") == "needs_user":
            self.store.write_status(now=self.now(), state="idle", step=None,
                                    message="")

    def assets_state(self) -> dict:
        """/api/status 的 assets 字段：没启用资源线程时按"都已就绪"汇报
        （旧测试和旧安装路径不装资源线程，控制台不该显示进度条）。"""
        if self.assets is not None:
            return self.assets.state()
        state = {kind: {"state": "ready", "progress": 1.0, "error": None,
                        "total_bytes": None, "model": None}
                 for kind in ("xhs_downloader", "asr_model")}
        from .assets import asset_total_bytes, llmwiki_ready
        needed = self.store.load_settings().get("engine") == "llmwiki"
        ready = needed and llmwiki_ready(self.store.root)
        state["llmwiki"] = {
            "state": ("ready" if ready else "downloading") if needed else "not_needed",
            "progress": 1.0 if ready else 0.0, "error": None,
            "total_bytes": asset_total_bytes("llmwiki"), "model": None}
        return state

    def start_login(self) -> bool:
        """拉起扫码登录子进程。

        两种情况拒绝：扫码子进程已在进行（同一时间只允许一个，抢浏览器资料夹）；
        运行子进程正在扫描收藏（step == scan，也占用同一个浏览器资料夹）。
        拉起之前先删掉残留的旧二维码（理由见 remove_stale_login_qr）。
        """
        if self.login_active():
            return False
        if self.store.read_status().get("step") == "scan":
            return False
        remove_stale_login_qr(self.store)
        # 记下拉起那一刻有没有 xhs_login 事项：自动开跑的前提。
        status = self.store.read_status()
        self._login_had_xhs_login = any(
            item.get("code") == "xhs_login" for item in status.get("needs_user", []))
        self._login_proc = self.spawn(self.login_child_args())
        self._last_login_spawn_at = self.now()
        return True

    # ---------------- 启动时的"假运行中"清理 ----------------

    def cleanup_stale_running(self) -> None:
        """守护进程启动时清理假运行中状态。

        状态写着 running/pausing，但 runner.lock 能被非阻塞拿到——说明上次
        服务退出时运行子进程被一起带走了（断电/重启），并没有真的在跑。
        这时把状态改成 error 并提示用户重新运行。检查完立刻释放锁。
        """
        status = self.store.read_status()
        if status.get("state") not in ("running", "pausing"):
            return
        lock_path = self.store.root / "runner.lock"
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                # 锁被占着：真的有运行子进程在跑，什么都不动。
                return
            # 拿到锁说明没有子进程在跑：改状态、发通知（fd 关闭时锁自动释放）。
            self.store.write_status(now=self.now(), state="error", step=None,
                                    message=MESSAGE_STALE_RUNNING)
            self.notify("error")
        finally:
            os.close(fd)

    # ---------------- tick：一次完整检查 ----------------

    def tick(self, now=None) -> None:
        """定时线程的主体：一次"要不要开跑 / 子进程收尾"检查。

        测试直接调用这个方法并传入时间，不依赖真实线程。顺序：先收两个
        子进程的尾 → 每日运行 → 追加运行 → 登录过期自动扫码。
        """
        if now is None:
            now = self.now()
        self._reap_run_child(now)
        self._reap_login_child(now)

        settings = self.store.load_settings()
        scheduler = self.store.read_scheduler()
        paused = self.store.pause_requested()
        running = self.is_running()

        # 每日定时运行：先把"今天已跑"写盘再开跑，同一天不会跑第二次。
        if due_daily(settings, now, scheduler.get("last_scheduled_date"),
                     running, paused):
            self.store.write_scheduler(last_scheduled_date=now.date().isoformat())
            self.start_run("schedule")

        # 追加运行：上一次结果 incomplete 且距结束 ≥ 60 分钟，每天最多 6 次。
        runs = self.store.list_runs(1)
        last_run = runs[0] if runs else None
        if due_followup(last_run, now, scheduler.get("followup_date"),
                        scheduler.get("followups", 0), self.is_running(), paused):
            if self.start_run("followup"):
                todays = (scheduler.get("followups", 0)
                          if scheduler.get("followup_date") == now.date().isoformat()
                          else 0)
                self.store.write_scheduler(followup_date=now.date().isoformat(),
                                           followups=todays + 1)

        # 登录过期自动拉起扫码：6 小时内不重复；扫码进行中、
        # 运行子进程正在扫描（占用浏览器）时不拉。
        status = self.store.read_status()
        if any(item.get("code") == "xhs_login" for item in status.get("needs_user", [])):
            last = self._last_login_spawn_at
            if (not self.login_active()
                    and status.get("step") != "scan"
                    and (last is None
                         or now - last >= timedelta(hours=LOGIN_AUTORETRY_HOURS))):
                self.start_login()

    # ---------------- 子进程收尾 ----------------

    def _reap_run_child(self, now) -> None:
        """运行子进程退出后的检查。

        子进程正常结束时 runner（cli run）会写最终运行记录。如果最新一条
        记录的 started_at 早于"拉起时间减 5 秒"，说明这条是以前的、本次
        子进程没写记录就崩了：补一条 result=error 的记录、状态改成 error、
        发通知。补的记录只有数字和代码，不含任何链接或敏感内容。
        """
        if self._run_proc is None or self._run_proc.poll() is None:
            return  # 没有子进程，或还没退出。
        self._run_proc = None
        spawned_at = self._run_spawned_at
        trigger = self._run_trigger or "manual"
        self._run_spawned_at = None
        self._run_trigger = None

        wrote_record = False
        runs = self.store.list_runs(1)
        if runs and runs[0].get("started_at"):
            from datetime import datetime
            try:
                started = datetime.fromisoformat(runs[0]["started_at"])
            except ValueError:
                started = None
            if (started is not None
                    and spawned_at is not None
                    and started >= spawned_at - timedelta(seconds=CRASH_GRACE_SECONDS)):
                wrote_record = True
        if wrote_record:
            return

        # 没写记录：补一条 error（字段形状与 runner._base_record 对齐）。
        record = {
            "run_id": f"svc-{now:%Y%m%d-%H%M%S}",
            "trigger": trigger,
            "started_at": spawned_at.isoformat() if spawned_at else now.isoformat(),
            "ended_at": now.isoformat(),
            "result": "error",
            "stop_reason": "runner_crashed",
            "message": MESSAGE_RUNNER_CRASHED,
            "scan": {"visible": None, "nominal": None, "incomplete": False},
            "new_found": 0,
            "processed": {},
            "counts": {},
            "cleanup": {"ran": False, "notes": 0, "freed_bytes": 0, "skipped": 0},
        }
        self.store.append_run(record)
        self.store.write_status(now=now, state="error", step=None,
                                message=MESSAGE_RUNNER_CRASHED)
        self.notify("error")

    def _reap_login_child(self, now) -> None:
        """扫码子进程退出后的检查：登录真的修好了才自动开跑。

        自动开跑的前提：拉起扫码时 needs_user 里确实有 xhs_login（说明是
        "登录过期 → 扫码修好"这条线），现在事项没了，而且设置里已经能读到
        profile_url（由 login 子命令写入）。主页地址也从
        设置里读——守护进程不解析扫码输出，只信落盘的数据。
        """
        if self._login_proc is None or self._login_proc.poll() is None:
            return
        self._login_proc = None
        # 子进程退出后再删一次二维码截图（正常退出时扫码流程自己会删，
        # 进程被强制结束就来不及删，这里兜底收尾）。
        remove_stale_login_qr(self.store)
        had_xhs_login = self._login_had_xhs_login
        self._login_had_xhs_login = False
        if not had_xhs_login:
            return  # 拉起时就没有登录事项：扫码不是在修登录，不开跑。
        status = self.store.read_status()
        if any(item.get("code") == "xhs_login" for item in status.get("needs_user", [])):
            return  # 还需要登录：扫码没成功（超时/被拦），不开跑。
        # 从设置里读 login 子命令写回的主页地址；读不到就不开跑（还没登录）。
        if not self.store.load_settings().get("profile_url"):
            return
        if self.is_running():
            return
        self.start_run("after_login")

    # ---------------- 后台线程 ----------------

    def start(self) -> None:
        """启动定时线程（每 30 秒一次 tick）。"""
        thread = threading.Thread(target=self._tick_loop, name="supervisor-tick",
                                  daemon=True)
        thread.start()

    def _tick_loop(self):
        while True:
            time.sleep(TICK_SECONDS)
            try:
                self.tick()
            except Exception:
                # 定时线程绝不能因为一次检查出错就死掉；错误留给状态/日志体现。
                continue


# ---------------------------------------------------------------------------
# 知识库统计（kb 字段的数据来源，带 30 秒缓存）
# ---------------------------------------------------------------------------

def count_files(directory: Path) -> int:
    """数一个目录下的文件个数（不含目录本身；目录不存在返回 0）。

    只用 os.scandir / entry.is_file，绝不读文件内容；读不了的目录按 0 算。
    """
    directory = Path(directory)
    try:
        if not directory.is_dir():
            return 0
    except OSError:
        # 上级目录没有访问权限时 is_dir 会抛异常，按 0 算。
        return 0
    total = 0
    stack = [directory]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def kb_stats(kb_root, ledger_counts: dict, engine: str = "markdown") -> dict:
    """统计知识库：主题数、来源数（数目录下的文件）、暂缓数（台账的 deferred）。

    主题/来源是引擎写到磁盘上的 Markdown 文件，直接数文件；"暂缓"的笔记
    在台账里是 deferred 阶段（它的来源页混在来源目录里，靠文件分不出来），
    所以暂缓数取台账统计——台账是阶段的权威记录，也不用读文件内容。

    两种引擎的目录结构不一样，按 ``engine`` 分开数：轻量引擎数 主题/ 和
    来源/；LLM Wiki 引擎没有这两个目录，交给它自己的 page_counts 按
    wiki/ 下的真实结构数（以前这里只认轻量引擎的目录，选 LLM Wiki 时
    主题数、来源数一直是 0）。
    """
    root = Path(kb_root).expanduser()
    if engine == "llmwiki":
        from .engines.llmwiki.project import page_counts
        pages = page_counts(root)
    else:
        pages = {"topics": count_files(root / "主题"),
                 "sources": count_files(root / "来源")}
    return {
        "topics": pages["topics"],
        "sources": pages["sources"],
        "deferred": int(ledger_counts.get("deferred", 0)),
    }


# ---------------------------------------------------------------------------
# HTTP 服务：ServiceServer / ServiceHandler
# ---------------------------------------------------------------------------

class ServiceServer(ThreadingHTTPServer):
    """控制台的 HTTP 服务：持有 Supervisor、CSRF 令牌和各可注入依赖。

    可注入项（测试全部换成假实现，不碰真实钥匙串/模型/文件系统）：
    - ledger_factory：() -> Ledger（台账，读 counts 和失败列表）；
    - keychain：钥匙串封装（load 查 Key 是否已存、save 存 Key）；
    - make_provider：(settings) -> Provider（test-llm 用）；
    - make_engine：(settings) -> 引擎（setup/engine 的健康检查用）；
    - opener：(参数列表) -> 用系统命令打开（open-kb 用，只允许打开 kb_root）；
    - disk_usage / clock / cache_seconds：磁盘查询、单调时钟、缓存时长；
    - skills_root / skill_roots / tool_home：查询 Skill 的安装目录（前者是
      Claude Code 的，后者按位置名逐个改写）和检测 AI 工具用的主目录。
    """

    daemon_threads = True

    def __init__(self, address, *, supervisor: Supervisor,
                 console_path=None, ledger_factory=None, keychain=None,
                 make_provider=None, make_engine=None, opener=None,
                 disk_usage=None, clock=None, cache_seconds=CACHE_SECONDS,
                 skills_root=None, skill_roots=None, tool_home=None,
                 token=None):
        self.supervisor = supervisor
        # 控制台页面随包分发（src/xhs_wiki/web/console.html）。
        self.console_path = Path(console_path if console_path is not None
                                 else Path(__file__).resolve().parent
                                 / "web" / "console.html")
        # 台账连接"每个请求用完就关闭"。原实现按线程缓存连接
        # （threading.local），而 HTTP 服务每个请求一个新线程——线程结束
        # 后连接要等垃圾回收才释放，实测句柄会累积（曾见过 38 个
        # ledger.sqlite3 句柄）。现在请求路径一律用 ledger_scope()，
        # 打开 → 用 → 关，绝不多留。
        self._ledger_factory = ledger_factory or (lambda: Ledger(config.ledger_path()))
        self.keychain = keychain or self._default_keychain
        self.make_provider = make_provider or self._default_make_provider
        self.make_engine = make_engine or self._default_make_engine
        # opener：默认用系统的 /usr/bin/open 在访达里选中知识库目录
        # （仅 open-kb 一个入口会调，参数由 _post_open_kb 组好）。
        self.opener = opener or (lambda args: subprocess.run(
            args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        self.disk_usage = disk_usage or shutil.disk_usage
        # ---------------- 慢接口缓存 ----------------
        self.clock = clock or time.monotonic
        self.cache_seconds = cache_seconds
        self._cache_lock = threading.Lock()
        # 健康检查会启动后台，可能比下一次选择更晚结束。用递增代次
        # 区分请求，只允许最新一次选择落库，锁不覆盖耗时健康检查。
        self._engine_selection_lock = threading.Lock()
        self._engine_selection_serial = 0
        self._kb_cache = None
        # CSRF 令牌：32 字节随机数的十六进制，只放在内存里，重启就换。
        self.csrf_token = secrets.token_hex(32)
        # 控制台访问令牌。服务启动时确保 APP_HOME/console-token
        # 存在（32 字节随机 urlsafe，0600，重启沿用）。所有路由（除
        # /healthz 和 /auth）都要带着它换来的 Cookie 才能访问——本机
        # 其他账户和沙盒 App 都能连回环端口，光靠 Host/Origin/CSRF 挡不住
        # 它们（那些头都是请求方自己填的）。测试可注入固定值。
        self.console_token = token if token is not None else \
            console_auth.ensure_token(supervisor.store.root)
        # 跳转页里放的是一次性登录码，这里登记"已经用过的码"，
        # 保证每张码只能换一次 Cookie（见 console_auth.LoginCodeBook）。
        self.login_codes = console_auth.LoginCodeBook()
        # 同时处理的连接数上限（见 MAX_CONCURRENT_CONNECTIONS）。
        self._connection_slots = threading.BoundedSemaphore(
            MAX_CONCURRENT_CONNECTIONS)
        # Skill 的安装位置（默认 ~/.claude/skills；测试注入临时目录，
        # 绝不在测试里碰真实 ~/.claude）。
        self.skills_root = skills_root
        # 其他 AI 工具的技能目录（位置名 → 目录，见 skill.TARGET_ROOTS）
        # 和检测工具用的主目录；None 表示用默认值，测试注入临时目录，
        # 绝不碰真实的 ~/.workbuddy、~/.agents。
        self.skill_roots = dict(skill_roots or {})
        self.tool_home = tool_home
        # 写 Skill 的锁：改勾选的接口要"存设置 + 按新设置写文件"一气呵成，
        # 两次快速保存交错时，后写的必须是最新的设置。可重入：接口拿着锁
        # 再调 sync_skill 不会把自己卡住。
        self._skill_lock = threading.RLock()
        super().__init__(address, ServiceHandler)

    def skill_target_roots(self) -> dict:
        """sync / targets_state 用的目录改写表（没改写的位置用默认目录）。"""
        roots = dict(self.skill_roots)
        if self.skills_root is not None:
            roots["claude_code"] = self.skills_root
        return roots

    # ---------------- 连接数上限 ----------------

    def process_request(self, request, client_address):
        """每个新连接先占一个名额；名额用完时直接关掉这个连接。

        ThreadingHTTPServer 默认来一个连接就开一个线程、占一个文件句柄，
        没有上限。配合 ServiceHandler 的读写超时，同机程序再也没法靠
        攒几百个空闲连接把服务的句柄耗光（那时连 /healthz 都连不上）。
        """
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        """处理线程结束（不论成功、超时还是出错）时归还名额。"""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()

    def handle_error(self, request, client_address):
        """处理请求时的未预期异常：只记异常类型，不打印完整堆栈。

        默认实现会把整段堆栈打到 stderr（LaunchAgent 下就进了日志），
        堆栈里可能带路径。读写超时属于正常情况（对方只连不发），不记。
        """
        error = sys.exc_info()[1]
        if isinstance(error, (TimeoutError, ConnectionError)):
            return
        logger.warning("request_failed:%s", type(error).__name__)

    # ---------------- Skill 同步 ----------------

    def sync_skill(self, settings: dict, withdraw=()) -> dict:
        """按当前设置更新查询 Skill（skill.sync），返回各位置的结果代码。

        什么时机调用：首次设置完成、修改 kb_root、engine 或要装进的
        AI 工具之后——Skill 正文里写死了知识库路径和引擎，改了设置不更新，
        AI 就会去旧路径找知识库。勾选的位置写入；``withdraw`` 是用户刚取消
        勾选的位置，撤掉本工具写过的文件（只有改勾选的接口会传）。失败只
        体现在返回的结果代码里（磁盘满/权限）：Skill 旧一点不影响流水线，
        不该让设置接口整体失败。
        """
        from . import skill
        with self._skill_lock:
            try:
                return skill.sync(settings, roots=self.skill_target_roots(),
                                  withdraw=withdraw)
            except (OSError, ValueError, KeyError):
                # 只带稳定错误意图，不把异常信息（可能含路径）写进任何地方。
                return {}

    # ---------------- 可注入依赖的默认实现 ----------------

    @staticmethod
    def _default_keychain():
        """默认钥匙串封装：llm.keys.Keychain（测试必须注入假的替换）。"""
        from .llm.keys import Keychain
        return Keychain()

    def _default_make_provider(self, settings):
        """默认的 Provider 工厂：按设置创建提供方（API 类的 Key 从钥匙串读）。"""
        from .llm import make_provider
        chain = self.keychain() if callable(self.keychain) else self.keychain
        provider_name = settings["llm"]["provider"]

        def key_provider():
            return chain.load(provider_name)

        return make_provider(settings["llm"], key_provider=key_provider)

    def _default_make_engine(self, settings):
        """复用 runner 的公共工厂；健康检查不需要构造或调用真实模型。"""
        from .engines import make_engine
        return make_engine(settings, None, self.supervisor.store.root)

    # ---------------- 台账（每个请求用完就关闭） ----------------

    @contextmanager
    def ledger_scope(self):
        """台账连接的上下文管理器：打开 → 用 → 一定关闭。

        为什么不再按线程缓存：ThreadingHTTPServer 每个请求一个
        新线程，线程结束后缓存的连接要等垃圾回收才释放，句柄会一直
        累积；改成每个请求里 ``with server.ledger_scope() as ledger:``
        用完立即关闭，连接数不会增长。同一个数据库文件的多个连接靠
        SQLite 自己的锁排队（WAL + busy_timeout），并发安全不受影响。
        """
        ledger = self._ledger_factory()
        try:
            yield ledger
        finally:
            ledger.close()

    # ---------------- 缓存的取数方法 ----------------

    def cached_kb_stats(self, kb_root, ledger_counts: dict,
                        engine: str = "markdown") -> dict:
        """带缓存地取知识库统计（30 秒内不重算）。engine 决定按哪种目录结构数。"""
        return self._cached("_kb_cache",
                            lambda: kb_stats(kb_root, ledger_counts, engine))

    def _cached(self, attr, compute):
        """通用缓存逻辑，三个规则：
        1. 缓存没过期 → 直接返回缓存值；
        2. 从来没有缓存 → 拿住锁同步算一次（并发时只有一个线程真正去算）；
        3. 缓存过期 → 试着拿锁重算；拿不到锁说明别的线程在算，返回稍旧的旧值。
        """
        entry = getattr(self, attr)
        if entry is not None and self.clock() - entry[0] < self.cache_seconds:
            return entry[1]
        if entry is None:
            with self._cache_lock:
                entry = getattr(self, attr)  # 双检：可能别的线程刚算完。
                if entry is None:
                    value = compute()
                    setattr(self, attr, (self.clock(), value))
                    return value
                return entry[1]
        if self._cache_lock.acquire(blocking=False):
            try:
                value = compute()
                setattr(self, attr, (self.clock(), value))
                return value
            finally:
                self._cache_lock.release()
        return entry[1]


class ServiceHandler(BaseHTTPRequestHandler):
    """请求处理：GET 检查 Host/Origin；POST 在此之上再校验 CSRF 令牌、
    Content-Type、Origin 和请求体大小（五道校验，一道不过就不执行动作）。"""

    # 每个连接的读写超时（秒）。BaseHTTPRequestHandler 会在
    # 建立连接时把它设到套接字上：对方 10 秒不发数据（只连不发、或请求体
    # 发一半就停），读操作就超时，连接被关掉、线程和句柄随之释放。
    # 只限制"等对方收发数据"的时间，不限制接口自己的处理时间（测试模型
    # 连接这类慢操作不受影响）。
    timeout = REQUEST_TIMEOUT_SECONDS

    def log_message(self, *_):
        # 不往 stderr 打访问日志（LaunchAgent 下会一直追加 daemon.log）。
        pass

    # ---------------- 通用小工具 ----------------

    def _allowed_hosts(self):
        port = self.server.server_port
        return {"127.0.0.1:" + str(port), "localhost:" + str(port)}

    def _host_origin_ok(self) -> bool:
        """Host / Origin 检查：Host 必须是本机地址加当前端口；Origin 如果
        带了（GET 可能不带），必须是本服务的 http 地址。"""
        allowed = self._allowed_hosts()
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if host not in allowed:
            return False
        if origin and origin not in {"http://" + h for h in allowed}:
            return False
        return True

    def _send_body(self, status: int, body: bytes, content_type: str):
        """带安全响应头发送一段内容。"""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; connect-src 'self'; "
                         "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict):
        self._send_body(status,
                        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8")

    def _read_body(self, length: int):
        """按声明的长度读请求体；对方迟迟不发完（读超时）或断开时返回 None。

        配合类属性 timeout，声明了长度却不把请求体发完的连接
        会在超时后被关掉，而不是一直占着处理线程。读失败时什么都不回复
        （对方已经不在了），直接标记关闭连接。
        """
        if length <= 0:
            return b""
        try:
            return self.rfile.read(length)
        except (TimeoutError, OSError):
            self.close_connection = True
            return None

    def _cookie_ok(self) -> bool:
        """检查请求有没有带有效的控制台访问 Cookie。

        Cookie 由 POST /auth 用访问令牌换来（HttpOnly、SameSite=Strict），
        名字带端口（console_auth.cookie_name）。用恒定时间比较
        （console_auth.tokens_match）——普通 == 的比较耗时随不匹配位置
        变化，理论上能被计时逐位猜出令牌；它还能扛住请求方故意塞进来的
        非 ASCII 字符（compare_digest 直接比字符串会抛异常）。
        """
        expected_name = console_auth.cookie_name(self.server.server_port)
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == expected_name and console_auth.tokens_match(
                    value, self.server.console_token):
                return True
        return False

    def _send_healthz(self, query: str):
        """GET /healthz：探活接口，不需要 Cookie，不含任何数据。

        - 不带参数（install.sh 用）：只回 {"ok": true}；
        - 带 ``challenge=<32–128 位十六进制>``（xhs-wiki open 用）：额外回
          ``proof``——用访问令牌对"本服务实际监听的端口 + 挑战值"算的 HMAC。
          它证明"我是持有同一个令牌、并且就在这个端口上的那份服务"，本身
          推不出令牌（原理见 console_auth 顶部）。端口取服务自己绑定的
          端口，不取请求头里的任何值——转发者没法让真服务按别的端口作答。
        """
        payload = {"ok": True}
        challenge = (parse_qs(query).get("challenge") or [""])[0]
        if console_auth.CHALLENGE_RE.fullmatch(challenge):
            payload["proof"] = console_auth.healthz_proof(
                self.server.console_token, challenge,
                self.server.server_port)
        self._send_json(200, payload)

    # ---------------- GET ----------------

    def do_GET(self):
        if not self._host_origin_ok():
            self.send_error(403)
            return
        parts = urlsplit(self.path)
        route = parts.path
        # /healthz：探活用，不含任何数据，不需要 Cookie。
        if route == "/healthz":
            self._send_healthz(parts.query)
            return
        # /auth 的中转页（POST /auth 成功后 303 到这里）：不含任何数据，
        # 不需要 Cookie，由本站自己再跳到 / ——这一跳才会带上 Strict Cookie
        # （为什么必须中转，见 console_auth.AUTH_BOUNCE_PAGE 的说明）。
        if route == "/auth":
            self._send_body(200, console_auth.AUTH_BOUNCE_PAGE,
                            "text/html; charset=utf-8")
            return
        # 其余所有路由（包括 GET / 和 /api/login-qr.png）都要验 Cookie。
        if not self._cookie_ok():
            if route in ("/", "/index.html"):
                # 提示页：不含 CSRF 令牌、不含任何数据；不在默认
                # 端口时写出带 --port 的完整命令。
                self._send_body(200, no_auth_page(self.server.server_port),
                                "text/html; charset=utf-8")
            else:
                self._send_json(403, {"error": "console_auth_required"})
            return
        if route in ("/", "/index.html"):
            # 控制台页面：把占位符换成本次启动的 CSRF 令牌。
            html = self.server.console_path.read_text(encoding="utf-8")
            self._send_body(200, html.replace("{{CSRF_TOKEN}}",
                                              self.server.csrf_token).encode("utf-8"),
                            "text/html; charset=utf-8")
        elif route == "/api/status":
            self._send_json(200, build_status(self.server))
        elif route == "/api/login-qr.png":
            qr_path = self.server.supervisor.store.root / "login-qr.png"
            if self.server.supervisor.login_active() and qr_path.exists():
                self._send_body(200, qr_path.read_bytes(), "image/png")
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    # ---------------- POST /auth：用访问令牌换 Cookie ----------------

    def _post_auth(self):
        """POST /auth：校验表单里的访问令牌，通过后下发 Cookie 并跳转。

        - 表单来自 ``xhs-wiki open`` 生成的本地文件（0600），提交时
          Origin 是 null，所以这个路由**不做** Origin 校验——它的安全
          完全靠令牌本身（0600 文件 + 恒定时间比较）；
        - 通过：HttpOnly + SameSite=Strict + Path=/、有效期 1 年的
          Cookie，再用 303 跳到中转页 GET /auth（不带任何参数），由中转页
          跳到 /。不直接 303 到 /：从本地文件提交过来的这一串跳转按浏览器
          规则算"跨站"，紧跟着的请求不会带 Strict Cookie（实测确认），
          用户会先看到"请运行 xhs-wiki open"的提示页；
        - 失败：403。

        两种凭据：
        - ``nonce`` + ``expires`` + ``code``：``xhs-wiki open`` 写进跳转页的
          一次性登录码（2 分钟有效、只能用一次、只认本服务的端口，见
          console_auth.LoginCodeBook）。跳转页里不再有主令牌；登录成功后
          顺手删掉跳转页文件；
        - ``token``：主令牌本身（手里有令牌文件的人本来就能做任何事，
          保留它方便排查问题时手动登录）。
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            self._send_json(400, {"error": "content_length_invalid"})
            return
        raw = self._read_body(length)
        if raw is None:
            return
        fields = {name: values[0] for name, values in
                  parse_qs(raw.decode("utf-8", "replace")).items() if values}
        token = fields.get("token", "")
        if token:
            accepted = console_auth.tokens_match(token,
                                                 self.server.console_token)
        else:
            accepted = self.server.login_codes.redeem(
                self.server.console_token, self.server.server_port, fields)
            if accepted:
                console_auth.remove_open_page(self.server.supervisor.store.root)
        if not accepted:
            self._send_json(403, {"error": "console_auth_failed"})
            return
        cookie = (f"{console_auth.cookie_name(self.server.server_port)}="
                  f"{self.server.console_token};"
                  f" Path=/; Max-Age={console_auth.COOKIE_MAX_AGE};"
                  " HttpOnly; SameSite=Strict")
        self.send_response(303)
        self.send_header("Location", "/auth")
        self.send_header("Set-Cookie", cookie)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------------- POST：前六道校验 ----------------

    def do_POST(self):
        route = urlsplit(self.path).path
        # /auth 特殊：来自本地文件的表单提交 Origin 是 null，不做 Origin
        # 校验，只查 Host；安全完全靠令牌本身（见 _post_auth）。
        if route == "/auth":
            if self.headers.get("Host", "") not in self._allowed_hosts():
                self._send_json(403, {"error": "host_or_origin_rejected"})
                return
            self._post_auth()
            return
        # 第一关：Host / Origin。
        if not self._host_origin_ok():
            self._send_json(403, {"error": "host_or_origin_rejected"})
            return

        # 第二关：访问 Cookie。没有它，任何 POST 一律 403（连"这个
        # 接口存不存在"都不告诉对方）——否则同机其他账户可以拿到 CSRF
        # 令牌后伪造 Origin 调接口。
        if not self._cookie_ok():
            self._send_json(403, {"error": "console_auth_required"})
            return
        if route not in POST_ROUTES:
            self.send_error(404)
            return

        # 第三关：POST 必须带 Origin 且匹配（GET 允许省略，POST 不允许）。
        allowed = self._allowed_hosts()
        origin = self.headers.get("Origin")
        if not origin or origin not in {"http://" + h for h in allowed}:
            self._send_json(403, {"error": "origin_missing_or_mismatched"})
            return
        # 第四关：CSRF 令牌。tokens_match 是"恒定时间"比较——普通 == 的
        # 比较耗时随不匹配位置变化，理论上能被计时猜令牌。
        token = self.headers.get("X-CSRF-Token", "")
        if not console_auth.tokens_match(token, self.server.csrf_token):
            self._send_json(403, {"error": "csrf_token_invalid"})
            return
        # 第五关：Content-Type 必须是 application/json。
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";")[0].strip().lower() != "application/json":
            self._send_json(400, {"error": "content_type_invalid"})
            return
        # 第六关：请求体不超过 4 KB（超了连读都不读，直接拒绝）。
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "content_length_invalid"})
            return
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            self._send_json(400, {"error": "payload_too_large"})
            return
        raw = self._read_body(length) if length > 0 else b"{}"
        if raw is None:
            return
        try:
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "body_not_json_object"})
            return

        self.dispatch_post(route, body)

    # ---------------- POST：各路由的执行 ----------------

    def dispatch_post(self, route: str, body: dict):
        """五道校验全部通过后，按路由执行动作。"""
        supervisor = self.server.supervisor
        store = supervisor.store

        if route == "/api/run-now":
            if supervisor.is_running():
                self._send_json(409, {"error": "already_running"})
                return
            supervisor.start_run("manual")
            self._send_json(200, {"ok": True})

        elif route == "/api/pause":
            store.request_pause()
            self._send_json(200, {"status": store.read_status()})

        elif route == "/api/resume":
            store.clear_pause()
            if not supervisor.is_running():
                supervisor.start_run("resume")
            self._send_json(200, {"status": store.read_status()})

        elif route == "/api/settings":
            # 路由层白名单。日常页只会发定时、条数、清理、语音模型
            # 和模型名；其余字段（可执行文件路径、知识库位置、引擎、完成
            # 标记……）一律 400 field_not_editable，settings.json 不动。
            # 知识库位置和引擎只能走向导的专用接口（各自有校验、迁移和
            # Skill 同步），所以这里也不再需要同步 Skill。
            try:
                changes = console_settings_changes(body,
                                                   store.load_settings())
            except ValueError as error:
                field = str(error).partition(":")[2]
                self._send_json(400, {"error": "field_not_editable",
                                      "field": field})
                return
            try:
                store.update_settings(changes)
            except ValueError as error:
                # 校验失败：整体拒绝并给出错误代码（不部分保存）。
                self._send_json(400, {"error": str(error)})
                return
            # 切换了语音模型就立即唤醒资源线程去检查——
            # 新模型缺失时马上开始下载，进度照常显示在顶部进度条。
            if "asr_model" in changes and supervisor.assets is not None:
                wake = getattr(supervisor.assets, "wake_asr", None)
                if callable(wake):
                    wake()
            status = build_status(self.server)
            self._send_json(200, {"ok": True,
                                  "next_run_at": status["next_run_at"],
                                  "settings": status["settings"]})

        elif route == "/api/login":
            if supervisor.login_active():
                self._send_json(200, {"login": {"active": True,
                                                "qr_available": supervisor.qr_available()}})
                return
            if store.read_status().get("step") == "scan":
                self._send_json(409, {"error": "scan_in_progress"})
                return
            supervisor.start_login()
            self._send_json(200, {"login": {"active": True,
                                            "qr_available": supervisor.qr_available()}})

        elif route == "/api/assets/retry":
            manager = supervisor.assets
            if manager is None:
                self._send_json(409, {"error": "assets_not_managed"})
                return
            manager.retry()
            self._send_json(200, {"ok": True, "assets": manager.state()})

        elif route == "/api/setup/kb-root":
            self._post_setup_kb_root(body)

        elif route == "/api/setup/engine":
            self._post_setup_engine(body)

        elif route == "/api/setup/llm":
            self._post_setup_llm(body)

        elif route == "/api/setup/key":
            self._post_setup_key(body)

        elif route == "/api/setup/test-llm":
            self._post_setup_test_llm(body)

        elif route == "/api/setup/first-import":
            self._post_setup_first_import(body)

        elif route == "/api/setup/finish":
            self._post_setup_finish()

        elif route == "/api/setup/restart":
            self._post_setup_restart()

        elif route == "/api/notes/retry":
            self._post_notes_retry(body)

        elif route == "/api/open-kb":
            self._post_open_kb()

        elif route == "/api/skill-targets":
            self._post_skill_targets(body)

    # ---------------- 向导和日常页的各个接口 ----------------

    def _post_skill_targets(self, body: dict):
        """选择把查询 Skill 装进哪些 AI 工具（向导第 6 步和日常页共用）。

        请求体：{"targets": [位置名, ...]}，位置名只能是
        store.SKILL_TARGETS 里的固定名字（claude_code / workbuddy /
        agents），不能带路径之类的自由文本。保存设置后立即按新设置同步：
        勾选的位置写入 Skill；原来勾着、这次取消的位置撤掉本工具写过的
        文件（原来就没勾的位置不碰，那里用户自己放的东西保持原样）。

        返回 {"ok": true, "skill_targets": 各位置状态, "results": {位置:
        结果代码}}。某个位置写入失败（比如目录不可写）不算整个请求失败，
        页面按 results 里的 failed 提示用户。
        """
        from . import skill
        store = self.server.supervisor.store
        targets = body.get("targets")
        with self.server._skill_lock:
            # 改之前勾着哪些：和 skill.sync 用同一个读法（手工改坏的值按默认处理）。
            before = skill.selected_targets(store.load_settings())
            try:
                store.update_settings({"skill_targets": targets})
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            settings = store.load_settings()
            results = self.server.sync_skill(
                settings, withdraw=before - set(settings["skill_targets"]))
        self._send_json(200, {
            "ok": True,
            "skill_targets": skill.targets_state(
                settings, roots=self.server.skill_target_roots(),
                home=self.server.tool_home),
            "results": results,
        })

    def _post_setup_kb_root(self, body: dict):
        """设置知识库位置（向导第 1 步，也是"重新设置"里换位置的入口）。

        步骤：
        1. 统一校验（store.validate_kb_root，和 validate_settings 同一套
           规则）：绝对路径、无控制字符、不是程序包、不在/不含
           APP_HOME、不是主目录或系统目录……不过就 400 + 错误代码；
        2. 先把"改完之后的整份设置"校验一遍，确保后面写设置不会失败
           ——不能出现"内容已经搬走、设置却没改成"的半截状态；
        3. 旧位置有 .xhs-wiki/ 标记且和新位置不同 → 搬过去
           （有运行在进行时 409，新位置非空时 400 kb_root_target_not_empty；
           旧目录里有用户自己的文件时只搬本工具生成的五类内容，
           见 migrate_kb_root）；
        4. 新位置不存在就创建（新建的目录权限 0700，只有本人能进；
           已存在的用户目录不改权限），不可写报 kb_root_not_writable；
        5. 写设置、同步 Skill（Skill 正文里写着知识库路径）。
        返回 {"ok": true, "kb_root": 新路径, "migrated": 是否搬了旧内容,
        "moved": "all"（整个目录）/ "generated"（只搬生成内容）/ null}。
        """
        supervisor = self.server.supervisor
        store = supervisor.store
        try:
            path = validate_kb_root(body.get("path"), app_home=store.root)
            validate_settings({**store.load_settings(), "kb_root": str(path)})
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        old = Path(store.load_settings()["kb_root"]).expanduser()
        migrated = False
        moved = None
        if kb_migration_needed(old, path, app_home=store.root):
            # 运行子进程正在往旧知识库里写：这时搬家会把它写到一半的
            # 内容拆散，等它结束再换。
            if supervisor.is_running():
                self._send_json(409, {"error": "run_in_progress"})
                return
            try:
                moved = migrate_kb_root(old, path)
            except ValueError as error:
                status = 500 if str(error) == "kb_root_move_failed" else 400
                self._send_json(status, {"error": str(error)})
                return
            migrated = True
        try:
            # mode 只作用于这次新建的最后一级目录；中间缺的上级
            # 目录按系统默认权限建，已存在的目录一律不动。
            path.mkdir(parents=True, exist_ok=True, mode=KB_DIR_MODE)
            if not os.access(path, os.W_OK):
                raise OSError("read-only")
        except OSError:
            self._send_json(400, {"error": "kb_root_not_writable"})
            return
        try:
            store.update_settings({"kb_root": str(path)})
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        # 知识库统计缓存里还是旧位置的数字，清掉让下一次 /api/status 重算。
        self.server._kb_cache = None
        # 知识库位置变了，Skill 里写的路径也要跟着更新。
        self.server.sync_skill(store.load_settings())
        self._send_json(200, {"ok": True, "kb_root": str(path),
                              "migrated": migrated, "moved": moved})

    def _post_setup_engine(self, body: dict):
        """先准备后台，再做一次必定收尾的健康检查；通过后才保存候选设置。"""
        engine = body.get("engine")
        if engine not in ("markdown", "llmwiki"):
            self._send_json(400, {"error": "engine_invalid"})
            return
        with self.server._engine_selection_lock:
            self.server._engine_selection_serial += 1
            selection_serial = self.server._engine_selection_serial
        store = self.server.supervisor.store
        manager = self.server.supervisor.assets
        merged = dict(store.load_settings())
        merged["engine"] = engine
        changes = {"engine": engine}
        if engine == "llmwiki":
            from . import assets as assets_module
            if not assets_module.llmwiki_supported():
                self._send_json(400, {"error": "engine_unavailable",
                                      "reason": "llmwiki_unsupported_platform"})
                return
            if not assets_module.llmwiki_ready(store.root):
                # 正常 serve 启动时已有管理器；嵌入调用尚未创建时按需接入。
                if manager is None:
                    manager = AssetManager(store)
                    self.server.supervisor.assets = manager
                    manager.on_ready = self.server.supervisor.on_assets_ready
                    progress = manager.request_llmwiki()
                    manager.start()
                else:
                    progress = manager.request_llmwiki()
                self._send_json(202, {"state": "downloading", "progress": progress})
                return
            # 安装已存在（例如 CLI 先装好）也自动补齐路径，绝不信任请求体。
            changes["llmwiki_binary"] = str(
                assets_module.llmwiki_binary_path(store.root).resolve())
            # 第三步在健康检查之后。旧提供方是 API 时先用默认 Claude
            # 候选配置检查，成功才一起保存，避免用户卡在第二步。
            if merged["llm"]["provider"] not in ("claude_code", "codex"):
                changes["llm"] = dict(DEFAULT_SETTINGS["llm"])
            merged.update(changes)
        healthy, reason = self._check_engine_and_close(merged)
        if not healthy:
            self._send_json(400, {"error": "engine_unavailable",
                                  "reason": str(reason)})
            return
        with self.server._engine_selection_lock:
            if selection_serial != self.server._engine_selection_serial:
                self._send_json(409, {"error": "engine_selection_changed"})
                return
            try:
                store.update_settings(changes)
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            if engine == "markdown" and manager is not None:
                manager.release_llmwiki_request()
            # 引擎换了，知识库统计的数法也跟着换；清掉缓存让下一次 /api/status 重算。
            self.server._kb_cache = None
            # Skill 与设置在同一代次中更新，不让慢请求覆盖新选择。
            self.server.sync_skill(store.load_settings())
        self._send_json(200, {"ok": True, "engine": engine})

    def _check_engine_and_close(self, settings: dict) -> tuple[bool, str]:
        """一次性健康检查，无论正常、不健康或抛错都释放本次引擎。

        轻量引擎没有 close 方法；LLM Wiki 的 close 会安全停掉本次拉起
        的后台，不能让向导测试占住后台的单实例锁。
        """
        from .engines.llmwiki.backend import EngineUnavailable
        engine_obj = None
        try:
            engine_obj = self.server.make_engine(settings)
            return engine_obj.health()
        except EngineUnavailable as error:
            return False, error.code
        except ValueError as error:
            return False, str(error)
        except OSError:
            return False, "engine_unavailable"
        finally:
            close = getattr(engine_obj, "close", None)
            if callable(close):
                close()

    def _post_setup_llm(self, body: dict):
        """设置整理模型：沿用 store 的校验规则（provider 限四种、model 非空、
        base_url 只有 openai_compatible 需要）。codex 的空模型名由页面换成
        "default"（store 要求 model 非空；CodexProvider 把 "default" 当作不传 -m，
        用 Codex 自己的默认模型）。"""
        provider = body.get("provider")
        model = body.get("model")
        base_url = body.get("base_url")
        if (self.server.supervisor.store.load_settings().get("engine") == "llmwiki"
                and provider not in ("claude_code", "codex")):
            self._send_json(400, {"error": "llmwiki_provider_unsupported"})
            return
        if provider not in LLM_PROVIDERS:
            self._send_json(400, {"error": "llm_provider_invalid"})
            return
        if not isinstance(model, str):
            self._send_json(400, {"error": "llm_model_invalid"})
            return
        llm = {"provider": provider, "model": model, "base_url": base_url}
        try:
            self.server.supervisor.store.update_settings({"llm": llm})
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(200, {"ok": True})

    def _post_setup_key(self, body: dict):
        """保存 API Key 进钥匙串。只返回 {"saved": true}：Key 绝不回显、
        不写日志。"""
        provider = body.get("provider")
        key = body.get("key")
        if provider not in ("anthropic", "openai_compatible"):
            # 只有 API 类提供方需要 Key（claude_code/codex 用登录态）。
            self._send_json(400, {"error": "key_provider_invalid"})
            return
        if not isinstance(key, str) or not key.strip():
            self._send_json(400, {"error": "key_invalid"})
            return
        chain = self.server.keychain() if callable(self.server.keychain) else self.server.keychain
        try:
            chain.save(provider, key)
        except LLMError as error:
            # 只报错误代码，绝不把 Key 或命令输出放进响应。
            self._send_json(400, {"error": f"key_save_failed:{getattr(error, 'code', '')}"
                                            .rstrip(":")})
            return
        self._send_json(200, {"saved": True})

    def _post_setup_test_llm(self, body: dict):
        """测试整理模型：调 provider.check()（一次最小调用）。

        anthropic / openai_compatible 会真实发出一次请求、产生极少量费用，
        所以请求体必须带 {"confirm_cost": true}（页面上先让用户确认过），
        否则拒绝执行。
        """
        settings = self.server.supervisor.store.load_settings()
        provider_name = settings["llm"]["provider"]
        if (provider_name in ("anthropic", "openai_compatible")
                and body.get("confirm_cost") is not True):
            self._send_json(400, {"error": "cost_confirmation_required"})
            return
        try:
            provider = self.server.make_provider(settings)
            ok, reason = provider.check()
        except LLMError as error:
            self._send_json(400, {"ok": False,
                                  "reason": getattr(error, "code", "") or "llm_error"})
            return
        except ValueError as error:
            self._send_json(400, {"ok": False, "reason": str(error)})
            return
        self._send_json(200, {"ok": bool(ok), "reason": str(reason)})

    def _post_setup_first_import(self, body: dict):
        """首次导入范围：{"limit": 20|null}。

        null（向导里选"全部收藏"）原样保存成 first_import_limit=null
        ——runner._eligible 本来就把空值当作"不限"；以前 null 会被换成
        上限 1000 保存，"全部收藏"名不副实。整数仍然限制在 1–1000
        （store 的校验）。"""
        limit = body.get("limit", 20)
        if limit is not None and (isinstance(limit, bool)
                                  or not isinstance(limit, int)):
            self._send_json(400, {"error": "first_import_limit_invalid"})
            return
        try:
            self.server.supervisor.store.update_settings({"first_import_limit": limit})
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(200, {"ok": True, "first_import_limit": limit})

    def _post_setup_restart(self):
        """重新走首次设置：只把 setup_completed 置回 false。

        其他设置（知识库位置、引擎、模型、密钥、导入范围等）全部保留——
        向导会照常回填已保存的值，用户改哪步就重存哪步；已整理的知识库
        内容不会被删除。有运行正在进行时返回 409 run_in_progress（运行
        子进程在跑中途改标记会造成状态混乱，等它结束再点）。"""
        supervisor = self.server.supervisor
        if supervisor.is_running():
            self._send_json(409, {"error": "run_in_progress"})
            return
        supervisor.store.update_settings({"setup_completed": False})
        self._send_json(200, {"ok": True})

    def _post_setup_finish(self):
        """完成首次设置：知识库、引擎、模型、登录状态都就绪才允许，然后把
        setup_completed 置真并立即以 trigger=manual 开跑。"""
        store = self.server.supervisor.store
        settings = store.load_settings()
        missing = []
        if (settings.get("engine") == "llmwiki"
                and settings["llm"]["provider"] not in ("claude_code", "codex")):
            self._send_json(400, {"error": "llmwiki_provider_unsupported"})
            return

        # 1. 知识库：目录存在且可写。
        kb_root = Path(settings["kb_root"]).expanduser()
        if not kb_root.is_dir() or not os.access(kb_root, os.W_OK):
            missing.append("kb_root")
        # 2. 引擎：health() 通过。
        healthy, _reason = self._check_engine_and_close(settings)
        if not healthy:
            missing.append("engine")
        # 3. 模型：设置合法；API 类还要求钥匙串里已经有 Key。
        provider_name = settings["llm"]["provider"]
        chain = self.server.keychain() if callable(self.server.keychain) else self.server.keychain
        if provider_name in ("anthropic", "openai_compatible"):
            try:
                chain.load(provider_name)
            except LLMError:
                missing.append("llm_key")
        # 4. 登录：设置里已经识别到主页地址。
        if not settings.get("profile_url"):
            missing.append("login")

        if missing:
            self._send_json(400, {"error": "setup_incomplete",
                                  "missing": missing})
            return
        store.update_settings({"setup_completed": True})
        # 完成首次设置后立即清掉旧的 setup_incomplete 事项——
        # 页面上不该再显示"请先完成首次设置"（之前要等下一次运行收尾）。
        store.clear_needs_user("setup_incomplete")
        # 首次设置完成，安装正式版 Skill（之前是"尚未完成设置"版）。
        self.server.sync_skill(store.load_settings())
        # 资源没就绪时 start_run 会把这次运行拦下（needs_user
        # assets_not_ready），资源就绪后自动开跑；把就绪状态告诉页面，
        # 页面据此提示"组件下载完成后会自动开始导入"。
        assets_ready = (self.server.supervisor.assets is None
                        or self.server.supervisor.assets.all_ready())
        self.server.supervisor.start_run("manual")
        self._send_json(200, {"ok": True, "setup_completed": True,
                              "assets_ready": assets_ready})

    def _post_notes_retry(self, body: dict):
        """失败笔记单条重试：调 ledger.retry（退回失败前的阶段、次数清零）。"""
        note_id = body.get("note_id")
        if not isinstance(note_id, str) or not note_id:
            self._send_json(400, {"error": "note_id_invalid"})
            return
        try:
            # 台账连接随开随关，不在请求线程里缓存。
            with self.server.ledger_scope() as ledger:
                ledger.retry(note_id)
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(200, {"ok": True})

    def _post_open_kb(self):
        """在访达里显示知识库文件夹：只认设置里的 kb_root，不接受任何参数。

        以前直接执行 ``open <kb_root>``——kb_root 若是 .app 包、
        .command 文件或以 - 开头的值，open 会把它当程序启动或当成选项。
        现在三道保险：
        1. 先过统一校验（validate_kb_root，带 APP_HOME），不过就 400，
           **不调用** opener——settings.json 被人直接改坏也拦得住；
        2. resolve 成真实绝对路径（一定以 / 开头，不可能被当成选项），
           并且必须是已经存在的目录，否则 400 kb_root_missing；
        3. 用 ``/usr/bin/open -R``：只在访达里选中这个目录，永远不会
           启动任何程序；open 写绝对路径，不按 PATH 查找。
        """
        store = self.server.supervisor.store
        settings = store.load_settings()
        try:
            path = validate_kb_root(settings.get("kb_root"),
                                    app_home=store.root)
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        try:
            resolved = path.resolve()
            is_dir = resolved.is_dir()
        except OSError:
            # 所在目录没有访问权限时明确报错，而不是让请求崩掉。
            self._send_json(400, {"error": "kb_root_inaccessible"})
            return
        if not is_dir:
            # 首次整理前目录可能还没建出来（向导会建，但用户可能删了）。
            self._send_json(400, {"error": "kb_root_missing"})
            return
        self.server.opener([OPEN_BIN, "-R", str(resolved)])
        self._send_json(200, {"ok": True})


def _safe_disk_usage(disk_usage_fn, path):
    """查磁盘剩余：目录还不存在（首次设置前默认的 ~/XHS-Wiki 可能没建）时，
    往上找第一个存在的祖先目录查；连查询都失败就返回 None（页面显示 —）。

    exists() 遇到"没有访问权限"会抛 PermissionError（它只吞
    "不存在"一类的错误），以前会让整个 /api/status 崩掉、页面什么都看
    不到。现在任何 OSError 都按"查不到"处理。
    """
    try:
        path = Path(path).expanduser()
        while not path.exists() and path.parent != path:
            path = path.parent
        return disk_usage_fn(path)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# /api/status 的组装
# ---------------------------------------------------------------------------

def build_status(server: ServiceServer) -> dict:
    """组装 /api/status 的 JSON。

    红线自查：settings.profile_url 是规范主页链接（不带 xsec_token），允许
    出现；Key 只出现 key_saved 布尔值；failed_notes 只带编号/标题/阶段/错误
    代码——台账的普通查询天然不含签名链接。
    """
    supervisor = server.supervisor
    store = supervisor.store
    now = supervisor.now()
    settings = store.load_settings()
    scheduler = store.read_scheduler()
    nxt = next_run_at(settings, now, scheduler.get("last_scheduled_date"))
    usage = _safe_disk_usage(server.disk_usage, settings["kb_root"])

    # 台账连接随开随关（ledger_scope），绝不在请求线程里缓存。
    with server.ledger_scope() as ledger:
        counts = ledger.counts()
        failed_notes = [
            {
                "note_id": note["note_id"],
                "title": note.get("title"),
                "stage": note.get("stage"),
                "error": note.get("last_error"),
            }
            for note in ledger.all_notes()
            if str(note.get("stage", "")).startswith(FAILED_PREFIX)
        ][:FAILED_NOTES_LIMIT]

    # Key 是否已存：只查"能不能读到"，读到的值立刻丢弃，绝不进响应。
    keychain = server.keychain() if callable(server.keychain) else server.keychain
    provider_name = settings["llm"]["provider"]
    key_saved = False
    if provider_name in ("anthropic", "openai_compatible"):
        try:
            keychain.load(provider_name)
            key_saved = True
        except LLMError:
            key_saved = False

    # 步骤代码 → 中文（runner 集中定义，控制台直接复用）。
    from .runner import STEP_LABELS
    from .assets import llmwiki_supported
    from . import skill as skill_module

    return {
        "status": store.read_status(),
        "settings": settings,
        "next_run_at": nxt.isoformat() if nxt else None,
        "runs": store.list_runs(30),
        "step_labels": STEP_LABELS,
        "disk": ({"free_gb": round(usage.free / 1024 ** 3, 1),
                  "total_gb": round(usage.total / 1024 ** 3, 1)}
                 if usage is not None else {"free_gb": None, "total_gb": None}),
        "login": {"active": supervisor.login_active(),
                  "qr_available": supervisor.qr_available()},
        # 后台资源（下载组件 / 语音模型 / 可选 LLM Wiki）的下载进度。
        "assets": supervisor.assets_state(),
        "llmwiki_supported": llmwiki_supported(),
        # 查询 Skill 装进了哪些 AI 工具、本机检测到了哪些工具（向导第 6 步
        # 和日常页显示）。检测只看几个路径存不存在，每 5 秒轮询一次也不费事。
        "skill_targets": skill_module.targets_state(
            settings, roots=server.skill_target_roots(), home=server.tool_home),
        "paused": store.pause_requested(),
        # ---------------- 台账、知识库、首次设置与模型 ----------------
        "counts": counts,
        "kb": server.cached_kb_stats(settings["kb_root"], counts, engine=settings.get("engine")),
        "failed_notes": failed_notes,
        "setup_completed": bool(settings.get("setup_completed")),
        "llm": {
            "provider": provider_name,
            "model": settings["llm"].get("model"),
            "base_url": settings["llm"].get("base_url"),
            "key_saved": key_saved,
        },
        "engine": settings.get("engine"),
        "cleanup_total_bytes": scheduler.get("cleanup_freed_bytes_total", 0),
    }


# ---------------------------------------------------------------------------
# 启动入口（cli serve 子命令调用）
# ---------------------------------------------------------------------------

def serve(port: int = DEFAULT_PORT) -> None:
    """启动守护进程：清理残留状态 → 起 HTTP 服务 → 起定时线程和资源线程。

    serve 同时启动后台资源准备线程（AssetManager）——安装脚本在
    组件和语音模型下载完之前就把控制台打开了，下载由这里接手，进度
    显示在控制台顶部，资源就绪后被拦下的运行自动补跑。

    一进来先把工作目录切到 APP_HOME。服务可能由 LaunchAgent（cwd
    是 ~）或 nohup（继承用户终端的目录，比如没权限的"文稿"）启动，
    所有子进程（Chrome、curl、ffmpeg、claude…）都会继承这个目录，
    轻则行为受影响，重则被 macOS 的 TCC 权限挡住一直卡到超时。
    切不过去（目录不存在/没权限）说明数据目录本身有问题，直接抛错退出。
    """
    try:
        os.chdir(config.app_home())
    except OSError as error:
        # 不带敏感信息地报错退出：目录路径可能出现在 str(error) 里，
        # 但 APP_HOME 是应用自己的数据目录，不涉及用户内容。
        raise RuntimeError(f"serve_chdir_failed: {error.__class__.__name__}") from error
    store = ServiceStore(config.app_home())
    supervisor = Supervisor(store)
    # 启动时清理"假运行中"状态（服务重启/断电留下的）和残留的二维码。
    supervisor.cleanup_stale_running()
    remove_stale_login_qr(store)
    manager = AssetManager(store)
    supervisor.assets = manager
    manager.on_ready = supervisor.on_assets_ready
    server = ServiceServer(("127.0.0.1", port), supervisor=supervisor)

    def request_shutdown(_signum, _frame):
        """退出信号只打断主循环，清理放在 finally，避免信号内争用锁。"""
        raise KeyboardInterrupt

    # 安装脚本停旧服务用 SIGTERM，终端 Ctrl-C 用 SIGINT；两者都必须走
    # 同一清理路径。保留旧处理器，serve 返回后不影响调用方的信号行为。
    previous_handlers = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, request_shutdown)
        print(MESSAGE_SERVING.format(port=server.server_port), flush=True)
        supervisor.start()
        manager.start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # 收尾期间重复收到退出信号不能打断 curl 的终止和 PID 文件清理。
        # serve_forever 已被打断，不在同一线程调用 shutdown（它会死锁）。
        for signum in previous_handlers:
            signal.signal(signum, signal.SIG_IGN)
        try:
            manager.stop()
        finally:
            try:
                server.server_close()
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
