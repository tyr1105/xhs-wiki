# -*- coding: utf-8 -*-
"""cli.py —— 命令行入口 `xhs-wiki`（也可 `python -m xhs_wiki` 运行）。

这个文件做什么：用 argparse 实现各子命令，把参数翻译成对下层模块的
调用。子命令：run（一次运行）、login（扫码登录）、status（阶段统计 +
最近运行）、cleanup（清理原件）、doctor（环境检查）、serve
（启动守护进程和控制台）、print-launch-agent（只打印 LaunchAgent 配置）、
open（带访问令牌打开控制台，见 console_auth.py）、assets（安装资源的
下载和校验）。

在整体里的位置：docs/architecture.md §1 的最上层，被用户和 LaunchAgent
调用；它 import runner / cleanup / scan / store / ledger，不直接碰网络
或浏览器（login 例外——它本来就是要打开带窗口的浏览器给用户扫码）。

测试支持：所有子命令都支持 --home <目录> 覆盖 APP_HOME（设置
XHS_WIKI_HOME 环境变量，config.py 会读它）；main() 还接受 deps_factory
参数注入假的依赖，测试不碰真实数据。

红线：输出里绝不出现签名链接、Key、个人路径；运行记录和台账里的
链接都是规范链接（不带 xsec_token）。
"""

import argparse
import importlib
import os
import platform
import stat
import sys
from pathlib import Path

from . import cleanup as cleanup_module
from . import config, runner
from .ledger import Ledger
from .store import ServiceStore, now_beijing


def _build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（单独一个函数，方便测试直接检查参数定义）。"""
    parser = argparse.ArgumentParser(
        prog="xhs-wiki",
        description="把小红书收藏自动整理成本机知识库",
    )
    # 所有子命令共用的 --home：覆盖 APP_HOME（测试用，见 config.app_home）。
    parser.add_argument("--home", help="覆盖应用数据目录 APP_HOME（测试用）")
    # 守护进程拉起子进程时曾把 --home 写在子命令后面
    # （run --trigger manual --home <目录>），而 argparse 默认只认写在
    # 子命令前面的全局参数，导致真实环境里"立即运行"/定时/扫码登录全部
    # 以退出码 2 失败。修法：给每个子命令的 parser 也加一个同名 --home，
    # default=argparse.SUPPRESS——没在子命令后提供时不写入 namespace，
    # 不会覆盖前面全局位置的值；提供了就以后写的为准。两种写法都可用。
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="执行一次运行")
    run_parser.add_argument(
        "--trigger", default="manual",
        choices=["manual", "schedule", "followup", "after_login", "resume"],
        help="触发方式",
    )

    sub.add_parser("login", help="扫码登录小红书（带窗口）")
    sub.add_parser("status", help="打印阶段统计和最近 5 次运行")

    cleanup_parser = sub.add_parser("cleanup", help="清理已入库笔记的下载原件")
    group = cleanup_parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True,
                       help="只看清单不删除（默认）")
    group.add_argument("--apply", action="store_true",
                       help="实际执行删除（删除前三道核对）")

    sub.add_parser("doctor", help="环境检查：逐项打印通过或不通过")
    serve_parser = sub.add_parser("serve", help="启动守护进程和控制台")
    serve_parser.add_argument("--port", type=int, default=None,
                              help="控制台端口（默认 18765）")
    agent_parser = sub.add_parser(
        "print-launch-agent", help="打印 LaunchAgent 配置（只打印、不写入）")
    agent_parser.add_argument("--python", default=None,
                              help="LaunchAgent 用的 Python 解释器路径（默认当前解释器）")
    # 控制台要带访问令牌才能打开，open 是唯一的正确入口。
    open_parser = sub.add_parser(
        "open", help="在浏览器里打开控制台（自动带上只有你能读到的访问令牌）")
    open_parser.add_argument("--port", type=int, default=None,
                             help="控制台端口（默认 18765；serve 用了 --port 时填同一个）")

    # 安装资源的下载/校验（install.sh 调用；也供用户手动补装）。
    assets_parser = sub.add_parser(
        "assets", help="安装资源的下载和校验（下载组件、语音模型、LLM Wiki 后台）")
    assets_sub = assets_parser.add_subparsers(dest="assets_command",
                                              required=True)
    assets_sub.add_parser("install-downloader",
                          help="下载并安装 XHS-Downloader 及其依赖")
    assets_sub.add_parser("install-llmwiki",
                          help="下载并校验 LLM Wiki 后台，自动配置程序路径")
    model_parser = assets_sub.add_parser(
        "install-model", help="下载并安装语音模型（whisper 或 sensevoice）")
    model_parser.add_argument("name", choices=["whisper", "sensevoice"],
                              help="模型名")
    verify_parser = assets_sub.add_parser(
        "verify", help="核对语音模型文件是否齐全")
    verify_parser.add_argument("name", nargs="?", default=None,
                               choices=[None, "whisper", "sensevoice"],
                               help="只核对这一个模型（默认两个都查）")

    # 给每个子命令的 parser 也加上 --home（写在子命令前后都可用，
    # 理由见上面的注释块）。SUPPRESS 保证子命令后没写时不覆盖全局值。
    for sub_parser in sub.choices.values():
        sub_parser.add_argument("--home", default=argparse.SUPPRESS,
                                help=argparse.SUPPRESS)
    return parser


def _print(line: str = "") -> None:
    """集中一处打印（测试里 monkeypatch 这里就能拿到全部输出）。"""
    print(line)


# ---------------------------------------------------------------------------
# 各子命令的实现
# ---------------------------------------------------------------------------

def _cmd_run(args, deps_factory=None) -> int:
    """run 子命令：执行一次运行并打印结果摘要。"""
    deps = (deps_factory or runner.default_deps)()
    record = runner.run(args.trigger, deps=deps)
    # 打印的内容全部来自运行记录——它不含签名链接（红线），可以放心输出。
    _print(f"运行 {record['run_id']}（{record['trigger']}）：{record['result']}")
    if record.get("stop_reason"):
        _print(f"  停止原因：{record['stop_reason']}")
    _print(f"  {record.get('message') or ''}")
    scan = record.get("scan") or {}
    _print(f"  扫描：可见 {scan.get('visible')} / 标称 {scan.get('nominal')}"
           f"{'（不完整）' if scan.get('incomplete') else ''}，"
           f"新增 {record.get('new_found', 0)} 条")
    processed = record.get("processed") or {}
    parts = []
    for stage in ("download", "parse", "ingest", "distill"):
        stats = processed.get(stage) or {}
        parts.append("/".join(f"{k}={v}" for k, v in stats.items()))
    _print("  处理：" + "，".join(parts))
    counts = record.get("counts") or {}
    _print("  台账：" + ("，".join(f"{k}={v}" for k, v in sorted(counts.items()))
                       or "空"))
    cleanup_stats = record.get("cleanup") or {}
    if cleanup_stats.get("ran"):
        _print(f"  清理：{cleanup_stats.get('notes')} 条，释放 "
               f"{cleanup_stats.get('freed_bytes', 0)} 字节")
    return 0


def _cmd_login(args) -> int:
    """login 子命令：扫码登录，成功后写回 profile_url 并清掉 xhs_login 事项。

    扫码必须带窗口（用户得看到二维码才能扫），由 scan.login_flow 保证。
    测试时 monkeypatch scan.login_flow 换成假实现，不启动真 Chrome。
    """
    from . import scan
    store = ServiceStore(config.app_home())
    qr_path = config.app_home() / "login-qr.png"
    result = scan.login_flow(config.browser_profile_dir(), str(qr_path))
    outcome = result.get("result")
    _print(f"登录结果：{outcome}")
    if outcome in ("logged_in", "already_logged_in"):
        profile_url = result.get("profile_url")
        if profile_url:
            store.update_settings({"profile_url": profile_url})
            _print("已识别主页地址并写入设置")
        # 登录成功：清掉状态里可能残留的 xhs_login 事项。
        store.clear_needs_user("xhs_login")
        return 0
    if outcome == "blocked":
        _print("页面被拦截（要求验证或访问受限），请稍后再试")
    else:  # timeout
        _print("等待扫码超时")
    return 1


def _cmd_status(args) -> int:
    """status 子命令：打印台账阶段统计和最近 5 次运行。"""
    store = ServiceStore(config.app_home())
    ledger = Ledger(config.ledger_path())
    try:
        counts = ledger.counts()
        _print("阶段统计：" + ("，".join(f"{k}={v}" for k, v in sorted(counts.items()))
                             or "还没有收藏记录"))
        runs = store.list_runs(5)
        _print("最近运行：")
        if not runs:
            _print("  （还没有运行记录）")
        for record in runs:
            _print(f"  {record.get('run_id')} {record.get('trigger')} "
                   f"{record.get('result')} {record.get('message') or ''}")
    finally:
        ledger.close()
    return 0


def _cmd_cleanup(args, deps_factory=None) -> int:
    """cleanup 子命令：--dry-run 只看清单（默认），--apply 实际删除。"""
    ledger = Ledger(config.ledger_path())
    try:
        if not args.apply:
            candidates = cleanup_module.find_candidates(ledger)
            summary = cleanup_module.summarize_candidates(candidates)
            _print(f"可清理 {summary['total']['notes']} 条，共占用 "
                   f"{summary['total']['bytes']} 字节（--apply 才会删除）")
            for stage, stats in summary["by_stage"].items():
                if stats["notes"]:
                    _print(f"  {stage}：{stats['notes']} 条 / {stats['bytes']} 字节")
            return 0
        deps = (deps_factory or runner.default_deps)()
        settings = deps.store.load_settings()
        engine = deps.make_engine(settings)
        candidates = cleanup_module.find_candidates(ledger, engine)
        outcome = cleanup_module.apply(
            candidates, ledger=ledger, engine=engine, store=deps.store,
            now=now_beijing)
        _print(f"已清理 {outcome['notes']} 条，释放 {outcome['freed_bytes']} 字节")
        for skipped in outcome["skipped"]:
            _print(f"  跳过 {skipped['note_id']}：{skipped['reason']}")
        return 0
    finally:
        ledger.close()


def _check(label: str, ok: bool, detail: str = "") -> bool:
    """doctor 的一项检查：统一格式打印 ✓/✗ + 说明，返回是否通过。"""
    mark = "✓" if ok else "✗"
    _print(f"{mark} {label}" + (f"：{detail}" if detail else ""))
    return ok


def _cmd_doctor(args, deps_factory=None) -> int:
    """doctor 子命令：环境检查，逐项打印通过或不通过。

    检查项：macOS 版本、Chrome、PyAV、vendor 里
    的 XHS-Downloader、模型文件、整理模型登录状态、知识库目录可写。
    有不通过的项没关系（返回码只作参考，exit 0 保证脚本可连续检查）。
    全部检查只读文件系统、检查本机依赖和登录状态，不发网络请求、不调模型。
    """
    home = config.app_home()
    deps_factory = deps_factory or runner.default_deps
    settings = ServiceStore(home).load_settings()

    # 1. macOS 版本：platform.mac_ver() 在 macOS 上返回版本号，其他系统为空。
    mac_version = platform.mac_ver()[0]
    _check("macOS 版本", bool(mac_version), mac_version or "不是 macOS")

    # 2. Google Chrome：看标准安装位置（Playwright 用 channel='chrome'）。
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    _check("Google Chrome", chrome.exists(), "已安装" if chrome.exists()
           else "未找到 /Applications/Google Chrome.app")

    # 3. PyAV 是自带音视频解码库的 Python 包。真正 import 才能发现
    #    本地二进制库缺失或不兼容，不能只看包名是否安装；失败时不输出
    #    原始异常，避免把本机路径等细节带进诊断结果。
    try:
        importlib.import_module("av")
    except Exception:
        _check("PyAV", False, "无法加载音视频解析组件，请重新运行安装脚本")
    else:
        _check("PyAV", True, "音视频解析组件可用")

    # 4. vendor 里的 XHS-Downloader（下载组件，安装脚本放进去并校验）。
    vendor = next(iter(sorted(config.vendor_dir().glob("XHS-Downloader*"))), None)
    _check("XHS-Downloader", vendor is not None and vendor.is_dir(),
           vendor.name if vendor else f"{home / 'vendor'} 下没有")

    # 5. 模型文件（语音模型目录非空）。
    models = config.models_dir()
    model_files = [p for p in models.rglob("*") if p.is_file()] if models.is_dir() else []
    _check("语音模型文件", bool(model_files),
           f"{len(model_files)} 个文件" if model_files else f"{models} 为空")

    # 6. 整理模型登录状态：claude_code/codex 查登录（不花钱）；
    #    anthropic/openai_compatible 只查钥匙串里有没有 Key。
    try:
        deps = deps_factory()
        provider_name = settings["llm"]["provider"]
        try:
            provider = deps.make_provider(settings)
            if provider_name in ("claude_code", "codex"):
                ok, reason = provider.check()
                _check(f"整理模型（{provider_name}）", ok, reason)
            else:
                _check(f"整理模型（{provider_name} 的 Key）",
                       deps.key_exists(provider_name), "钥匙串条目")
        except Exception as error:
            _check(f"整理模型（{provider_name}）", False, type(error).__name__)
    except Exception as error:
        _check("整理模型", False, type(error).__name__)

    # LLM Wiki 依赖另一个固定版本的后台和本机命令行助手；只在选了它时
    # 核对，避免给轻量引擎用户制造多余的缺组件提示。这里只查找，不启动后台。
    if settings["engine"] == "llmwiki":
        from . import assets
        from .llm.claude_code import find_claude
        from .llm.codex import find_codex

        ready = assets.llmwiki_ready(home)
        _check("LLM Wiki 后台程序", ready,
               "文件校验通过" if ready else "请运行 xhs-wiki assets install-llmwiki")
        provider_name = settings["llm"]["provider"]
        find_agent = {"claude_code": find_claude, "codex": find_codex}.get(provider_name)
        found = bool(find_agent()) if find_agent is not None else False
        _check("LLM Wiki 整理工具", found,
               f"{provider_name} 已找到" if found else
               ("llmwiki_agent_missing" if find_agent is not None
                else "llmwiki_provider_unsupported"))

    # 7. 知识库目录可写（**不创建任何目录**）。
    #    - 目录已存在：检查它本身能不能写；
    #    - 目录不存在：往上找到第一个存在的父目录，检查那个目录能不能写，
    #      并在结果里写明"将创建"。
    #    之前直接 mkdir 的做法会在真实主目录下建出空的 ~/XHS-Wiki，
    #    doctor 是检查工具，不应该有建目录的副作用。
    try:
        kb_root = Path(settings["kb_root"]).expanduser()
        target = kb_root
        will_create = not kb_root.exists()
        if will_create:
            parent = kb_root.parent
            while not parent.exists():
                parent = parent.parent
            target = parent
        writable = os.access(target, os.W_OK)
        detail = str(kb_root) + ("（将创建）" if will_create else "")
        _check("知识库目录可写", writable, detail)
    except (OSError, KeyError, TypeError) as error:
        _check("知识库目录可写", False, type(error).__name__)

    return 0


# ---------------------------------------------------------------------------
# serve / print-launch-agent
# ---------------------------------------------------------------------------

# LaunchAgent 环境变量里 PATH 的"兜底"部分：常见安装位置（Homebrew、
# 用户本地、系统目录）。PATH 以安装时终端里的 PATH 为主（见
# launch_agent_path），这份列表只追加在最后，保证系统目录一定在。
LAUNCH_AGENT_PATH = ":".join([
    str(Path("~/.local/bin").expanduser()),
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
])

# 写进 plist 的网络相关环境变量：代理的大小写两种写法，
# 加上 Hugging Face 镜像地址 HF_ENDPOINT（语音模型下载会用到），以及
# uv 的包索引和 Python 下载镜像（后台安装组件依赖时同样需要）。
# LaunchAgent 启动的服务读不到 ~/.zshrc 里 export 的变量，安装时
# 不把它们写进 plist，服务里的下载和模型调用就用不上代理。
LAUNCH_AGENT_NETWORK_VARS = (
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "HF_ENDPOINT", "UV_DEFAULT_INDEX", "UV_INDEX_URL",
    "UV_PYTHON_INSTALL_MIRROR",
)


# 系统自带命令所在的目录：在 plist 的 PATH 里排在用户
# 终端的 PATH 之前。它们只能由系统改动，放前面不会让同名的"冒牌命令"
# 抢先被找到；uv、claude 这些系统里没有的命令仍会在后面的
# 用户目录里找到。
SYSTEM_PATH_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def _path_dir_trusted(directory: str, *, stat_fn=os.stat, uid=None) -> bool:
    """PATH 里的一个目录能不能写进后台服务的 PATH。

    后台服务会按 PATH 查找 uv、claude 等程序并执行。目录要是
    别人也能往里放文件，同机其他账户就能放一个同名程序，借用户自己的
    服务执行。所以跳过：
    - "其他人可写"的目录（权限位 o+w，比如误设成 777 的目录）；
    - 属主既不是 root 也不是当前用户、并且"组可写"的目录。
    （Intel Mac 上 Homebrew 的 /usr/local/bin 常见是"属主是本人或 root、
    admin 组可写"，属于正常情况，保留。）读不到权限信息的目录也跳过。
    """
    uid = os.getuid() if uid is None else uid
    try:
        info = stat_fn(directory)
    except OSError:
        return False
    if info.st_mode & stat.S_IWOTH:
        return False
    if info.st_uid not in (0, uid) and info.st_mode & stat.S_IWGRP:
        return False
    return True


def launch_agent_path(python: str, environ=None, *, stat_fn=os.stat,
                      uid=None) -> str:
    """算出写进 plist 的 PATH。

    以前是写死的一串目录：nvm/volta 装的 claude/codex、~/.cargo/bin
    里的 uv，安装时在终端里找得到，服务里却
    找不到。现在按顺序拼：
    1. 启动服务用的 Python 所在目录（venv 的 bin）放最前——本项目自己的
       命令和依赖优先；
    2. 系统目录 /usr/bin、/bin、/usr/sbin、/sbin（放在用户
       PATH 之前，防止用户目录里的同名程序冒充系统命令）；
    3. 当前进程（也就是运行安装脚本的终端）的 PATH，保持原顺序；
    4. LAUNCH_AGENT_PATH 兜底（Homebrew、~/.local/bin 等常见位置）。
    每一项都要满足：非空、绝对路径（相对路径会随工作目录变化，等于
    让任意目录里的同名程序有机会被执行）、不含控制字符（plist 是 XML，
    控制字符会让生成失败）、目录确实存在、别人不能往里写（见
    _path_dir_trusted）；重复的只留第一次出现的位置。
    stat_fn / uid 可注入（测试用）。
    """
    environ = os.environ if environ is None else environ
    candidates = [str(Path(python).parent)]
    candidates += list(SYSTEM_PATH_DIRS)
    candidates += environ.get("PATH", "").split(os.pathsep)
    candidates += LAUNCH_AGENT_PATH.split(os.pathsep)
    result, seen = [], set()
    for entry in candidates:
        if not entry or not os.path.isabs(entry):
            continue
        if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in entry):
            continue
        normalized = os.path.normpath(entry)
        if normalized in seen or not os.path.isdir(normalized):
            continue
        if not _path_dir_trusted(normalized, stat_fn=stat_fn, uid=uid):
            continue
        seen.add(normalized)
        result.append(normalized)
    return os.pathsep.join(result)


def launch_agent_network_env(environ=None) -> dict:
    """从当前环境里挑出要写进 plist 的代理和镜像变量（有值才写）。

    值里带换行等控制字符的直接跳过：plist 是 XML，控制字符会让文件
    不合法，安装脚本随后的 plutil 检查会失败。代理地址可能带账号密码，
    plist 由安装脚本设为 0600，只有本人能读。
    """
    environ = os.environ if environ is None else environ
    picked = {}
    for name in LAUNCH_AGENT_NETWORK_VARS:
        value = environ.get(name)
        if not value or not value.strip():
            continue
        if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value):
            continue
        picked[name] = value
    return picked


def launch_agent_log_path() -> Path:
    """LaunchAgent 日志文件的路径（~/Library/Logs/xhs-wiki/daemon.log）。

    只算路径、不建目录：print-launch-agent 保证零副作用（不写 ~/Library）。
    日志必须在 ~/Documents 之外——macOS 的隐私保护不允许 launchd 替
    LaunchAgent 进入"文稿"里的目录（实际踩过，表现为 exit 78）。
    """
    return Path.home() / "Library" / "Logs" / "xhs-wiki" / "daemon.log"


def build_launch_agent(python_path: str = None, environ=None) -> dict:
    """构造 LaunchAgent 的配置字典（plist）。只生成内容，不写任何文件。

    - Label 用 config.LAUNCH_AGENT_LABEL（io.github.xhs-wiki.service）；
    - ProgramArguments 是 <python> -I -m xhs_wiki.cli serve。
      ``-I``（隔离模式）不把当前目录放进模块搜索路径——LaunchAgent 的
      工作目录是主目录，主目录里要是有 secrets.py、json.py 这类与标准库
      同名的文件，不加 -I 时服务会加载它们（轻则崩溃，重则执行任意代码）；
    - RunAtLoad / KeepAlive 为真（登录后自动启动、崩溃后自动拉起）；
    - WorkingDirectory 是用户主目录（不能在 ~/Documents 里；serve 启动后
      会自己切到 APP_HOME）；
    - 环境变量：HOME、USER、PATH（安装时的 PATH，见 launch_agent_path），
      以及有值的代理和镜像变量（见 launch_agent_network_env）。
    ``environ`` 可注入（测试用），默认读当前进程的环境变量。
    """
    environ = os.environ if environ is None else environ
    python = python_path or sys.executable
    log_path = str(launch_agent_log_path())
    env = {
        "HOME": environ.get("HOME", ""),
        "USER": environ.get("USER", ""),
        "PATH": launch_agent_path(python, environ),
    }
    env.update(launch_agent_network_env(environ))
    return {
        "Label": config.LAUNCH_AGENT_LABEL,
        "ProgramArguments": [python, "-I", "-m", "xhs_wiki.cli", "serve"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(Path.home()),
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        "EnvironmentVariables": env,
    }


def print_launch_agent(python_path: str = None) -> None:
    """把 LaunchAgent 配置用 plistlib 序列化成 XML 打印到标准输出。

    输出必须能直接重定向存成 plist 文件，所以除了 plist 本身什么都不打印
    （不打印提示语、不建任何目录）。"""
    import plistlib
    _raw_print(plistlib.dumps(build_launch_agent(python_path)).decode("utf-8"), end="")


def _raw_print(text: str, end: str = "\n") -> None:
    """绕过 _print 钩子的直接打印：plist 输出不能被测试钩子改写格式。"""
    print(text, end=end)


def _cmd_serve(args) -> int:
    """serve 子命令：启动守护进程（HTTP 控制台 + 定时线程）。

    启动时清理残留状态（假运行中、过期二维码），然后常驻。
    端口默认 18765（config.DEFAULT_PORT）。
    """
    from . import daemon
    daemon.serve(args.port if args.port is not None else config.DEFAULT_PORT)
    return 0


def _cmd_print_launch_agent(args) -> int:
    """print-launch-agent 子命令：只打印 plist 配置，不写入任何位置。

    生成失败（比如环境变量里有 plist 不能表示的字符）时，
    错误说明写到**标准错误**。安装脚本把标准输出重定向成 plist 临时文件，
    写到标准输出的话用户在终端里什么都看不到，只看到安装失败。
    """
    try:
        print_launch_agent(args.python)
    except (ValueError, TypeError) as error:
        print(f"错误：生成 LaunchAgent 配置失败（{type(error).__name__}）",
              file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# open（带访问令牌打开控制台）
# ---------------------------------------------------------------------------

# open_console 的结果代码 → 给用户看的中文说明（{port} 运行时填入）。
OPEN_MESSAGES = {
    "opened": "已在浏览器打开控制台。",
    "service_not_running": (
        "控制台服务没有在运行（127.0.0.1:{port} 没有回应）。"
        "请先启动服务：重新登录 macOS 让后台服务自动启动，"
        "或在终端运行 xhs-wiki serve，然后再运行 xhs-wiki open。"),
    "token_missing": (
        "找不到控制台访问令牌。请确认服务和这条命令用的是同一个数据目录"
        "（环境变量 XHS_WIKI_HOME），必要时重启服务后再运行 xhs-wiki open。"),
    "service_identity_mismatch": (
        "端口 {port} 上回应的不是本机这份 xhs-wiki 服务（或者访问令牌已经更换），"
        "为安全起见没有打开。请重启 xhs-wiki 服务后再试；"
        "如果端口被其他程序占用，请先让它退出。"),
    # 不建议"在访达里打开 open-console.html"——跳转页里
    # 只有 2 分钟内有效的一次性登录码，过一会儿再打开它已经没用；
    # 重新运行 xhs-wiki open 会重新核对服务身份、签发新的登录码。
    "open_failed": (
        "没能自动打开浏览器。请稍后重新运行 xhs-wiki open。"),
}


def _cmd_open(args, opener=None) -> int:
    """open 子命令：确认服务在运行且身份无误后，用跳转页打开控制台。

    流程全部在 console_auth.open_console 里（探活 + 身份核对 → 写 0600
    跳转页 → ``/usr/bin/open <跳转页>``）。令牌只写进 0600 的跳转页文件，
    绝不出现在 URL、命令行参数或这里的输出里。
    opener 可注入（测试传记录参数的假实现，不真的打开浏览器）。
    退出码：打开成功 0，其余 1（install.sh 据此提示手动命令）。
    """
    from . import console_auth
    port = args.port if args.port is not None else config.DEFAULT_PORT
    result = console_auth.open_console(config.app_home(), port, opener=opener)
    _print(OPEN_MESSAGES[result].format(port=port))
    return 0 if result == "opened" else 1


# ---------------------------------------------------------------------------
# assets（安装资源的下载和校验，install.sh 调用）
# ---------------------------------------------------------------------------

def _cmd_assets(args) -> int:
    """assets 子命令：下载组件 / 语音模型 / LLM Wiki 的下载、安装和核对。

    真实的网络和 uv 都在 assets.py 里；这里只做分发和打印。模型已齐全时
    install-model 先提示再跳过（安装脚本可重复执行）。
    """
    from . import assets
    home = config.app_home()
    if args.assets_command == "install-llmwiki":
        _print("开始下载并校验 LLM Wiki 后台程序……")
        binary = assets.install_llmwiki(home)
        # 路径只能由本机安装器写回，控制台的设置白名单不因此扩大。
        ServiceStore(home).update_settings({"llmwiki_binary": str(binary)})
        _print("LLM Wiki 后台程序已就绪，已自动配置")
        return 0
    if args.assets_command == "install-downloader":
        _print("开始下载并安装 XHS-Downloader（下载组件）……")
        directory = assets.install_xhs_downloader(home)
        _print(f"已安装到 {directory}")
        return 0
    if args.assets_command == "install-model":
        ok, missing = assets.verify_model(args.name, home)
        if ok:
            _print(f"模型 {args.name} 已齐全，跳过下载")
            return 0
        _print(f"开始下载语音模型 {args.name}（缺失 {len(missing)} 个文件）……")
        assets.install_model(args.name, home)
        _print(f"已安装到 {config.models_dir()}")
        return 0
    # verify：不指定名字就两个模型都查。
    names = [args.name] if args.name else list(assets.MODEL_NAMES)
    failed = False
    for name in names:
        ok, missing = assets.verify_model(name, home)
        mark = "✓" if ok else "✗"
        detail = "齐全" if ok else "缺失：" + "、".join(missing)
        _print(f"{mark} 模型 {name}：{detail}")
        failed = failed or not ok
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv=None, deps_factory=None, *, opener=None) -> int:
    """命令行入口。返回进程退出码（0 正常，1 需要用户处理/失败）。

    deps_factory：构造 RunnerDeps 的工厂（测试注入假依赖用；默认
    runner.default_deps）。opener：open 子命令打开跳转页用的函数
    （测试注入记录参数的假实现；默认真的执行 /usr/bin/open）。
    --home 必须在任何 config 调用之前生效，所以在分发子命令前先设置
    环境变量。--home 写在子命令前面或后面都可以（子命令的
    parser 也定义了同名参数）。
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.home:
        # config.app_home 每次调用都重新读环境变量，先设再往下走即可。
        os.environ["XHS_WIKI_HOME"] = str(Path(args.home).expanduser())
    try:
        if args.command == "run":
            return _cmd_run(args, deps_factory)
        if args.command == "login":
            return _cmd_login(args)
        if args.command == "status":
            return _cmd_status(args)
        if args.command == "cleanup":
            return _cmd_cleanup(args, deps_factory)
        if args.command == "doctor":
            return _cmd_doctor(args, deps_factory)
        if args.command == "serve":
            return _cmd_serve(args)
        if args.command == "print-launch-agent":
            return _cmd_print_launch_agent(args)
        if args.command == "open":
            return _cmd_open(args, opener)
        if args.command == "assets":
            return _cmd_assets(args)
        parser.error(f"未知子命令：{args.command}")  # pragma: no cover
    except ValueError as error:
        # 可预料的失败（设置不合法、引擎不可用等）：打印错误代码，退出码 1。
        _print(f"错误：{error}")
        return 1
    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
