# -*- coding: utf-8 -*-
"""notify.py —— macOS 系统通知 + 去重。

这个文件做什么：把"需要用户处理"的事项（登录过期、磁盘不足、模型不可用
等）用 macOS 的系统通知（屏幕右上角弹出的横幅）告诉用户。

在整体里的位置：基础层之一，被 runner/daemon 调用；文案表复用
store.NEEDS_USER_TITLES，发送时间记在 store 的 notify.json 里。

做法：
- 用 macOS 自带的 osascript 命令执行一句 AppleScript：
  display notification "正文" with title "小红书收藏知识库" subtitle "…"
- 启动 osascript 时用参数列表（不经过 shell），正文里的引号和反斜杠会先
  转义，避免内容被拼接出别的 AppleScript 语句；
- 同一个代码 6 小时内只发一次（发送时间记在服务目录的 notify.json 里），
  防止反复失败时通知轰炸；
- 通知正文只用固定文案，不放链接、路径或笔记标题。

通知副标题为什么是一条命令：AppleScript 的 display notification 不支持"点击
后打开某个地址"的动作，点通知只会打开"脚本编辑器"。控制台现在要带
访问令牌才能打开，唯一的正确入口是 ``xhs-wiki open``（它写 0600 的跳转
页再打开，见 console_auth.py），所以通知的副标题统一写这条命令，
而不是控制台地址——地址本身打开后只会看到"请运行 xhs-wiki open"。
"""

import subprocess
from datetime import datetime, timedelta

from .store import NEEDS_USER_TITLES

# 通知默认标题；下载熔断另用明确的暂停标题，便于用户看出停止原因。
NOTIFY_TITLE = "小红书收藏知识库"

# 通知副标题：告诉用户怎么打开控制台处理（走 xhs-wiki open 的跳转页，
# 不写控制台地址，理由见文件顶部）。
NOTIFY_SUBTITLE = "在终端运行 xhs-wiki open 打开控制台"

# osascript 是系统自带、路径固定的命令，一律用绝对路径调用，
# 不再按 PATH 查找（服务的 PATH 会优先搜索 ~/.local/bin 等目录，
# 出现同名命令时通知内容会被交给它）。
OSASCRIPT_BIN = "/usr/bin/osascript"

# 代码 → 中文正文（固定文案）。直接复用存储层的表，保证文案单一来源。
TEMPLATES = dict(NEEDS_USER_TITLES)

# 同一代码的通知间隔（小时）。
NOTIFY_DEDUP_HOURS = 6


def escape_applescript(text: str) -> str:
    """转义放进 AppleScript 双引号字符串里的文本。

    AppleScript 字符串里反斜杠和双引号有特殊含义，要先替换反斜杠再替换
    引号（顺序反了会把转义符本身再转掉）。
    """
    return text.replace("\\", "\\\\").replace('"', '\\"')


def notify(code: str, store, *, now, runner=subprocess.run) -> bool:
    """发一条系统通知。

    参数：
    - code：NEEDS_USER_TITLES 表里的通知代码（如 "xhs_login"），未知代码抛 ValueError；
    - store：ServiceStore 实例，用来读写 notify.json（去重账本）；
    - now：当前时间（datetime，由调用方传入，方便测试）；
    - runner：执行外部命令的函数，默认 subprocess.run（测试传假的）。

    返回：
    - True：这次真的发了通知；
    - False：6 小时内同一代码已经发过被去重跳过，**或者** osascript 调用
      失败/超时/返回码非 0（失败时不记录发送时间，下次还能再试）。
    """
    if code not in TEMPLATES:
        raise ValueError(f"unknown_notify_code:{code}")

    # 去重：查上次发送时间，还在间隔内就直接跳过。
    times = store.read_notify_times()
    last_sent = times.get(code)
    if last_sent is not None:
        last = datetime.fromisoformat(last_sent)
        if now - last < timedelta(hours=NOTIFY_DEDUP_HOURS):
            return False

    # 组装并执行 osascript 命令（参数列表，不走 shell）。下载熔断沿用
    # 同一套正文与六小时去重，只把标题换成任务约定的暂停提示。
    title = "小红书下载暂停" if code == "xhs_download_blocked" else NOTIFY_TITLE
    script = (
        f'display notification "{escape_applescript(TEMPLATES[code])}" '
        f'with title "{escape_applescript(title)}" '
        f'subtitle "{escape_applescript(NOTIFY_SUBTITLE)}"'
    )
    # 超时 15 秒：osascript 卡住时调用方（比如运行子进程）不会一直等。
    # 任何失败（抛异常、超时、返回码非 0）都当作"没发出去"，返回 False
    # 且不记录发送时间——这样下次还能重试，而不是被去重账本挡住。
    try:
        result = runner([OSASCRIPT_BIN, "-e", script], timeout=15,
                        capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False

    # 记录本次发送时间，然后返回成功。
    times[code] = now.isoformat()
    store.write_notify_times(times)
    return True
