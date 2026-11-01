# -*- coding: utf-8 -*-
"""runner.py —— 一次运行的编排器：按台账把收藏从"已发现"推进到"已整理"。

这个文件做什么：把一次运行的步骤按顺序串起来——
    预检 → 扫描收藏 → 登记台账 → 每周下载重试 → 处理
    （download → parse → ingest → distill 四个环节）→ 清理原件 → 汇总
并把过程写进状态文件（status.json）、结果写进运行记录（runs.jsonl）。

在整体里的位置：docs/architecture.md §9。被 cli（run 子命令）和
daemon 调用；它自己调用 scan / ledger / download / parse / engines /
cleanup / notify 这些下层模块。

设计要点：
- 处理按台账逐条推进（next_batch → 执行 →
  advance / fail），不另起子进程跑处理循环；
- 预检：setup_completed、磁盘、整理模型（claude_code/codex 查登录，
  anthropic/openai_compatible 只查钥匙串里有没有 Key，不发请求）、引擎
  health()；
- "熔断"：模型抛 LLMError（not_logged_in / quota_or_rate_limit /
  auth_failed）时立即停止所有模型环节，这一轮不再调模型。
- 下载熔断：连续三条都拿不到信息或出现网络错误时，本轮不再下载；
  已下载的笔记继续处理，结果记为 needs_user，避免一小时后自动追加请求。

中断恢复：
- ingest 环节不再依赖本次运行的内存：每条 parsed 笔记都从磁盘加载
  解析包（deps.load_package）并核对 sha256，不一致或加载失败记
  package_changed——上次运行中断的笔记下一次照常入库；
- distill 环节顺带接续台账里还停在 ingested 的笔记。

两条贯穿始终的规矩：
1. 所有外部依赖（浏览器、下载器、解析、引擎、模型、时钟、通知、磁盘
   检查）都通过 RunnerDeps 注入；真实环境用 default_deps()，测试用假实现。
2. 暂停永远在"安全点"发生：步骤之间停在边界，处理期间每条笔记之间
   检查一次 PAUSE 标记。

红线：签名链接只从台账读出、直接传给下载器，绝不写进日志、状态、
运行记录或异常信息。
"""

import fcntl
import json
import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from . import config
from .engines.base import package_from_dict
from .ledger import Ledger
from .llm.base import LLMError
from .store import ServiceStore, now_beijing

# ---------------- 固定参数（docs/architecture.md §9） ----------------

# 磁盘剩余低于 10 GiB 就不开跑（下载和处理都需要空间）。
MIN_FREE_BYTES = 10 * 1024 ** 3
# 整个处理阶段（download→parse→ingest→distill）的时间上限。
PROCESS_TIMEOUT = timedelta(hours=12)
# parse / ingest / distill 环节每批取的条数（download 环节用 max_new_per_run）。
BATCH_SIZE = 50
# 每周下载重试认领的失败原因（扫描刚更新过签名链接，这些原因值得再试）。
RETRYABLE_DOWNLOAD_ERRORS = ("metadata_unavailable", "network_error")
# 下载熔断只统计疑似访问受限的连续失败；与每周重试规则分开定义，
# 避免以后调整重试范围时，无意改变保护账号的停止条件。
DOWNLOAD_BREAK_CODES = ("metadata_unavailable", "network_error")
DOWNLOAD_BREAK_LIMIT = 3
# 熔断后算 incomplete 的 LLM 错误代码（稍后追加运行会重试）。
LLM_RETRY_CODES = ("not_logged_in", "quota_or_rate_limit")
# 熔断后算 needs_user 的 LLM 错误代码。
LLM_AUTH_CODES = ("auth_failed",)

# 步骤的中文名（状态文件里 step 存英文代码，控制台显示中文）。
STEP_LABELS = {
    "preflight": "预检",
    "scan": "扫描收藏",
    "register": "登记台账",
    "download_retry": "重试下载失败的笔记",
    "process": "下载解析入库整理",
    "cleanup": "清理原件",
    "summarize": "汇总",
}

# 结束时给用户看的一句话（按结果集中定义，方便统一维护）。
RESULT_MESSAGES = {
    "completed": "本次运行完成",
    "incomplete": "还有笔记没到终态，稍后会自动追加运行",
    "paused": "已在安全点暂停，可在控制台点「继续」",
    "needs_user": "有需要你处理的事项，请在控制台查看",
    "error": "服务运行出错，请查看日志",
    "skipped": "已有运行在进行中，本次没有开跑",
}

# setup_incomplete 是 runner 自己用的"需要用户处理"代码，不在 store 的
# 事项表里。在 runner 模块内补文案：import
# 时合并进 store.NEEDS_USER_TITLES（add_needs_user 校验代码要用它）；
# notify.TEMPLATES 不动——tests/test_notify.py 锁定了那张表的代码集合，
# 所以 setup_incomplete 暂不发系统通知（用户打开控制台就能看到这个事项）。
SETUP_INCOMPLETE_TITLE = "请先在控制台完成首次设置"

# llmwiki_review_required 是 LLM Wiki 引擎用的"需要用户处理"代码（LLM Wiki 有
# 一组整理改动需要用户在桌面版或控制台里确认）。沿用同样的做法，文案
# 只合并进 store 的表，notify.TEMPLATES 的快照保持原样。
LLMWIKI_REVIEW_REQUIRED_TITLE = "LLM Wiki 有一组整理改动需要你在桌面版或控制台里确认"


def _register_extra_titles() -> None:
    """把 setup_incomplete 的文案合并进 store 的文案表。

    顺序很关键：先 import notify（它的 TEMPLATES 在 import 时从
    NEEDS_USER_TITLES 拷贝一份快照），再往 store 的表里合并——这样无论
    两个模块谁先被加载，notify.TEMPLATES 都不会出现新代码（tests/test_notify.py
    锁定了那张表的代码集合）。setdefault 保证重复 import 不会覆盖；store
    将来自己加上这个代码时也以 store 的文案为准。
    """
    from . import notify as notify_module  # noqa: F401  先触发 notify 的快照
    from . import store as store_module
    store_module.NEEDS_USER_TITLES.setdefault("setup_incomplete",
                                              SETUP_INCOMPLETE_TITLE)
    store_module.NEEDS_USER_TITLES.setdefault("llmwiki_review_required",
                                              LLMWIKI_REVIEW_REQUIRED_TITLE)


_register_extra_titles()


def _truncate(text, limit):
    """把文本截断到 limit 个字（超长时加省略号），防止异常信息把记录撑爆。"""
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# 依赖注入：RunnerDeps 与 default_deps
# ---------------------------------------------------------------------------

@dataclass
class RunnerDeps:
    """一次运行需要的全部外部依赖。

    run() 只认这个对象，不直接碰浏览器 / 下载器 / 模型 / 时钟，这样
    测试时可以整套换成假实现（见 tests/test_runner.py）。
    解析（parse 的 build_package）和具体引擎这些模块也只
    出现在这里：真实实现延迟 import，测试用假实现。
    """
    store: ServiceStore
    ledger: Ledger
    now: object                    # () -> datetime（北京时间）
    sleep: object                  # (秒) 睡一会儿，测试里是空操作
    notify: object                 # (code) 发一条系统通知（内部自带去重）
    disk_free_bytes: object        # (path) -> 剩余字节数
    scan_favorites: object         # (profile_url, *, headless) -> ScanResult
    detect_profile_url: object     # () -> 主页地址或 None
    download_note: object          # (note_id, signed_url, work_dir, *, vendor_dir) -> dict
    build_package: object          # (note_id, work_dir, download) -> {"package", "package_sha256"}
    load_package: object = None    # (work_dir) -> 解析包 dict（parse.package.load_package 的形状）
    make_engine: object = None     # (settings) -> Engine（health/ingest/distill/…）
    make_provider: object = None   # (settings) -> Provider（check()）
    key_exists: object = None      # (provider 名) -> 钥匙串里有没有该提供方的 Key
    cleanup_apply: object = None   # (ledger, engine, store) -> 清理结果 dict
    log: object = None             # (文本) 记一行日志，默认写到 APP_HOME/logs


def _default_log(home: Path):
    """默认日志函数：往 APP_HOME/logs/run-<日期>.log 追加一行（0600）。

    日志里只写步骤和数字，绝不写链接、标题等敏感内容。
    """
    def write(text: str) -> None:
        log_dir = home / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "run.log"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(text + "\n")
    return write


def default_deps() -> RunnerDeps:
    """构造真实环境的依赖。

    真实实现对解析（build_package）和引擎模块做延迟
    import：只有真正运行到那一步才 import；import 失败会直接抛
    ImportError，由 run() 兜成 error 记录而不是让守护进程崩溃。
    """
    from . import scan, download

    home = config.app_home()
    store = ServiceStore(home)
    ledger = Ledger(config.ledger_path())

    def _scan_favorites(profile_url, *, headless):
        return scan.scan_favorites(profile_url, headless=headless,
                                    user_data_dir=config.browser_profile_dir())

    def _detect_profile_url():
        return scan.detect_profile_url(config.browser_profile_dir())

    def _download_note(note_id, signed_url, work_dir, *, vendor_dir=None):
        return download.download_note(note_id, signed_url, work_dir,
                                      vendor_dir=vendor_dir or config.vendor_dir())

    def _build_package(note_id, work_dir, download_result):
        # parse.package.build_package 返回扁平的解析包 dict（自带 package_sha256）；
        # 这里包一层成 {"package", "package_sha256"}，保持 RunnerDeps 的接口约定不变。
        # 显式传入模型目录和设置里选的语音模型。原来两个参数都
        # 没传——用户在设置里选了快速模式（sensevoice/whisper 二选一）也
        # 不生效，总是用 build_package 的默认值 whisper；models_dir 也没
        # 指向 config.models_dir()。设置每次调用时现读（而不是闭包里存
        # 一份），保证运行中改设置后下一次解析就用新的。
        from .parse.package import build_package
        settings_now = store.load_settings()
        outcome = build_package(note_id, Path(work_dir), download_result,
                                models_dir=config.models_dir(),
                                asr_model=settings_now["asr_model"])
        return {"package": outcome,
                "package_sha256": outcome["package_sha256"]}

    def _load_package(work_dir):
        # 从磁盘读上一次运行已经解析好的解析包（ingest 不依赖
        # 本次运行的内存放的解析包，中断后也能继续入库）。parse.package 的
        # load_package 对缺失/损坏/不一致统一抛 ValueError("package_changed")。
        from .parse.package import load_package
        return load_package(Path(work_dir))

    def _make_provider(settings):
        from .llm import make_provider
        from .llm.keys import Keychain
        chain = Keychain()

        def key_provider():
            # make_provider 的 key_provider 参数：取 API Key 的回调。
            return chain.load(settings["llm"].get("provider"))

        return make_provider(settings["llm"], key_provider=key_provider)

    def _make_engine(settings):
        # 轻量引擎（markdown）和 LLM Wiki 引擎（llmwiki）。
        if settings.get("engine") == "llmwiki":
            from .engines.llmwiki.engine import LlmWikiEngine
            from pathlib import Path as _Path
            return LlmWikiEngine(
                _Path(settings["kb_root"]).expanduser(),
                config.app_home(), settings)
        if settings.get("engine") != "markdown":
            raise ValueError("engine_not_available:" + str(settings.get("engine")))
        from .engines.markdown.engine import MarkdownEngine
        return MarkdownEngine(settings["kb_root"], _make_provider(settings))

    def _key_exists(provider_name):
        # anthropic / openai_compatible 只检查钥匙串里有没有条目，
        # 不发任何请求（预检不花钱，也不验证 Key 是否有效）。
        from .llm.keys import Keychain
        try:
            Keychain().load(provider_name)
            return True
        except LLMError:
            return False

    def _cleanup_apply(ledger, engine, store):
        from . import cleanup as cleanup_module
        candidates = cleanup_module.find_candidates(ledger, engine)
        return cleanup_module.apply(candidates, ledger=ledger, engine=engine,
                                    store=store, now=now_beijing)

    def _notify(code):
        from . import notify as notify_module
        return notify_module.notify(code, store, now=now_beijing())

    return RunnerDeps(
        store=store,
        ledger=ledger,
        now=now_beijing,
        sleep=lambda seconds: __import__("time").sleep(seconds),
        disk_free_bytes=lambda path: __import__("shutil").disk_usage(path).free,
        scan_favorites=_scan_favorites,
        detect_profile_url=_detect_profile_url,
        download_note=_download_note,
        build_package=_build_package,
        load_package=_load_package,
        make_engine=_make_engine,
        make_provider=_make_provider,
        key_exists=_key_exists,
        cleanup_apply=_cleanup_apply,
        notify=_notify,
        log=_default_log(home),
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

class _StopRun(Exception):
    """内部信号：某个步骤决定了"这次运行到此为止"，带着结果信息跳到收尾。

    只在 run() 内部使用，不会跑到外面去。
    """

    def __init__(self, result, stop_reason, message):
        super().__init__(result)
        self.result = result
        self.stop_reason = stop_reason
        self.message = message


def _base_record(run_id, trigger, started):
    """运行记录的初始骨架（数字先置零/空，收尾时补齐 ended_at 等）。"""
    return {
        "run_id": run_id,
        "trigger": trigger,
        "started_at": started.isoformat(),
        "ended_at": None,
        "result": None,
        "stop_reason": None,
        "message": "",
        "scan": {"visible": None, "nominal": None, "incomplete": False},
        "new_found": 0,
        "retry": {"ran": False, "retried": 0},
        # 本轮每个环节成功/失败的条数（processed.ingest.deferred 是
        # "内容不足进入暂缓"的条数）。
        "processed": {
            "download": {"ok": 0, "failed": 0},
            "parse": {"ok": 0, "failed": 0},
            "ingest": {"ok": 0, "deferred": 0, "failed": 0},
            "distill": {"ok": 0, "failed": 0},
        },
        # 熔断时记下 LLM 错误代码（None 表示没熔断）。
        "llm_break": None,
        "cleanup": {"ran": False, "notes": 0, "freed_bytes": 0, "skipped": 0},
        "counts": {},
    }


def run(trigger, *, deps: RunnerDeps) -> dict:
    """执行一次完整运行，返回写进 runs.jsonl 的那条记录。

    trigger 是触发方式：schedule（定时）/ manual（手动）/ followup（追加）/
    after_login（扫码成功后）/ resume（继续）。
    """
    started = deps.now()
    run_id = f"run-{started:%Y%m%d-%H%M%S}"
    # 第 0 步：单实例锁（APP_HOME/runner.lock，非阻塞）。拿不到说明另一个
    # 运行在进行，记一条 skipped 直接返回。锁靠进程持有：函数返回或进程
    # 退出时自动释放（关闭文件描述符即释放）。
    lock_path = deps.store.root / "runner.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            record = _base_record(run_id, trigger, started)
            record.update(result="skipped", stop_reason="already_running",
                          message=RESULT_MESSAGES["skipped"])
            record["ended_at"] = deps.now().isoformat()
            deps.store.append_run(record)
            return record
        return _run_locked(trigger, deps, run_id, started)
    finally:
        os.close(fd)


def _run_locked(trigger, deps, run_id, started):
    """已经拿到锁之后的主流程。任何异常都被兜住，保证一定写记录。"""
    store = deps.store
    ledger = deps.ledger
    record = _base_record(run_id, trigger, started)
    # 本次运行遇到的 needs_user 代码（收尾时要发通知、决定最终状态）。
    encountered = []
    # 扫描是否成功（决定收尾时能不能清掉旧的 xhs_login 事项）。
    scan_succeeded = False
    # 熔断代码（not_logged_in / quota_or_rate_limit / auth_failed）或 None。
    llm_break = None
    # 处理阶段是否超时（12 小时上限）。
    timed_out = False
    # 是否在处理阶段看到过暂停请求（决定结果是否算 paused）。
    pause_seen = False
    current_step = "preflight"

    def log(text):
        if deps.log:
            deps.log(f"[{deps.now().isoformat()}] {text}")

    def needs_user(code, detail=""):
        """登记一个"需要用户处理"的事项，并结束这次运行。"""
        store.add_needs_user(code, _truncate(detail, 200), now=deps.now())
        encountered.append(code)
        raise _StopRun("needs_user", code, RESULT_MESSAGES["needs_user"])

    def pause_barrier(next_step):
        """步骤之间的安全点：用户请求了暂停就停在这里。"""
        if store.pause_requested():
            raise _StopRun("paused", "pause_requested", RESULT_MESSAGES["paused"])
        return next_step

    def check_pause_between_notes():
        """处理阶段每条笔记之间的安全点。返回 True 表示要停。"""
        if store.pause_requested():
            return True
        return False

    def break_on_llm_error(error):
        """模型调用抛 LLMError 时的熔断处理。

        - not_logged_in / quota_or_rate_limit：记为熔断，本轮不再调模型，
          结果算 incomplete（稍后追加运行会重试）；
        - auth_failed：Key 无效，用户必须处理 → needs_user llm_unavailable。
        两种都**不**把失败计入笔记的 attempts（不是笔记本身的问题）。
        返回 True 表示已经处理完（调用方直接停止模型环节）。
        """
        nonlocal llm_break
        code = getattr(error, "code", None)
        if code in LLM_RETRY_CODES:
            llm_break = code
            log(f"模型调用熔断：{code}，本轮停止模型环节")
            return True
        if code in LLM_AUTH_CODES:
            needs_user("llm_unavailable", f"模型调用被拒绝（{code}）")
        # 其他 LLM 错误（timeout / bad_output / unavailable）不算熔断，
        # 由各环节按普通失败处理。
        return False

    engine = None
    settings = None
    ingested_ids = []  # 本轮 ingest 成功的笔记（distill 一次处理）

    try:
        settings = store.load_settings()
        # 改选全部后立即恢复首次跳过的记录；扫描失败也不把选择丢掉。
        # skipped 是单独终态，恢复后才会进入 discovered 的待下载队列。
        if settings.get("first_import_limit") is None:
            ledger.restore_skipped()
        store.write_status(now=deps.now(), state="running", step="preflight",
                           run_id=run_id, trigger=trigger,
                           started_at=started.isoformat(),
                           message="正在预检（设置 / 磁盘 / 模型 / 引擎）")

        # ---------------- 第 1 步：预检 ----------------
        if not settings.get("setup_completed"):
            needs_user("setup_incomplete", "首次设置还没有完成")
        free_bytes = deps.disk_free_bytes(config.app_home())
        if free_bytes < MIN_FREE_BYTES:
            needs_user("disk_low",
                       f"数据目录所在磁盘剩余 {free_bytes / 1024 ** 3:.1f} GiB")
        # 整理模型：CLI 类（claude_code / codex）调 provider.check() 只查
        # 登录状态不花钱；API 类（anthropic / openai_compatible）不发请求，
        # 只查钥匙串里有没有 Key。
        provider_name = settings["llm"]["provider"]
        try:
            provider = deps.make_provider(settings)
            if provider_name in ("claude_code", "codex"):
                ok, reason = provider.check()
                if not ok:
                    needs_user("llm_unavailable", _truncate(reason, 200))
            else:
                if not deps.key_exists(provider_name):
                    needs_user("llm_unavailable",
                               "钥匙串里没有该提供方的 API Key")
        except LLMError as error:
            # 预检阶段模型就不可用：直接交给用户处理（熔断逻辑只用于处理
            # 阶段——预检都没过，这一轮本来就不会开始调模型）。
            needs_user("llm_unavailable",
                       _truncate(getattr(error, "code", ""), 200))
        # 引擎：health() 不通过就交给用户处理。
        try:
            engine = deps.make_engine(settings)
            ok, reason = engine.health()
        except (ImportError, ValueError) as error:
            ok, reason = False, _truncate(str(error), 200)
        if not ok:
            needs_user("engine_unavailable", _truncate(reason, 200))

        # ---------------- 第 2 步：扫描 ----------------
        current_step = pause_barrier("scan")
        store.write_status(now=deps.now(), step="scan", message="正在扫描收藏页")
        profile_url = settings.get("profile_url")
        if not profile_url:
            # 主页地址还没识别过：先静默识别一次，识别到就写回设置。
            profile_url = deps.detect_profile_url()
            if profile_url:
                store.update_settings({"profile_url": profile_url})
                settings = store.load_settings()
                log("已识别主页地址并写回设置")
            else:
                needs_user("xhs_login", "未登录且未能识别主页地址")
        scan_result = deps.scan_favorites(profile_url, headless=settings["headless"])
        record["scan"] = {"visible": scan_result.get("visible"),
                          "nominal": scan_result.get("nominal"),
                          "incomplete": scan_result.get("reason") == "scan_incomplete"}
        status = scan_result.get("status")
        # 扫描不完整（ok + scan_incomplete）时照常往下走：收藏按时间倒序，
        # 新收藏总在最前面，扫到的部分足够覆盖当天新增。
        if status == "login_required":
            needs_user("xhs_login", "收藏页要求登录")
        elif status != "ok":
            needs_user("xhs_blocked", _truncate(scan_result.get("reason"), 200)
                       or "页面被拦截或结构无法识别")
        # 扫描成功才清掉旧的 xhs_login 事项（上次登录过期留下的）。
        store.clear_needs_user("xhs_login")
        scan_succeeded = True

        # ---------------- 第 3 步：登记台账 ----------------
        current_step = pause_barrier("register")
        # 把"这次扫描是否完整"一起交给台账——扫描不完整、
        # 又没看全前 N 条时不建立首次范围的基线（规则见
        # Ledger._apply_first_import）。
        added = ledger.upsert_discovered(
            scan_result.get("items") or [],
            first_import_limit=settings.get("first_import_limit"),
            scan_complete=not record["scan"]["incomplete"],
        )
        record["new_found"] = len(added)

        # ---------------- 第 4 步：每周下载重试 ----------------
        # 本次扫描不完整时跳过：重试要用的新签名链接在收藏列表很深的
        # 位置，不完整的扫描里没有。
        current_step = pause_barrier("download_retry")
        scheduler = store.read_scheduler()
        last_retry = scheduler.get("last_download_retry_at")
        due = last_retry is None
        if last_retry is not None:
            from datetime import datetime
            age = deps.now() - datetime.fromisoformat(last_retry)
            due = age >= timedelta(days=settings["download_retry_every_days"])
        if due and not record["scan"]["incomplete"]:
            retried = 0
            for note in ledger.all_notes("failed:download"):
                if note.get("last_error") in RETRYABLE_DOWNLOAD_ERRORS:
                    ledger.retry(note["note_id"])
                    retried += 1
            # 时间戳照常更新（即使这轮没有可重试的笔记，下次也按一周后再查）。
            store.write_scheduler(last_download_retry_at=deps.now().isoformat())
            record["retry"] = {"ran": True, "retried": retried}
            if retried:
                log(f"每周下载重试：重置 {retried} 条")

        # ---------------- 第 5 步：处理（四个环节按台账推进） ----------------
        current_step = pause_barrier("process")
        store.write_status(now=deps.now(), step="process",
                           message="正在下载、解析、入库、整理")
        process_started = deps.now()
        max_new = settings.get("max_new_per_run", 100)

        def out_of_time():
            return deps.now() - process_started >= PROCESS_TIMEOUT

        # ---- download：discovered → downloaded（本轮最多 max_new_per_run 条）----
        # 连续计数只属于本轮；成功或其他失败都会打断这段目标错误序列。
        consecutive_download_failures = 0
        batch = ledger.next_batch("discovered", max_new)
        for note in batch:
            if check_pause_between_notes():
                pause_seen = True
                break
            if out_of_time():
                timed_out = True
                break
            note_id = note["note_id"]
            signed_url = ledger.signed_url(note_id)
            if not signed_url:
                # 台账里没有签名链接（理论上不会发生）：按失败记录，代码
                # 沿用 metadata_unavailable，和下载器返回的失败共用计数，
                # 绝不能把 None 传给下载器。
                result = {"status": "failed", "code": "metadata_unavailable"}
            else:
                result = deps.download_note(note_id, signed_url,
                                            config.work_dir(note_id),
                                            vendor_dir=config.vendor_dir())
            if result.get("status") == "ok":
                # 把下载结果写到工作目录（parse 环节要读），权限 0600。
                _write_private_json(config.work_dir(note_id) / "download.json",
                                    result)
                ledger.advance(note_id, "downloaded",
                               media_kind=result.get("media_kind"))
                record["processed"]["download"]["ok"] += 1
                consecutive_download_failures = 0
            else:
                code = result.get("code")
                ledger.fail(note_id, "discovered", code)
                record["processed"]["download"]["failed"] += 1
                consecutive_download_failures = (
                    consecutive_download_failures + 1
                    if code in DOWNLOAD_BREAK_CODES else 0
                )
                if consecutive_download_failures >= DOWNLOAD_BREAK_LIMIT:
                    # 先照常记下第三条的失败次数，再仅停止下载循环。
                    # 不调用 needs_user()：它会结束整轮，导致已下载内容
                    # 无法继续解析、入库、整理和清理。encountered 会让
                    # 汇总优先判为 needs_user，阻止 incomplete 的追加运行。
                    store.add_needs_user("xhs_download_blocked", "", now=deps.now())
                    encountered.append("xhs_download_blocked")
                    break

        # ---- parse：downloaded → parsed ----
        if not (pause_seen or timed_out):
            batch = ledger.next_batch("downloaded", BATCH_SIZE)
            for note in batch:
                if check_pause_between_notes():
                    pause_seen = True
                    break
                if out_of_time():
                    timed_out = True
                    break
                note_id = note["note_id"]
                work = config.work_dir(note_id)
                download_data = _read_json(work / "download.json", {})
                try:
                    outcome = deps.build_package(note_id, work, download_data)
                except ValueError as error:
                    # 解析失败：错误代码抛 ValueError（可预料的失败）。
                    ledger.fail(note_id, "downloaded", str(error))
                    record["processed"]["parse"]["failed"] += 1
                    continue
                ledger.advance(note_id, "parsed",
                               package_sha256=outcome["package_sha256"])
                record["processed"]["parse"]["ok"] += 1

        # ---- ingest：parsed → ingested / deferred（模型环节） ----
        # 不依赖本次运行内存里的 packages 字典——只要台账里
        # 处于 parsed 的笔记，一律从磁盘重新加载解析包（上次运行在 parse
        # 之后、ingest 之前中断的笔记，这样也能接着入库）。
        if not (pause_seen or timed_out) and not llm_break:
            batch = ledger.next_batch("parsed", BATCH_SIZE)
            if batch and deps.load_package is None:
                # 依赖没配好是编程错误：按未预期错误收尾，而不是误记到笔记上。
                raise ValueError("load_package_missing")
            loaded = {}
            for n in batch:
                note_id = n["note_id"]
                work = config.work_dir(note_id)
                try:
                    data = deps.load_package(work)
                    # 核对 sha256：解析包在解析之后被改过（或磁盘损坏）
                    # 就不能入库，按 package_changed 记失败。
                    if data.get("package_sha256") != n.get("package_sha256"):
                        raise ValueError("package_changed")
                    loaded[note_id] = package_from_dict(
                        data, n.get("list_position") or 0)
                except (OSError, ValueError, KeyError, TypeError):
                    # load_package 自己会抛 ValueError("package_changed")；
                    # 文件缺失/损坏等也统一按 package_changed 记（加载失败
                    # 即失败，不猜测）。
                    ledger.fail(note_id, "parsed", "package_changed")
                    record["processed"]["ingest"]["failed"] += 1
            batch = [n for n in batch if n["note_id"] in loaded]
            if batch:
                try:
                    results = engine.ingest(
                        [loaded[n["note_id"]] for n in batch])
                except LLMError as error:
                    # 引擎熔断前已经把成功卡片落库；先同步这些逐条结果，再停。
                    # 尚未开始或被熔断的笔记仍停在 parsed，不虚增失败次数。
                    completed = getattr(error, "completed_results", [])
                    _apply_ingest(ledger, record, engine.name, batch, completed,
                                  ingested_ids, require_all=False)
                    if not break_on_llm_error(error):
                        # 没有逐条结果的批次异常无法归责给每一条笔记。
                        # 仅当异常明确携带失败编号时记一次，否则保留原状报错。
                        failed_id = getattr(error, "failed_note_id", None)
                        if failed_id not in loaded:
                            raise
                        ledger.fail(failed_id, "parsed", error.code)
                        record["processed"]["ingest"]["failed"] += 1
                else:
                    # 模型调用已完成，先完整记账，再在下一环节检查暂停；
                    # 不能因中途暂停丢掉已落库的后半批结果。
                    _apply_ingest(ledger, record, engine.name, batch, results,
                                  ingested_ids)
                if check_pause_between_notes():
                    pause_seen = True

        # ---- distill：ingested → distilled（模型环节，本轮一次调用） ----
        # 处理对象 = 本轮 ingest 成功的笔记 + 台账里还停在 ingested 的笔记
        # （上次运行在 distill 之前中断的笔记也要接着整理，否则它们
        # 会永远停在 ingested，每次运行都判 incomplete）。
        distill_ids = list(ingested_ids)
        if not (pause_seen or timed_out) and not llm_break:
            for n in ledger.next_batch("ingested", BATCH_SIZE):
                if n["note_id"] not in distill_ids:
                    distill_ids.append(n["note_id"])
        if distill_ids and not (pause_seen or timed_out) and not llm_break:
            try:
                result = engine.distill(distill_ids)
            except LLMError as error:
                if not break_on_llm_error(error):
                    for note_id in distill_ids:
                        ledger.fail(note_id, "ingested",
                                    getattr(error, "code", "llm_error"))
                        record["processed"]["distill"]["failed"] += 1
                    result = None
                else:
                    result = None
            except Exception as error:
                # LLM Wiki 引擎的"需要用户确认整理改动"：暂停并登记事项。
                # 其他异常原样上抛，走未预期错误收尾。
                from .engines.llmwiki.engine import ReviewRequired
                if isinstance(error, ReviewRequired):
                    needs_user("llmwiki_review_required",
                               "LLM Wiki 有一组整理改动需要你在桌面版或控制台里确认")
                raise
            if result is not None:
                _apply_distill(ledger, record, distill_ids, result)

        # ---------------- 第 6 步：清理原件 ----------------
        if not pause_seen:
            current_step = pause_barrier("cleanup")
            if settings.get("cleanup_enabled") and engine is not None:
                store.write_status(now=deps.now(), step="cleanup",
                                   message="正在清理已入库的原件")
                # 清理出错不能让整次运行失败：记下错误类型，结果按原规则判定。
                try:
                    outcome = deps.cleanup_apply(ledger, engine, store)
                    record["cleanup"] = {
                        "ran": True,
                        "notes": outcome.get("notes", 0),
                        "freed_bytes": outcome.get("freed_bytes", 0),
                        "skipped": len(outcome.get("skipped", [])),
                    }
                except (ImportError, ValueError, OSError) as error:
                    record["cleanup"] = {"ran": False, "notes": 0,
                                         "freed_bytes": 0, "skipped": 0,
                                         "error": type(error).__name__}

        # ---------------- 第 7 步：汇总 ----------------
        current_step = "summarize"
        store.write_status(now=deps.now(), step="summarize", message="正在汇总")
        record["counts"] = ledger.counts()

        # 结果判定：暂停 > 需要用户处理 > 未完成 > 完成。
        if pause_seen or store.pause_requested():
            stop_reason = "process_timeout" if timed_out else "pause_requested"
            raise _StopRun("paused", stop_reason, RESULT_MESSAGES["paused"])
        if encountered:
            raise _StopRun("needs_user", encountered[-1],
                           RESULT_MESSAGES["needs_user"])
        if timed_out or llm_break:
            record["result"] = "incomplete"
            record["stop_reason"] = ("process_timeout" if timed_out
                                     else f"llm_{llm_break}")
            record["message"] = RESULT_MESSAGES["incomplete"]
        elif record["scan"]["incomplete"]:
            # 扫描不完整：照常处理了扫到的部分，稍后自动重扫补齐（不发通知）。
            record["result"] = "incomplete"
            record["stop_reason"] = "scan_incomplete"
            record["message"] = (f"收藏页这次只扫到 {record['scan']['visible']}/"
                                 f"{record['scan']['nominal']} 条，已处理其中"
                                 f"的新收藏，稍后会自动重扫")
        else:
            # 还有笔记没到终态（skipped 不属于待处理队列）→ incomplete，
            # 守护进程会按规则追加运行。
            remaining = [n for n in ledger.all_notes()
                         if n["stage"] in ("discovered", "downloaded", "parsed",
                                           "ingested")]
            if remaining:
                record["result"] = "incomplete"
                record["message"] = RESULT_MESSAGES["incomplete"]
            else:
                record["result"] = "completed"
                record["message"] = RESULT_MESSAGES["completed"]

    except _StopRun as stop:
        record["result"] = stop.result
        record["stop_reason"] = stop.stop_reason
        record["message"] = stop.message
    except Exception as error:
        # 兜住一切意外异常：只记异常类型和所在步骤，不写参数、路径或链接。
        record["result"] = "error"
        record["stop_reason"] = "unexpected_error"
        record["message"] = (f"{type(error).__name__}：在「"
                             f"{STEP_LABELS.get(current_step, current_step)}」"
                             f"步骤发生未预期的错误")
        if deps.log:
            deps.log(f"运行异常：{type(error).__name__} step={current_step}")
    finally:
        record["llm_break"] = llm_break
        # 引擎自己管理的外部资源（如 LLM Wiki 后台进程）在这里收掉；
        # 失败不影响运行结果（后台带着等待确认的任务也能安全跨重启）。
        if engine is not None and hasattr(engine, "close"):
            try:
                engine.close()
            except Exception:
                pass
        try:
            _finish(deps, record, encountered, scan_succeeded)
        except Exception as error:
            record["result"] = "error"
            record["stop_reason"] = "finish_failed"
            record["message"] = f"{type(error).__name__}：收尾时发生未预期的错误"
    return record


def _result_field(result, field, default=None):
    """兼容引擎的数据类与字典返回值，保留空字典和空列表的实际含义。"""
    if hasattr(result, field):
        return getattr(result, field)
    if isinstance(result, dict):
        return result.get(field, default)
    return default


def _apply_ingest(ledger, record, engine_name, batch, results, ingested_ids,
                  *, require_all=True):
    """逐条应用入库结果；熔断只接收实际完成项，不惩罚尚未执行的笔记。"""
    by_id = {_result_field(result, "note_id"): result for result in results}
    for note in batch:
        note_id = note["note_id"]
        result = by_id.get(note_id)
        if result is None:
            if require_all:
                ledger.fail(note_id, "parsed", "ingest_no_result")
                record["processed"]["ingest"]["failed"] += 1
            continue
        status = _result_field(result, "status")
        if status == "ok":
            ref = _result_field(result, "engine_ref")
            if not isinstance(ref, dict):
                ref = {"raw": str(ref)}
            ledger.advance(note_id, "ingested", engine=engine_name,
                           engine_ref=json.dumps(ref, ensure_ascii=False))
            ingested_ids.append(note_id)
            record["processed"]["ingest"]["ok"] += 1
        elif status == "deferred":
            reason = _result_field(result, "insufficient_reason") or "内容不足"
            ledger.advance(note_id, "deferred", deferred_reason=_truncate(reason, 200))
            record["processed"]["ingest"]["deferred"] += 1
        else:
            code = _result_field(result, "error") or "ingest_failed"
            ledger.fail(note_id, "parsed", str(code))
            record["processed"]["ingest"]["failed"] += 1


def _apply_distill(ledger, record, note_ids, result):
    """把 engine.distill 的返回结果按笔记推进到 distilled / 记失败。

    返回值形状（接口约定）：
    - 首选：对象（或 dict）带 per_note —— {note_id: {"status": "ok"/"failed",
      "code": 错误代码}}，逐条推进；
    - 兜底（没有 per_note 的 DistillResult）：status == "ok" 时全部推进；
      否则全部记 failed（错误代码取 errors[0]["code"]，没有就 distill_failed）。
    """
    per_note = _result_field(result, "per_note")
    if per_note is None:
        status = _result_field(result, "status")
        if status == "ok":
            per_note = {nid: {"status": "ok"} for nid in note_ids}
        else:
            errors = _result_field(result, "errors")
            if errors is None:
                errors = []
            code = (errors[0].get("code") if errors and isinstance(errors[0], dict)
                    else None) or "distill_failed"
            per_note = {nid: {"status": "failed", "code": code}
                        for nid in note_ids}
    for note_id in note_ids:
        outcome = per_note.get(note_id) or {"status": "failed",
                                            "code": "distill_no_result"}
        if outcome.get("status") == "ok":
            ledger.advance(note_id, "distilled")
            record["processed"]["distill"]["ok"] += 1
        elif outcome.get("status") == "deferred":
            # LLM Wiki 的整理模型主动暂缓：来源未被消费，进入
            # 台账的 deferred 终态，来源页保留、原件不清理。
            reason = _truncate(str(outcome.get("code") or "内容不足"), 200)
            ledger.advance(note_id, "deferred", deferred_reason=reason)
        else:
            ledger.fail(note_id, "ingested",
                        str(outcome.get("code") or "distill_failed"))
            record["processed"]["distill"]["failed"] += 1


def _finish(deps, record, encountered, scan_succeeded):
    """运行结束的收尾：补齐时间 → 清 needs_user → 写状态/记录 → 通知。"""
    store = deps.store
    ended = deps.now()
    record["ended_at"] = ended.isoformat()
    record["counts"] = deps.ledger.counts()

    # 清理 needs_user：本次没遇到的事项清掉；登录和下载受限需要对应的
    # 恢复证据，不能因为本轮没走到该环节就把旧提示误删。
    status = store.read_status()
    for item in status.get("needs_user", []):
        code = item.get("code")
        if code in encountered:
            continue
        if code == "xhs_login" and not scan_succeeded:
            continue
        # 本轮下载成功才证明已经恢复；若成功后又熔断，上面的 encountered
        # 分支会保留新事项。只有扫描成功、无待下载内容或预检失败都不算恢复。
        if code == "xhs_download_blocked" and not record["processed"]["download"]["ok"]:
            continue
        store.clear_needs_user(code)

    # 最终状态：完成/未完 → idle；暂停 → paused；有事项 → needs_user；出错 → error。
    final_state = {
        "completed": "idle", "incomplete": "idle", "paused": "paused",
        "needs_user": "needs_user", "error": "error",
    }.get(record["result"], "idle")
    store.write_status(now=ended, state=final_state, step=None,
                       message=record["message"]
                       or RESULT_MESSAGES.get(record["result"], ""))

    # 写运行记录。
    store.append_run(record)

    # 发通知：本次遇到的每个事项发一条；意外错误再发一条 error。
    # 正常完成（completed）和暂停都不发通知（正常完成不打扰）。
    for code in encountered:
        try:
            deps.notify(code)
        except Exception:
            pass  # 通知失败不影响运行结果（下次遇到还会再发）
    if record["result"] == "error" and "error" not in encountered:
        try:
            deps.notify("error")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 小工具：私有 JSON 文件的原子写 / 读（下载结果等）
# ---------------------------------------------------------------------------

def _write_private_json(path: Path, data) -> None:
    """把 data 原子写成 JSON 文件，权限 0600。先写临时文件再 os.replace。"""
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".",
                                    suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)


def _read_json(path: Path, default):
    """读一个 JSON 文件；不存在或损坏时返回 default。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
