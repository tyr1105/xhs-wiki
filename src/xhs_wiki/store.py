# -*- coding: utf-8 -*-
"""store.py —— 服务目录（设置、状态、运行记录、定时信息、通知时间）的读写层。

这个文件做什么：所有需要落盘的"服务数据"——设置（settings.json）、当前状态
（status.json）、运行记录（runs.jsonl）、定时信息（scheduler.json）、通知发送
时间（notify.json）、暂停标记（PAUSE 文件）——都通过这里的 ServiceStore 类读写。

在整体里的位置：基础层之一，被 runner/daemon/notify 等上层模块使用；
路径由 config.py 给出（root 由调用方传入，测试时传临时目录）。

设置项见 docs/architecture.md §7（kb_root/engine/llm/asr_model/
first_import_limit/setup_completed 等，profile_url 默认 null 且允许 null）。

经过真实环境验证的加固行为（务必不要退化）：
1. 原子写：先写**唯一命名**的临时文件（tempfile.mkstemp，权限 0600），
   fsync 后 os.replace 顶替正式文件；
2. 跨进程锁：每个数据文件配一个从不被替换的 <name>.lock（fcntl.flock），
   "读→改→写"流程都放进锁里，防止守护进程和运行子进程互相覆盖；
3. runs.jsonl 用 os.open 以 0600 一步创建、追加写、整行一次写完；
4. 读设置时用默认值补齐缺少的字段（向前兼容）；
5. 时间统一用北京时间（UTC+8）的带时区 datetime，序列化为 ISO 8601。

术语说明（给非程序员）：
- "原子写"：先写临时文件，写完后一次性改名顶替正式文件。即使写到一半
  程序崩了，正式文件也还是完整的旧内容。
- "flock 锁"：操作系统提供的文件锁，一个进程拿住锁时，别的进程会等到
  它放开再继续，用来避免两个进程同时改同一个文件。
"""

import fcntl
import json
import os
import re
import tempfile
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

# 北京时间固定的 UTC+8 时区（中国不实行夏令时，固定偏移即可）。
BEIJING_TZ = timezone(timedelta(hours=8))


def now_beijing() -> datetime:
    """返回当前的北京时间（带时区的 datetime）。"""
    return datetime.now(BEIJING_TZ)


# ---------------------------------------------------------------------------
# 需要用户处理的事项：代码 → 固定中文标题。
# notify.py 的 TEMPLATES 直接复用这张表，保证两边文案永远一致。
# LLM Wiki 桌面版占用的情况不单列，由 LLM Wiki 引擎在 health() 里报告。
NEEDS_USER_TITLES = {
    "xhs_login": "小红书登录已过期，请在控制台扫码",
    "xhs_blocked": "小红书要求验证或拒绝访问，已暂停",
    "xhs_download_blocked": "连续几条笔记下载失败，可能被平台限制了，已暂停本轮下载；下一次定时运行会再试",
    # 实际门槛是 10 GiB（runner.MIN_FREE_BYTES / download 的
    # min_free_bytes 都是 10 GiB），文案与代码保持一致。
    "disk_low": "磁盘剩余不足 10 GiB，已暂停下载",
    "claude_logged_out": "Claude 命令行未登录",
    "manual_stop_present": "处理循环有手动停止标记",
    "batch_paused": "有批次停下来等你决定",
    "error": "服务运行出错",
    "llm_unavailable": "整理模型不可用，请在控制台检查设置",
    "engine_unavailable": "知识库引擎不可用",
}

# 整理模型（llm.provider）允许的四种提供方：
# - claude_code：本机的 claude 命令行
# - codex：本机的 codex 命令行
# - anthropic：官方 Anthropic API（Key 存钥匙串）
# - openai_compatible：任何兼容 OpenAI 接口的服务（DeepSeek、通义千问等）
LLM_PROVIDERS = ("claude_code", "codex", "anthropic", "openai_compatible")

# 知识库引擎：markdown（轻量引擎，默认）或 llmwiki（LLM Wiki 桌面版）。
ENGINES = ("markdown", "llmwiki")

# 语音识别模型：两个候选都允许（取舍见 docs/architecture.md §5）。
ASR_MODELS = ("sensevoice", "whisper")

# 查询 Skill 可以装进的"安装位置"。每个位置是一个技能目录，同一个目录
# 被几个 AI 工具共用时只算一个位置（目录和依据见 skill.py 的 TARGET_ROOTS）：
# - claude_code：Claude Code 的 ~/.claude/skills；
# - workbuddy：腾讯 WorkBuddy 的 ~/.workbuddy/skills；
# - agents：Codex、Cursor、Gemini CLI、GitHub Copilot 共用的 ~/.agents/skills。
# 放在这里（而不是 skill.py）是因为设置校验要用它，skill.py 反过来要
# 导入本模块；写在一处，两边永远一致。
SKILL_TARGETS = ("claude_code", "workbuddy", "agents")

# 设置文件的默认值（docs/architecture.md §7）。
# 注意：profile_url 默认 null——登录成功后由扫描模块把主页链接写进来。
# asr_model 默认 whisper，与安装脚本默认下载的语音模型一致。
DEFAULT_SETTINGS = {
    "schema_version": 1,
    "schedule_enabled": True,
    "daily_time": "03:00",
    "max_new_per_run": 100,
    "download_retry_every_days": 7,
    "profile_url": None,
    # 扫描默认静默（无窗口）模式——带窗口的 Chrome 会弹到前台打断用户。
    "headless": True,
    # 每天运行结束自动删除"已入库并核对通过"的笔记的下载原件（默认开）。
    "cleanup_enabled": True,
    # 知识库根目录（轻量引擎的输出位置；用户可在控制台改）。
    "kb_root": "~/XHS-Wiki",
    "engine": "markdown",
    # LLM Wiki Desktop 可执行文件的绝对路径（LLM Wiki 引擎用；null =
    # 还没配置，引擎 health() 会报 llmwiki_binary_missing）。
    "llmwiki_binary": None,
    "llm": {"provider": "claude_code", "model": "claude-haiku-5-5", "base_url": None},
    "asr_model": "whisper",  # 默认 Whisper（取舍见 docs/architecture.md §5）
    # 首次导入最多处理多少条旧收藏（首次可能积累了几千条，先小范围试）。
    "first_import_limit": 20,
    "setup_completed": False,
    # 查询 Skill 装进哪些 AI 工具（SKILL_TARGETS 里的位置）。默认只装
    # Claude Code——和加这项设置之前的行为一样；别的工具由用户在向导或
    # 日常页勾选。空列表也合法：用户选了"哪个工具都不装"。
    "skill_targets": ["claude_code"],
}

# 控制台（网页）只允许改这几项设置，其余只读（如 headless、schema_version）。
EDITABLE_SETTINGS_FIELDS = (
    "schedule_enabled",
    "daily_time",
    "max_new_per_run",
    "cleanup_enabled",
    "kb_root",
    "engine",
    "llmwiki_binary",
    "llm",
    "asr_model",
    "first_import_limit",
    "setup_completed",
    "profile_url",
    "skill_targets",
)

# 校验设置用的正则：时间是 00:00–23:59；主页链接是 24 位十六进制编号；
# 模型名只允许字母数字和 ._:/@- 这些常见字符，最长 100——模型名
# 会被写进 LLM Wiki 的 model.conf 并被包装脚本读入，含分号、反引号、
# 换行的值等于往 shell 里塞代码。
# 注意：三个正则都用 fullmatch（整串匹配）来用，而不是 match + "$"——
# Python 正则里的 "$" 允许结尾多一个换行，"gpt-4\n" 用 match 也能通过，
# 换行正好是要挡的字符之一。
_DAILY_TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
_PROFILE_URL_RE = re.compile(r"https://www\.xiaohongshu\.com/user/profile/[0-9a-f]{24}")
_LLM_MODEL_RE = re.compile(r"[A-Za-z0-9._:/@-]{1,100}")

# kb_root 不允许出现的后缀：这些是 macOS 的"包"或可执行文件后缀，
# ``open`` 遇到会直接启动它们而不是在访达里显示。
_KB_ROOT_FORBIDDEN_SUFFIXES = {
    ".app", ".bundle", ".framework", ".plugin", ".kext", ".pkg", ".mpkg",
    ".workflow", ".command", ".terminal",
}

# kb_root 里不允许出现的 Unicode 字符类别（只拦 ASCII 控制
# 字符不够）。unicodedata.category 返回每个字符的"类别代号"：
# - Cc：控制字符，除了常见的换行、制表符，还包括 \x85（NEL，Python 的
#   splitlines() 会当成换行）和 \x9b（CSI，终端转义序列的开头）；
# - Cf：格式字符，比如 U+202E（从右往左显示），能让路径在页面和 SKILL.md
#   里"看起来是另一个样子"；
# - Zl / Zp：行分隔符 U+2028、段分隔符 U+2029，同样会被当成换行。
# 这些字符混进 kb_root，就能借 SKILL.md 向 AI 注入一行"指令"（换行注入的变种）。
_KB_ROOT_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})

# 主目录下 macOS 约定的几个系统文件夹：它们**本身**不能当
# 知识库（里面的子文件夹可以）。原因：知识库根目录会直接放 索引.md、
# 主题/、来源/，混进"文稿""桌面"这类目录的顶层；换位置时还会牵扯到
# 用户自己的大量文件；"公共"文件夹还对同机其他账户开放。
_HOME_STANDARD_FOLDERS = ("Desktop", "Documents", "Downloads", "Movies",
                          "Music", "Pictures", "Public")

# 数据卷的挂载点：/Users、/private 都是从它下面"固件链接"
# （firmlink，系统把数据卷映射到熟悉路径的方式）出来的，它包含所有用户的
# 数据和程序数据目录，绝不能当知识库。
_DATA_VOLUME_ROOT = Path("/System/Volumes/Data")


def kb_root_text_ok(text: str) -> bool:
    """kb_root（或要写进 SKILL.md 的路径）是否只含"安全的可见字符"。

    规则：不能有反引号，也不能有 _KB_ROOT_FORBIDDEN_CATEGORIES 里任何一类
    字符（控制字符、格式字符、行/段分隔符）。skill.render 也用它再挡一道，
    两处规则保持一致。
    """
    return not any(ch == "`"
                   or unicodedata.category(ch) in _KB_ROOT_FORBIDDEN_CATEGORIES
                   for ch in text)


def _resolved(path: Path) -> Path:
    """把路径"解开"：展开符号链接、消掉 ``..``（不存在的尾巴原样保留）。

    为什么需要：``/Users/me/foo/..`` 和指向主目录的符号链接，字面上都
    不等于 ``/Users/me``，只按字符串比较会被绕过。resolve 出错（极少见，
    比如链接成环）时退回原路径，交给其他检查继续把关。
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def _same_file(a: Path, b: Path) -> bool:
    """两个路径是不是磁盘上的同一个目录/文件（比较设备号和 inode）。

    macOS 的 APFS 默认**不区分大小写**：``/users/ME`` 和 ``/Users/me`` 是
    同一个目录，但字符串不相等。用 samefile 比较"实际是不是同一个"，
    就不怕大小写、符号链接这类写法差异。任何一边不存在就返回 False。
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _is_same_dir(path: Path, target: Path) -> bool:
    """path 是否就是 target 本身（字面、解开后、实际文件三种比较）。"""
    for candidate in (path, _resolved(path)):
        for base in (target, _resolved(target)):
            if candidate == base:
                return True
    return _same_file(_resolved(path), target)


def _is_same_or_inside(path: Path, target: Path) -> bool:
    """path 是否就是 target、或者在 target 里面（任意深度）。

    先做字面比较（覆盖还不存在的路径），再从解开后的 path 往上逐级
    比较实际文件（覆盖大小写不同、符号链接绕路的写法）。
    """
    for candidate in (path, _resolved(path)):
        for base in (target, _resolved(target)):
            if candidate == base or candidate.is_relative_to(base):
                return True
    resolved = _resolved(path)
    return any(_same_file(level, target)
               for level in (resolved, *resolved.parents))


def is_same_or_inside(path, target) -> bool:
    """对外版本：path 是否就是 target、或在 target 里面（按实际目录比较）。

    背景（字符串比较曾导致换知识库位置时删光知识库）：daemon 以前用
    ``Path.is_relative_to`` 判断"新位置在不在旧库里面"——那是纯字符串
    比较。macOS 的 APFS 默认有三种"写法不同、其实是同一个目录"的情况：
    大小写不同（~/xhs-wiki 和 ~/XHS-Wiki）、Unicode 组合方式不同（é 的
    NFC 和 NFD 两种编码）、/System/Volumes/Data/... 这种固件链接别名。
    ``resolve()`` 一种也不会统一，字符串比较全部漏掉。这里逐级用
    samefile（比较设备号和 inode，即"磁盘上的身份证号"）比较，三种写法
    都能认出来。
    """
    return _is_same_or_inside(Path(path), Path(target))


def _fs_checks_kb_root(path: Path, app_home) -> None:
    """validate_kb_root 里需要访问文件系统的那几条检查（见调用处说明）。

    单独拆出来，是为了让调用方能把"访问被拒绝"（PermissionError 等）和
    "值本身不合法"分开处理：这里抛出的 OSError 由 validate_kb_root 统一
    转成 ``kb_root_inaccessible``。
    """
    resolved = _resolved(path)
    # 第 3 条后半：已经存在的话，必须是目录，且不是没写后缀的程序包。
    if resolved.exists():
        if not resolved.is_dir():
            raise ValueError("kb_root_invalid")
        if (resolved / "Contents" / "Info.plist").exists():
            raise ValueError("kb_root_invalid")
        # 挂载点（一块磁盘/一个卷的根目录，如外接盘
        # /Volumes/某盘、数据卷 /System/Volumes/Data）不能当知识库——
        # 数据卷根目录包含所有用户的数据和程序数据目录。
        if os.path.ismount(resolved):
            raise ValueError("kb_root_invalid")
    # 数据卷根目录单独再拦一次（不依赖 ismount 的判断方式）。
    if _is_same_dir(path, _DATA_VOLUME_ROOT):
        raise ValueError("kb_root_invalid")
    # 第 5 条（先于第 4 条检查，让主目录这类值报更直白的 kb_root_invalid，
    # 而不是"包含程序数据目录"）：顶层敏感目录本身不能当知识库。
    home = Path.home()
    for forbidden in (home, Path("/"), Path("/Users")):
        if _is_same_dir(path, forbidden):
            raise ValueError("kb_root_invalid")
    # 主目录下的"文稿""桌面""下载"等系统约定文件夹本身也
    # 不行（它们里面的子文件夹可以）。
    for name in _HOME_STANDARD_FOLDERS:
        if _is_same_dir(path, home / name):
            raise ValueError("kb_root_invalid")
    # ~/Library、~/.claude、~/.codex 本身和它们内部都不行。
    for forbidden in (home / "Library", home / ".claude", home / ".codex"):
        if _is_same_or_inside(path, forbidden):
            raise ValueError("kb_root_invalid")
    # 第 4 条：和程序自己的数据目录互不包含。
    if app_home is not None:
        app_home = Path(app_home)
        if _is_same_or_inside(path, app_home):
            raise ValueError("kb_root_inside_app_home")
        if _is_same_or_inside(app_home, path):
            raise ValueError("kb_root_contains_app_home")


def validate_kb_root(raw, *, app_home: "Path | None" = None) -> Path:
    """统一的 kb_root 校验，返回 expanduser 后的路径。

    被 /api/setup/kb-root、"打开知识库文件夹"（daemon）和 validate_settings
    共用，几条入口的规则完全一致。任何一条不满足就抛 ``ValueError``：

    1. 必须是非空字符串，不含反引号和"看不见的危险字符"（控制字符、
       Unicode 格式字符、行/段分隔符，见 kb_root_text_ok；换行会原样
       写进全局生效的 SKILL.md，等于向 AI 注入指令）；
    2. expanduser 后必须是绝对路径（以 / 开头，从源头上挡住 "-aTerminal"
       这类会被 open 当成选项的值）→ kb_root_not_absolute；
    3. 不能是 .app/.command 这类包或可执行文件：路径（以及解开符号链接
       后的路径）任何一级的后缀都不能是包后缀；已经存在的话，必须是
       普通目录，且里面没有 Contents/Info.plist（没写后缀的程序包），
       也不能是挂载点（一块磁盘的根目录，如 /System/Volumes/Data）；
    4. 给了 ``app_home`` 时：既不能在 APP_HOME 里（kb_root_inside_app_home），
       也不能把 APP_HOME 包含在内（kb_root_contains_app_home，否则知识库
       会把程序自己的数据目录装进去）；
    5. 不能是 $HOME、/、/Users、主目录下的"文稿""桌面""下载"等系统约定
       文件夹本身；也不能是 ~/Library、~/.claude、~/.codex 本身或在它们
       里面（这些位置放知识库要么污染系统目录，要么会被别的工具读到）。
       注意 $HOME 下面的普通子目录是允许的——默认值 ~/XHS-Wiki 就在
       $HOME 下。

    第 5 条和第 4 条同时做"字面比较"和"实际文件比较"：``~/foo/..``、
    大小写不同的写法、指向主目录的符号链接都会被认出来。

    第 3–5 条要访问文件系统。路径所在目录没有访问权限时
    （PermissionError 之类），以前会把异常原样抛出去、让请求连接直接
    断开；现在统一报 ``kb_root_inaccessible``，调用方能给出明确说明。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("kb_root_invalid")
    text = raw.strip()
    # 反引号和危险的不可见字符（见 kb_root_text_ok）不允许出现在任何位置。
    if not kb_root_text_ok(text):
        raise ValueError("kb_root_invalid")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError("kb_root_not_absolute")
    resolved = _resolved(path)
    # 第 3 条：包后缀（字面路径和解开后的路径都查，防符号链接改名绕过）。
    for candidate in (path, resolved):
        for part in candidate.parts:
            if Path(part).suffix.lower() in _KB_ROOT_FORBIDDEN_SUFFIXES:
                raise ValueError("kb_root_invalid")
    # 第 3–5 条中需要读文件系统的部分；读不了（没权限等）统一报
    # kb_root_inaccessible，不把底层异常（可能带路径）抛给调用方。
    try:
        _fs_checks_kb_root(path, app_home)
    except OSError:
        raise ValueError("kb_root_inaccessible") from None
    return path

# scheduler.json 的默认内容。"followups" 是今天已经追加运行了几次。
DEFAULT_SCHEDULER = {
    "last_scheduled_date": None,
    "followup_date": None,
    "followups": 0,
    "last_download_retry_at": None,
    "cleanup_freed_bytes_total": 0,
}

# 状态文件的默认内容（还没运行过时的样子）。
DEFAULT_STATUS = {
    "state": "idle",
    "step": None,
    "run_id": None,
    "trigger": None,
    "started_at": None,
    "updated_at": None,
    "message": "",
    "needs_user": [],
}

# 追加运行（followup）的规则参数：距上次结束 ≥ 60 分钟，每天最多 6 次。
FOLLOWUP_MINUTES = 60
FOLLOWUP_MAX_PER_DAY = 6


def _valid_base_url(url) -> bool:
    """检查 openai_compatible 的 base_url 是否合法（真正解析 URL，不比前缀）。

    规则（docs/architecture.md §8）：必须是 https；唯一的例外是本机
    回环地址上的 http（有的兼容服务就跑在本机上），因为模型调用的
    提示词是私人内容，明文 http 会被网络上看到。

    为什么不能靠字符串前缀判断：``http://localhost.evil.example``
    和 ``http://localhost@evil.example`` 都以 ``http://localhost`` 开头，
    但实际主机是 evil.example——openai SDK 解析后会把 Key 用明文发到
    外部主机。所以这里用 ``urllib.parse.urlsplit`` 真正解析一遍：

    - scheme 必须是 https；scheme 是 http 时 hostname 必须严格等于
      127.0.0.1 / localhost / ::1；
    - 拒绝带用户名密码的 URL（userinfo 写法可以骗过人眼）；
    - 拒绝没有主机名的 URL。
    """
    if not isinstance(url, str) or not url:
        return False
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        return False
    host = parts.hostname
    if not host:
        return False
    if parts.scheme == "https":
        return True
    if parts.scheme == "http":
        return host in ("127.0.0.1", "localhost", "::1")
    return False


def validate_settings(value: dict) -> dict:
    """校验一份设置是否合法。

    按 docs/architecture.md §7 的规则逐项检查；任何一项不通过就整体拒绝，
    抛 ValueError（错误代码写在异常消息里），不会部分保存。
    通过时原样返回这份设置。
    """
    if not isinstance(value, dict):
        raise ValueError("settings_not_dict")

    # 先看有没有表里没有的字段（未知字段直接拒绝，防止手滑写错键名）。
    for key in value:
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"unknown_field:{key}")

    # daily_time：必须是 00:00–23:59 之间的 "HH:MM" 字符串。
    daily_time = value.get("daily_time")
    if not isinstance(daily_time, str) or not _DAILY_TIME_RE.fullmatch(daily_time):
        raise ValueError("daily_time_invalid")

    # 整数字段：bool 是 int 的子类，所以要显式排除（True 当作 1 是常见的手滑）。
    # first_import_limit 上限取 1000（保守的宽范围，防止误填负数或天文数字）。
    for field, low, high in (
        ("max_new_per_run", 1, 200),
        ("download_retry_every_days", 1, 30),
    ):
        v = value.get(field)
        if isinstance(v, bool) or not isinstance(v, int) or not (low <= v <= high):
            raise ValueError(f"{field}_invalid")

    # first_import_limit：允许两种值——
    # - null：向导里选了"全部收藏"（runner._eligible 把空值当作不限）；
    # - 1–1000 的整数：只整理收藏列表前 N 条（首次可能积累几千条，先小范围试）。
    limit = value.get("first_import_limit")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 1000)
    ):
        raise ValueError("first_import_limit_invalid")

    # 布尔字段必须是真布尔值。
    for field in ("schedule_enabled", "headless", "cleanup_enabled", "setup_completed"):
        if not isinstance(value.get(field), bool):
            raise ValueError(f"{field}_invalid")

    # profile_url：null（还没登录）或完全匹配小红书个人主页的固定格式。
    profile_url = value.get("profile_url")
    if profile_url is not None and (
        not isinstance(profile_url, str) or not _PROFILE_URL_RE.fullmatch(profile_url)
    ):
        raise ValueError("profile_url_invalid")

    # kb_root：知识库根目录，走统一校验（这里拿不到
    # APP_HOME，APP_HOME 相关的两条由 daemon 的写入口再查一遍）。
    # kb_root 所在目录暂时读不了（没权限、后台服务被隐私
    # 保护挡住）时，校验报 kb_root_inaccessible。这只是"当前访问不了"，
    # 不是值本身危险（字符串层面的检查已经全部通过），不能因此让改定时、
    # 写主页地址这些**无关**的设置写入全部失败——所以这里放行。
    # 真正要用 kb_root 的两个入口（向导设置位置、在访达里显示）仍会
    # 明确报 kb_root_inaccessible。
    try:
        validate_kb_root(value.get("kb_root"))
    except ValueError as error:
        if str(error) != "kb_root_inaccessible":
            raise

    # engine：只能是两种引擎之一。
    if value.get("engine") not in ENGINES:
        raise ValueError("engine_invalid")

    # llmwiki_binary：null（没配置）或绝对路径（以 / 开头的
    # 非空字符串；~ 之类的写法交给用户自己展开，这里只认绝对路径）。
    llmwiki_binary = value.get("llmwiki_binary")
    if llmwiki_binary is not None and (
        not isinstance(llmwiki_binary, str)
        or not llmwiki_binary.strip()
        or not llmwiki_binary.startswith("/")
    ):
        raise ValueError("llmwiki_binary_invalid")

    # asr_model：只能是两个候选之一。
    if value.get("asr_model") not in ASR_MODELS:
        raise ValueError("asr_model_invalid")

    # llm：整理模型的配置对象。provider 限四种；model 非空且只含
    # 白名单字符（见 _LLM_MODEL_RE 的说明）；base_url 只有
    # openai_compatible 需要（见 _valid_base_url 的说明）。
    # llm 只能有 provider、model、base_url 三个键——以前多余子键
    # （如 claude_path）照存不误，而那个值会被当成可执行文件路径执行。
    llm = value.get("llm")
    if not isinstance(llm, dict):
        raise ValueError("llm_invalid")
    for sub_key in llm:
        if sub_key not in ("provider", "model", "base_url"):
            raise ValueError(f"unknown_field:llm.{sub_key}")
    if llm.get("provider") not in LLM_PROVIDERS:
        raise ValueError("llm_provider_invalid")
    if not isinstance(llm.get("model"), str) or not _LLM_MODEL_RE.fullmatch(
            llm["model"]):
        raise ValueError("llm_model_invalid")
    base_url = llm.get("base_url")
    if llm["provider"] == "openai_compatible":
        if not _valid_base_url(base_url):
            raise ValueError("base_url_invalid")
    elif base_url is not None:
        # 其他 provider 用不上 base_url，出现非 null 值多半是配置写错了。
        raise ValueError("base_url_not_allowed")

    # skill_targets：列表，每一项都必须是已知的安装位置，不能重复。
    # 这份列表决定往哪些目录写文件、从哪些目录删文件（skill.sync），
    # 所以只认 SKILL_TARGETS 里的固定名字，绝不接受路径之类的自由文本。
    targets = value.get("skill_targets")
    if (not isinstance(targets, list)
            or any(not isinstance(t, str) or t not in SKILL_TARGETS for t in targets)
            or len(set(targets)) != len(targets)):
        raise ValueError("skill_targets_invalid")

    # schema_version 目前固定为 1。
    if value.get("schema_version") != 1:
        raise ValueError("schema_version_invalid")

    return value


def _parse_daily_time(settings: dict, day: datetime) -> datetime:
    """把某一天的日期和设置里的 "HH:MM" 拼成一个北京时间的 datetime。"""
    hour, minute = (int(part) for part in settings["daily_time"].split(":"))
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def next_run_at(settings: dict, now: datetime, last_scheduled_date) -> "datetime | None":
    """计算"下一次定时运行"的时间点（纯函数，不碰磁盘）。

    规则：
    - 定时关闭 → None；
    - 今天没跑过、还没到 daily_time → 返回今天的 daily_time；
    - 今天已经跑过 → 返回明天的 daily_time；
    - 今天没跑过、但已过了 daily_time（比如电脑 03:00 在睡眠，05:00 才醒）
      → 返回 now，意思是"应该立即补跑"。
    """
    if not settings.get("schedule_enabled"):
        return None
    today_target = _parse_daily_time(settings, now)
    if last_scheduled_date == now.date().isoformat():
        # 今天已经跑过：等明天的同一时间。
        return _parse_daily_time(settings, today_target + timedelta(days=1))
    if now < today_target:
        return today_target
    return now


def due_daily(settings: dict, now: datetime, last_scheduled_date, running: bool, paused: bool) -> bool:
    """判断现在是否应该开跑"每日定时运行"（纯函数）。

    条件：定时开着；今天还没跑过；现在已经到了今天的 daily_time；
    当前没有别的运行在进行；也没有请求暂停。
    """
    if running or paused:
        return False
    if not settings.get("schedule_enabled"):
        return False
    if last_scheduled_date == now.date().isoformat():
        return False
    return now >= _parse_daily_time(settings, now)


def due_followup(last_run, now: datetime, followup_date, followups: int, running: bool, paused: bool) -> bool:
    """判断现在是否应该开跑"追加运行"（纯函数）。

    规则：上一次运行记录的结果是 incomplete（还有笔记没到终态）；
    距它结束已满 60 分钟；今天追加次数 < 6；没有运行在进行；没有请求
    暂停。followup_date 换天时计数按 0 算（由调用方在写盘时清零，
    这里只负责判断）。
    下载熔断的结果是 needs_user，不满足 incomplete 条件，因此不会追加；
    每日运行由 due_daily 独立判断，不受这个事项影响。
    """
    if running or paused:
        return False
    if not isinstance(last_run, dict) or last_run.get("result") != "incomplete":
        return False
    ended_at = last_run.get("ended_at")
    if not ended_at:
        return False
    ended = datetime.fromisoformat(ended_at)
    if now - ended < timedelta(minutes=FOLLOWUP_MINUTES):
        return False
    todays_followups = followups if followup_date == now.date().isoformat() else 0
    return todays_followups < FOLLOWUP_MAX_PER_DAY


class ServiceStore:
    """服务目录的读写入口。

    root 是服务目录（真实环境由 config.app_home() 给出，测试时传临时目录）。
    构造时确保目录存在且权限是 0700（只有当前用户能进）。
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    # ---------------- 基础：跨进程文件锁 / 原子写 / 读 JSON ----------------

    # 哪些名字允许加锁。守护进程和运行子进程是两个进程，会同时读写
    # 同一个服务目录；"读出来 → 改 → 写回去"的流程必须放进各自文件的
    # 锁里，否则后写的进程会把先写的进程的改动覆盖掉（丢失更新）。
    LOCKABLE_NAMES = ("settings", "status", "scheduler", "notify", "runs")

    @contextmanager
    def locked(self, name: str):
        """对服务目录下的 <name>.lock 加排他锁（fcntl.flock）。

        用法：with store.locked("status"): …（这一段里对 status.json 的
        读-改-写不会被另一个进程插进来）。

        为什么用锁文件而不是锁数据文件本身：数据文件会被"临时文件 +
        os.replace"整体替换，替换后锁住的其实是旧文件，锁就失效了；
        单独一个从不替换的 .lock 文件才锁得住。锁文件权限 0600，
        创建后一直保留（内容始终为空，留着不影响任何功能）。
        """
        if name not in self.LOCKABLE_NAMES:
            raise ValueError(f"unknown_lock_name:{name}")
        lock_path = self.root / f"{name}.lock"
        # 用低层 os.open 创建，权限一步到位 0600（避免 open() 先建 0644 再改）。
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            # 无论如何都释放锁并关闭文件描述符；出错时异常原样抛给调用方。
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def write_json(self, path: Path, data) -> None:
        """把 data 写成 JSON 文件：先写临时文件（0600）再 os.replace 顶替。

        参数 path 是相对 self.root 的文件名或路径。

        临时文件用 tempfile.mkstemp 生成**唯一**的名字：守护进程和运行
        子进程可能同时写同一个文件，如果临时文件名固定（比如 status.json.tmp），
        两个进程会写进同一个临时文件，结果可能变成两份内容的混合。
        mkstemp 创建出来权限就是 0600；写完 fsync 落盘再替换；出错时
        删掉临时文件，把异常原样抛出去。
        """
        target = self.root / path
        text = json.dumps(data, ensure_ascii=False, indent=2)
        fd, tmp_name = tempfile.mkstemp(
            dir=target.parent, prefix="." + target.name + ".", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except BaseException:
            # 出错（包括上面 with 里抛的异常）时清掉临时文件再抛出去，
            # 不在服务目录里留垃圾。
            tmp.unlink(missing_ok=True)
            raise

    def read_json(self, path: Path, default):
        """读一个 JSON 文件；不存在时返回 default。"""
        target = self.root / path
        if not target.exists():
            return default
        with open(target, "r", encoding="utf-8") as f:
            return json.load(f)

    # ---------------- 设置 settings.json ----------------

    def load_settings(self) -> dict:
        """读取设置；文件不存在时返回默认值（不必立刻写盘）。

        文件存在时，先用默认值补齐缺少的字段再返回（向前兼容）：
        以后新增设置项时，旧的设置文件不会因为缺字段而让读端出错。
        未知字段不在这里处理，仍由 validate_settings 在写盘时拒绝。
        注意 llm 是嵌套对象，浅合并即可（新旧整体替换）。
        """
        settings = self.read_json(Path("settings.json"), None)
        if settings is None:
            # 返回深拷贝：llm 是嵌套 dict，避免调用方改坏模块级默认值。
            return json.loads(json.dumps(DEFAULT_SETTINGS))
        # 迁移清洗：旧文件里 llm 可能存着 claude_path / codex_path 这类
        # 多余子键（修复前能经 /api/settings 写进去）。读取时只保留三个
        # 合法子键，下次写盘多余键自然消失——如果不清洗，validate_settings
        # 对 llm 键的新限制会让老用户的每次设置修改都失败。
        if isinstance(settings.get("llm"), dict):
            settings["llm"] = {k: v for k, v in settings["llm"].items()
                               if k in ("provider", "model", "base_url")}
        # 补齐用的默认值同样先深拷贝：skill_targets 是列表，旧设置文件
        # 没有这个键时，直接用模块级的默认列表，调用方一改就把默认值改坏了。
        return {**json.loads(json.dumps(DEFAULT_SETTINGS)), **settings}

    def update_settings(self, changes: dict) -> dict:
        """修改设置（只允许白名单里的字段）。

        流程：先检查每个要改的字段是否在白名单里（不是就抛
        ValueError('field_not_editable:<名>')），然后与旧设置合并、整体校验，
        校验不通过就抛异常且**不写盘**（文件内容保持不变），通过才原子写入。

        整个"读 → 合并 → 写"放进 settings 锁：守护进程和运行子进程
        可能同时改设置，不加锁会互相覆盖（丢失更新）。
        """
        for key in changes:
            if key not in EDITABLE_SETTINGS_FIELDS:
                raise ValueError(f"field_not_editable:{key}")
        with self.locked("settings"):
            merged = dict(self.load_settings())
            merged.update(changes)
            validate_settings(merged)
            self.write_json(Path("settings.json"), merged)
        return merged

    # ---------------- 状态 status.json ----------------

    def read_status(self) -> dict:
        """读取当前状态；不存在时返回默认值（state=idle）。"""
        status = self.read_json(Path("status.json"), None)
        if status is None:
            # 返回一份拷贝，避免调用方改坏模块级的默认值。
            return json.loads(json.dumps(DEFAULT_STATUS))
        return status

    def write_status(self, now: datetime = None, **fields) -> dict:
        """合并式更新状态：只改传入的字段，其余保留，并自动更新 updated_at。

        整个读-改-写放进 status 锁：运行子进程在写状态时，守护进程可能
        正好在加/清 needs_user，不加锁会互相覆盖。
        """
        if now is None:
            now = now_beijing()
        with self.locked("status"):
            status = self.read_status()
            status.update(fields)
            status["updated_at"] = now.isoformat()
            self.write_json(Path("status.json"), status)
        return status

    def add_needs_user(self, code: str, detail: str, now: datetime = None) -> dict:
        """往状态里加一条"需要用户处理"的事项。

        同一个代码只保留一条（重复出现不叠加），并且保留**首次**出现的
        时间 since——这样用户能看到问题最早是什么时候冒出来的。
        标题取 NEEDS_USER_TITLES 表中的固定文案。
        """
        if now is None:
            now = now_beijing()
        if code not in NEEDS_USER_TITLES:
            raise ValueError(f"unknown_needs_user_code:{code}")
        with self.locked("status"):
            status = self.read_status()
            existing = {
                i.get("code"): i for i in status.get("needs_user", []) if i.get("code")
            }
            old = existing.get(code, {})
            items = [i for i in status.get("needs_user", []) if i.get("code") != code]
            # since 优先沿用第一次出现的时间；只有第一次出现才用当前时间。
            since = old.get("since") or now.isoformat()
            items.append(
                {
                    "code": code,
                    "title": NEEDS_USER_TITLES[code],
                    "detail": detail,
                    "since": since,
                }
            )
            status["needs_user"] = items
            status["updated_at"] = now.isoformat()
            self.write_json(Path("status.json"), status)
        return status

    def clear_needs_user(self, code: str = None) -> dict:
        """清掉一条（按 code）或全部"需要用户处理"的事项（同样在 status 锁里）。"""
        with self.locked("status"):
            status = self.read_status()
            if code is None:
                status["needs_user"] = []
            else:
                status["needs_user"] = [
                    i for i in status.get("needs_user", []) if i.get("code") != code
                ]
            status["updated_at"] = now_beijing().isoformat()
            self.write_json(Path("status.json"), status)
        return status

    # ---------------- 运行记录 runs.jsonl ----------------

    def append_run(self, record: dict) -> None:
        """追加一行运行记录（JSONL：一行一个 JSON 对象）。

        三点加固：
        1. 用 os.open 以 0600 一步创建/打开——open(…, "a") 建出来是 0644，
           再 chmod 之间有短窗；os.open 的第三个参数让文件一落地就是 0600；
        2. 写入放进 runs 锁，两个进程同时追加不会串行交错；
        3. 每行一次性 write 完整的"JSON + 换行"，读者按行解析不会读到半行。
        """
        target = self.root / "runs.jsonl"
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self.locked("runs"):
            fd = os.open(
                target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(line)

    def list_runs(self, limit: int = 30) -> list:
        """读取运行记录，新的在前（倒序），最多 limit 条。

        遇到损坏的行（不是合法 JSON、或不是对象）就跳过，不抛异常——
        运行记录是给人看的，一行坏了不该拖垮整个控制台。
        """
        target = self.root / "runs.jsonl"
        if not target.exists():
            return []
        records = []
        with open(target, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            records.append(record)
            if len(records) >= limit:
                break
        return records

    # ---------------- 定时 scheduler.json ----------------

    def read_scheduler(self) -> dict:
        """读取定时信息；不存在时返回默认值。"""
        scheduler = self.read_json(Path("scheduler.json"), None)
        if scheduler is None:
            return json.loads(json.dumps(DEFAULT_SCHEDULER))
        return scheduler

    def write_scheduler(self, **fields) -> dict:
        """合并式更新定时信息（last_scheduled_date / followup_date / followups / …）。

        放进 scheduler 锁，防止守护进程和子进程同时更新时互相覆盖。
        """
        with self.locked("scheduler"):
            scheduler = self.read_scheduler()
            scheduler.update(fields)
            self.write_json(Path("scheduler.json"), scheduler)
        return scheduler

    # ---------------- 暂停标记 PAUSE ----------------

    def pause_requested(self) -> bool:
        """PAUSE 文件存在即表示用户请求了暂停。"""
        return (self.root / "PAUSE").exists()

    def request_pause(self) -> None:
        """创建 PAUSE 标记（空文件，权限 0600）。"""
        marker = self.root / "PAUSE"
        marker.touch()
        os.chmod(marker, 0o600)

    def clear_pause(self) -> None:
        """删除 PAUSE 标记（不存在时也照常返回，不报错）。"""
        marker = self.root / "PAUSE"
        if marker.exists():
            marker.unlink()

    # ---------------- 通知发送时间 notify.json（给 notify.py 用）----

    def read_notify_times(self) -> dict:
        """读取各通知代码上次发送时间，格式 {"代码": "ISO"}；不存在返回空 dict。"""
        return self.read_json(Path("notify.json"), {})

    def write_notify_times(self, times: dict) -> None:
        """整体写入各通知代码的上次发送时间（放进 notify 锁）。"""
        with self.locked("notify"):
            self.write_json(Path("notify.json"), times)
