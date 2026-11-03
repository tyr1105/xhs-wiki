# -*- coding: utf-8 -*-
"""skill.py —— 渲染、安装和卸载查询 Skill（Claude Code 和其他 AI 工具通用）。

这个文件做什么：把 ``skill/SKILL.md.tmpl`` 模板渲染成实际的 SKILL.md
（填入用户自己的知识库路径、引擎类型，以及这个引擎的索引、主题页、
来源页各在哪里），写到 ``<skills_root>/xhs-wiki/SKILL.md``；也能把它
删掉（卸载）。

SKILL.md 是一种通用格式（开放的 Agent Skills 约定）：一个文件夹里放一份
带 name、description 元数据的 Markdown 说明，AI 工具看 description 判断
什么时候用它。Claude Code、腾讯 WorkBuddy、Codex、Cursor、Gemini CLI、
GitHub Copilot 都认这种格式，区别只在各自去哪个目录找。所以"装进别的
工具"就是把同一份 SKILL.md 写到对应的目录里——见下面的 TARGET_ROOTS。

在整体里的位置：docs/architecture.md §12。被 daemon.py 调用（首次设置
完成、修改 kb_root、engine 或要装进的工具后自动更新 Skill），也被
install.sh 间接调用（安装时写"尚未完成设置"版本、升级时刷新已装的版本）。

红线自查：模板和渲染结果里只有知识库路径、引擎名和引擎自己的目录名
——没有任何领域词、主题分类、笔记编号或签名链接。description 必须保持
通用，不列具体领域（否则会变成暗示用户的收藏范围）。

测试支持：``skills_root`` / ``roots`` 参数注入临时目录，检测工具用的
主目录也能注入（``home``），安装、卸载和检测绝不碰真实的 ``~/.claude``、
``~/.workbuddy``、``~/.agents``。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .store import DEFAULT_SETTINGS, SKILL_TARGETS

# 模板随包分发（src/xhs_wiki/skill/SKILL.md.tmpl），只读。
TEMPLATE_PATH = Path(__file__).resolve().parent / "skill" / "SKILL.md.tmpl"

# Skill 的名字（也是 skills_root 下的目录名）。Cursor 和 GitHub Copilot
# 要求名字只用小写字母、数字和连字符，而且和文件夹同名，xhs-wiki 正好满足。
SKILL_NAME = "xhs-wiki"

# ---------------------------------------------------------------------------
# 安装位置：每个位置是一个技能目录（store.SKILL_TARGETS 里的名字 → 目录）
# ---------------------------------------------------------------------------
#
# 各工具去哪里找用户级的 Skill（2026-11 按各家官方文档核实）：
# - claude_code：Claude Code 读 ~/.claude/skills；Cursor 为了兼容也读这里。
# - workbuddy：腾讯 WorkBuddy。官方文档只写"上传或新建的技能默认存在用户
#   配置目录、所有项目都能用"，具体路径 ~/.workbuddy/skills 来自多份使用
#   资料和公开的技能合集安装说明，说法一致。WorkBuddy 只读用户授权过的
#   工作空间，提问前要把知识库文件夹选为工作空间（控制台页面上有提示）。
# - agents：开放约定里的共用目录 ~/.agents/skills。OpenAI Codex（命令行、
#   IDE 插件、ChatGPT 桌面版）、Cursor、Gemini CLI、GitHub Copilot 命令行
#   的官方文档都把它列为用户级技能目录，写一份几个工具都能用。
#
# 每个位置都能用环境变量改写：做隔离测试时，服务用真实主目录运行（Claude
# 登录态要用），但 Skill 必须写进测试目录，不能写进用户真实的技能目录。
# 环境变量在模块导入时读取（和 config.py 读 XHS_WIKI_HOME 的做法一致）。
_TARGET_ROOT_SOURCES = {
    "claude_code": ("XHS_WIKI_SKILLS_ROOT", "~/.claude/skills"),
    "workbuddy": ("XHS_WIKI_WORKBUDDY_SKILLS_ROOT", "~/.workbuddy/skills"),
    "agents": ("XHS_WIKI_AGENTS_SKILLS_ROOT", "~/.agents/skills"),
}
# 按 store.SKILL_TARGETS 的顺序排（页面按这个顺序显示）；那边多了一个
# 位置、这里忘了写目录时，导入本模块就会报 KeyError，不会悄悄漏装。
TARGET_ROOTS = {
    target: Path(os.environ.get(_TARGET_ROOT_SOURCES[target][0])
                 or _TARGET_ROOT_SOURCES[target][1]).expanduser()
    for target in SKILL_TARGETS
}

# 旧名字：以前只有 Claude Code 一个位置，叫 DEFAULT_SKILLS_ROOT。
DEFAULT_SKILLS_ROOT = TARGET_ROOTS["claude_code"]

# 检测"本机装了哪些工具"时用的线索：主目录下工具自己建的文件或文件夹，
# 或"应用程序"文件夹里的 App。只看这些路径存不存在，绝不读里面的内容。
# 每一项：(给用户看的工具名, 所属安装位置, 主目录下的线索, App 名)。
#
# 线索故意避开本工具自己会建的目录：写 Skill 时会建出 ~/.claude、
# ~/.workbuddy、~/.agents，拿它们当线索就成了"自己装完自己检测到"——
# 没装 Claude Code 的人完成设置后也会看到"本机有 Claude Code"。所以
# Claude Code 看 ~/.claude.json（它第一次运行就会建的配置文件）或桌面版
# Claude.app，WorkBuddy 看 App 或它的默认工作文件夹 ~/WorkBuddy（官方 FAQ
# 写的 Mac 主文件夹位置）。
_TOOL_MARKERS = (
    ("Claude Code", "claude_code", (".claude.json",), ("Claude.app",)),
    ("WorkBuddy", "workbuddy", ("WorkBuddy",), ("WorkBuddy.app",)),
    ("Codex", "agents", (".codex",), ("Codex.app",)),
    ("Cursor", "agents", (".cursor",), ("Cursor.app",)),
    ("Gemini CLI", "agents", (".gemini",), ()),
    ("GitHub Copilot", "agents", (".copilot",), ()),
)

# 检测用的主目录。环境变量 XHS_WIKI_DETECT_HOME 只给测试用：设了它，
# 主目录和"应用程序"文件夹都换成它下面的路径，测试结果不随开发者本机
# 装了什么而变，也不去看真实主目录。
_DETECT_HOME_OVERRIDE = os.environ.get("XHS_WIKI_DETECT_HOME")

# 引擎名 → 给用户看的说明（渲染进 SKILL.md 正文）。
_ENGINE_LABELS = {
    "markdown": "轻量引擎（Markdown 文件夹，可用任意编辑器打开）",
    "llmwiki": "LLM Wiki 引擎（在 LLM Wiki 桌面版里查看）",
}

# 未完成首次设置时插进正文的提示段（不写死 127.0.0.1 地址——
# 控制台现在要带访问令牌才能打开，正确入口是 `xhs-wiki open`）。
_SETUP_PENDING_NOTICE = (
    "> **尚未完成首次设置**：知识库还是空的。请在终端运行 "
    "`xhs-wiki open` 打开控制台完成设置，再使用这个技能。"
)


def _engine_layout(settings: dict) -> dict:
    """问当前引擎：索引、主题页、来源页各在知识库的哪个位置。

    为什么要问引擎：两种引擎的知识库目录结构不一样——轻量引擎是
    索引.md、主题/、来源/；LLM Wiki 引擎是 wiki/index.md、wiki/、
    wiki/sources/。以前模板里写死了轻量引擎的三个位置，选 LLM Wiki 的人
    在 Claude Code 里提问时，Skill 指向根本不存在的目录，查询步骤第 6 步
    又要求"找不到就说没有收藏过"，可能把收藏过的内容答成没有。

    怎么问：不在这里另记一份目录名（另记一份就会和引擎对不上，上面的
    问题正是这样来的），而是用守护进程和每次运行共用的引擎工厂
    （engines.make_engine）建一个引擎实例，只调它的 skill_context()。
    建实例不启动后台、不调用模型、不读密钥：提供方传 None，
    skill_context 用不到它；LLM Wiki 引擎建实例时只会确保 APP_HOME 下
    它自己的导入目录存在（没有就建），不碰知识库。

    返回 skill_context() 的结果，这里用到其中三个键：index、topics_dir、
    sources_dir，都是相对知识库根目录的写法（例如 "wiki/index.md"）。
    """
    from . import config
    from .engines import make_engine

    return make_engine(settings, None, config.app_home()).skill_context()


def render(settings: dict) -> str:
    """把模板渲染成 SKILL.md 全文。

    ``settings`` 是 store.load_settings() 的形状，用到三个键：
    - ``kb_root``：知识库根目录（渲染时 expanduser 成绝对路径）；
    - ``engine``：引擎名（markdown / llmwiki），转成中文说明，并决定
      查询步骤里索引、主题页、来源页的位置（见 _engine_layout）；
    - ``setup_completed``：False（或缺失）时插入"先完成设置"提示。
    """
    kb_root = str(Path(settings["kb_root"]).expanduser())
    # 纵深防御：kb_root 里的换行/控制字符/反引号会原样进 SKILL.md，
    # 等于向 Claude Code 注入任意 Markdown 标题和"指令"。写入侧
    # （store.validate_kb_root）已经拦截，这里再挡一道，保证无论如何
    # 不把这类内容写进全局生效的 Skill。
    # 和写入侧共用同一条规则（store.kb_root_text_ok）——
    # 以前这里只看 ASCII 控制字符，\x85、U+2028/U+2029（Python 的
    # splitlines() 会当成换行）和 U+202E（从右往左显示）都能混进来。
    from .store import kb_root_text_ok
    if not kb_root_text_ok(kb_root):
        raise ValueError("kb_root_invalid")
    engine = settings.get("engine") or "markdown"
    engine_label = _ENGINE_LABELS.get(engine, engine)
    # 设置里没写引擎时按轻量引擎处理（和上面的说明文字保持一致）。
    layout = _engine_layout({**settings, "engine": engine})
    notice = "" if settings.get("setup_completed") else _SETUP_PENDING_NOTICE
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    # 用户自己选的知识库路径放到最后再填：前面几步填的都是程序里的固定
    # 文字，路径里万一出现"{{INDEX}}"这类字样，也不会被当成占位符改掉。
    return (template
            .replace("{{INDEX}}", layout["index"])
            .replace("{{TOPICS_DIR}}", layout["topics_dir"])
            .replace("{{SOURCES_DIR}}", layout["sources_dir"])
            .replace("{{ENGINE_LABEL}}", engine_label)
            .replace("{{SETUP_NOTICE}}", notice)
            .replace("{{KB_ROOT}}", kb_root))


def _write(target_dir: Path, content: str) -> Path:
    """把渲染好的全文写成 ``<target_dir>/SKILL.md``，返回它的路径。

    写法遵循项目规范：先写同目录临时文件，再 ``os.replace`` 原子替换；
    目录权限 0700、文件 0600（Skill 里有用户的知识库路径，算私有数据）。
    """
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target_dir.chmod(0o700)
    target = target_dir / "SKILL.md"
    descriptor, name = tempfile.mkstemp(prefix=".skill-", dir=target_dir)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        temporary.chmod(0o600)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def install(settings: dict, *, skills_root: Path = None) -> Path:
    """渲染并写入 Skill，返回 SKILL.md 的路径。

    ``skills_root`` 是某个工具的技能目录；不传时写进 Claude Code 的目录
    （DEFAULT_SKILLS_ROOT），和以前只支持 Claude Code 时一样。
    """
    root = Path(skills_root) if skills_root is not None else DEFAULT_SKILLS_ROOT
    return _write(root / SKILL_NAME, render(settings))


def remove(skills_root: Path) -> str:
    """从一个技能目录里撤掉本工具写的 Skill，返回结果代码。

    只删本工具写进去的东西：``xhs-wiki/SKILL.md``，以及写入中断时可能
    留下的 ``.skill-*`` 临时文件；目录删空了才删目录本身，用户自己放进
    去的文件原样保留（和 uninstall.sh 的做法一致）。

    ``xhs-wiki`` 本身是符号链接时一个文件都不删：那多半是用户自己把它链
    到了别的工具的目录，顺着链接删会把那边正在用的 Skill 一起删掉。

    结果代码：removed（删了东西）、absent（本来就没有）、
    kept_symlink（是符号链接，没动）。
    """
    target = Path(skills_root) / SKILL_NAME
    if target.is_symlink():
        return "kept_symlink"
    if not target.is_dir():
        return "absent"
    removed = False
    candidates = [target / "SKILL.md", *target.glob(".skill-*")]
    for path in candidates:
        # 只删文件（或指向别处的链接本身），绝不进子目录。
        if path.is_symlink() or path.is_file():
            path.unlink()
            removed = True
    try:
        target.rmdir()   # 还有用户自己的文件时删不掉，正好保留
    except OSError:
        pass
    return "removed" if removed else "absent"


def uninstall(skills_root: Path = None) -> bool:
    """从一个技能目录里撤掉 Skill，返回是否删了东西（本来就没有则 False）。

    不传 ``skills_root`` 时撤 Claude Code 目录里的那份。只删本工具写的
    文件（见 remove），绝不碰 skills_root 下的其他技能。
    """
    root = Path(skills_root) if skills_root is not None else DEFAULT_SKILLS_ROOT
    return remove(root) == "removed"


def _roots(overrides: dict = None) -> dict:
    """每个安装位置实际用的目录：默认 TARGET_ROOTS，测试可逐个改写。"""
    roots = dict(TARGET_ROOTS)
    for target, root in (overrides or {}).items():
        if root is not None:
            roots[target] = Path(root)
    return roots


def selected_targets(settings: dict) -> set:
    """设置里勾选的位置。

    store.load_settings() 读出来的设置一定带 skill_targets（旧设置文件会用
    默认值补齐）；只有手工拼的半份设置才会缺这个键，这时按默认值处理
    （只装 Claude Code），而不是当成"一个都不装"把已装的 Skill 撤掉。
    手工改坏的值（不是列表）同样按默认值处理；不认识的名字直接忽略。
    """
    value = settings.get("skill_targets", DEFAULT_SETTINGS["skill_targets"])
    if not isinstance(value, (list, tuple)):
        value = DEFAULT_SETTINGS["skill_targets"]
    return {target for target in value if target in SKILL_TARGETS}


def sync(settings: dict, *, roots: dict = None, withdraw=()) -> dict:
    """按设置把 Skill 写进勾选的位置；``withdraw`` 里的位置撤掉。

    ``settings["skill_targets"]`` 是勾选的位置（store.SKILL_TARGETS 里的
    名字）。``withdraw`` 是用户这次**取消勾选**的位置：只有这些位置会删
    文件。改知识库位置、换引擎、完成设置、升级时只传设置、不传 withdraw，
    只更新勾选的位置，没勾选的位置一概不碰——那里要是有用户自己放的同名
    技能（比如以前手动复制过去的），不会因为一次例行更新就被删掉。

    返回 {位置: 结果代码}，结果代码：
    - installed：写好了（新装或者更新）；
    - removed / absent / kept_symlink：撤掉了 / 本来就没有 / 是符号链接
      没动（见 remove）；
    - unchanged：没勾选，这次也没要求撤，没动；
    - failed：写入或删除失败（目录不可写、磁盘满、知识库路径不合法）。
    各位置互不影响：一个失败，其余照常处理。

    几个位置其实是同一个目录时（比如用户把 ~/.agents/skills 链接到了
    ~/.claude/skills）按真实目录合并处理：只要其中一个勾选了就写，全都
    没勾选才撤——不然会出现"刚写进去，又被另一个位置删掉"。
    """
    selected = selected_targets(settings)
    withdrawn = {target for target in withdraw if target in SKILL_TARGETS}
    # 真实目录 → 共用这个目录的位置（按 SKILL_TARGETS 的顺序）。
    groups = {}
    for target, root in _roots(roots).items():
        real = os.path.realpath(root / SKILL_NAME)
        groups.setdefault(real, []).append((target, root))

    content = None
    results = {}
    for members in groups.values():
        root = members[0][1]
        names = {target for target, _ in members}
        if names & selected:
            try:
                # 只渲染一次：同一份设置写到几个位置，内容完全一样。
                if content is None:
                    content = render(settings)
                _write(root / SKILL_NAME, content)
                outcome = "installed"
            except (OSError, ValueError, KeyError):
                outcome = "failed"
        elif names & withdrawn:
            try:
                outcome = remove(root)
            except OSError:
                outcome = "failed"
        else:
            outcome = "unchanged"
        for target in names:
            results[target] = outcome
    return results


def _detect_dirs(home: Path = None) -> tuple:
    """检测用的主目录和"应用程序"文件夹列表。"""
    if home is None and _DETECT_HOME_OVERRIDE:
        home = Path(_DETECT_HOME_OVERRIDE)
    if home is not None:
        return Path(home), (Path(home) / "Applications",)
    real_home = Path.home()
    return real_home, (Path("/Applications"), real_home / "Applications")


def detect_tools(*, home: Path = None) -> list:
    """返回本机检测到的 AI 工具名（_TOOL_MARKERS 里的名字，按表中顺序）。

    判断方法：主目录下有这个工具自己建的文件或文件夹，或者"应用程序"
    文件夹里有它的 App（线索见 _TOOL_MARKERS）。只看路径存不存在，不读
    任何内容；结果只用来在页面上提示"本机有 xx"，不影响装不装（装哪些
    由用户勾选）。
    """
    home_dir, app_dirs = _detect_dirs(home)
    found = []
    for name, _target, home_markers, apps in _TOOL_MARKERS:
        paths = [home_dir / marker for marker in home_markers]
        paths.extend(app_dir / app for app_dir in app_dirs for app in apps)
        if any(path.exists() for path in paths):
            found.append(name)
    return found


def _display_path(path: Path, home: Path) -> str:
    """给页面看的路径：主目录开头的部分写成 ~，短一些。"""
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def targets_state(settings: dict, *, roots: dict = None, home: Path = None) -> list:
    """控制台要显示的各安装位置状态（/api/status 的 skill_targets）。

    每一项：id（位置名）、selected（是否勾选）、detected_tools（本机检测
    到的、会读这个位置的工具）、path（Skill 文件夹，主目录写成 ~）、
    installed（现在这里有没有本工具的 SKILL.md）。
    """
    selected = selected_targets(settings)
    home_dir, _apps = _detect_dirs(home)
    detected = set(detect_tools(home=home))
    state = []
    for target, root in _roots(roots).items():
        folder = root / SKILL_NAME
        state.append({
            "id": target,
            "selected": target in selected,
            "detected_tools": [name for name, owner, _f, _a in _TOOL_MARKERS
                               if owner == target and name in detected],
            "path": _display_path(folder, home_dir),
            "installed": (folder / "SKILL.md").is_file(),
        })
    return state
