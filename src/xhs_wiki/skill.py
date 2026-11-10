# -*- coding: utf-8 -*-
"""skill.py —— 渲染、安装和卸载查询 Skill（Claude Code 和其他 AI 工具通用）。

这个文件做什么：把 ``skill/SKILL.md.tmpl`` 模板渲染成实际的 SKILL.md
（填入用户自己的知识库路径、引擎类型，以及这个引擎的索引、主题页、
来源页各在哪里），写到 ``<skills_root>/xhs-wiki/SKILL.md``；也能把它
删掉（卸载）。

SKILL.md 是一种通用格式（开放的 Agent Skills 约定）：一个文件夹里放一份
带 name、description 元数据的 Markdown 说明，AI 工具看 description 判断
什么时候用它。Claude Code、腾讯 WorkBuddy、Codex、Cursor、Trae 等都认这种
格式，区别只在各自去哪个目录找。所以"装进别的工具"就是把同一份 SKILL.md
写到对应的目录里——见下面的 TARGET_DIRS。

三条安全规矩（写入、撤回、卸载都遵守）：
1. 只碰本工具写的文件：每份 SKILL.md 都带一行归属标记（OWNER_MARKER）；
   旧版（v0.1~v0.2）没有标记的，按模板里固定不变的几行认领。别人放的
   同名 xhs-wiki/SKILL.md 一律不覆盖、不删除，结果代码是 kept_foreign。
2. 不顺着符号链接写到别处、也不顺着删：xhs-wiki 文件夹本身是链接时，
   只有它正好指向另一个勾选了的、本身不是链接的位置才算数；其他情况
   一个文件都不动，结果代码是 kept_symlink。
3. 没勾选的位置，例行更新一概不碰；只有用户取消勾选时才撤。

在整体里的位置：docs/architecture.md §12。被 daemon.py 调用（首次设置
完成、修改 kb_root、engine 或要装进的工具后自动更新 Skill），也被
install.sh 间接调用（安装时写"尚未完成设置"版本、升级时刷新已装的版本，
见 installer_sync）。

红线自查：模板和渲染结果里只有知识库路径、引擎名和引擎自己的目录名
——没有任何领域词、主题分类、笔记编号或签名链接。description 必须保持
通用，不列具体领域（否则会变成暗示用户的收藏范围）。

测试支持：``skills_root`` / ``roots`` 参数注入临时目录，检测工具用的
主目录（``home``）和系统"应用程序"文件夹（SYSTEM_APPLICATIONS）也能换，
安装、卸载和检测绝不碰真实的 ``~/.claude``、``~/.workbuddy``、``~/.agents``。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from .store import DEFAULT_SETTINGS, SKILL_TARGETS

# 模板随包分发（src/xhs_wiki/skill/SKILL.md.tmpl），只读。
TEMPLATE_PATH = Path(__file__).resolve().parent / "skill" / "SKILL.md.tmpl"

# Skill 的名字（也是 skills_root 下的目录名）。Cursor、GitHub Copilot、Kiro
# 等要求名字只用小写字母、数字和连字符，而且和文件夹同名，xhs-wiki 正好满足。
SKILL_NAME = "xhs-wiki"

# 归属标记：模板里 frontmatter 后面紧跟的那一行（文件仍以 --- 开头，各工具
# 照常加载）。有这一行，就是本工具写的。uninstall.sh 用 grep 认同一行。
OWNER_MARKER = "<!-- managed-by: xhs-wiki -->"

# 旧版（v0.1~v0.2，模板里还没有归属标记）写的文件，按模板里一直没变的
# 几行认领：第 1 行是 ---、第 2 行是 name: xhs-wiki，正文里有这两个标题。
# 这样老用户升级后照样能更新、撤回、卸载以前装的那份。
_LEGACY_FIRST_LINES = ("---", "name: xhs-wiki")
_LEGACY_HEADINGS = ("# 小红书收藏知识库", "## 内容安全（先读这一段）")

# 认归属时最多读这么多字节：本工具的 SKILL.md 只有两三千字节，别人放的
# 大文件没必要整份读进来。
_OWNERSHIP_READ_LIMIT = 256 * 1024

# ---------------------------------------------------------------------------
# 安装位置：每个位置是一个技能目录（store.SKILL_TARGETS 里的名字 → 目录）
# ---------------------------------------------------------------------------
#
# 各工具去哪里找用户级的 Skill（2026-11 按各家官方文档核实）：
# - claude_code：Claude Code 读 ~/.claude/skills；Cursor、OpenCode、Goose、
#   Amp、VS Code 里的 GitHub Copilot（Local 会话）为了兼容也读这里。
# - workbuddy：腾讯 WorkBuddy。产品文档只写"上传或新建的技能默认存在用户
#   配置目录"；具体路径 ~/.workbuddy/skills 来自腾讯 SkillHub 的安装说明和
#   腾讯官方 GitHub 仓库（Tencent/skillhub、Tencent/BrowserSkill）。WorkBuddy
#   只读用户授权过的工作空间，提问前要把知识库文件夹选为工作空间。
# - agents：开放约定里的共用目录 ~/.agents/skills。OpenAI Codex（命令行、
#   IDE 插件、ChatGPT 桌面版）、Cursor、Gemini CLI、GitHub Copilot、OpenClaw
#   （默认状态目录时）、Kimi Code、Qwen Code、新版 Qoder、Zed、Devin Desktop
#   （原 Windsurf）、OpenCode、Goose、Amp、Factory Droid 的官方文档都列了它，
#   写一份这些工具都能用。注意：免费和 Google One 用户的 Gemini CLI 已在
#   2026-06-18 换成 Antigravity CLI，它不读这个目录（要勾 antigravity）。
# - 下面几个各有自己的目录，不读上面两个共用目录：腾讯 CodeBuddy
#   ~/.codebuddy/skills；字节 Trae 国际版 ~/.trae/skills、国内版
#   ~/.trae-cn/skills；阿里 Qoder ~/.qoder/skills；Qoder CN（原通义灵码）的
#   IDE 和插件 ~/.lingma/skills、命令行 ~/.qoder-cn/skills；AWS Kiro
#   ~/.kiro/skills；Google Antigravity ~/.gemini/config/skills；Cline
#   ~/.cline/skills。
# 这些工具都要求技能名只用小写字母、数字和连字符、和文件夹同名，
# xhs-wiki 都满足；OpenClaw 建议描述在 160 个字以内，现在是 113 个。
#
# 每个位置的技能目录，写成相对主目录的路径。uninstall.sh 里有一份同样的
# 清单（EXTRA_SKILL_DIRS），tests/test_skill_targets.py 会核对两边一致。
TARGET_DIRS = {
    "claude_code": ".claude/skills",
    "workbuddy": ".workbuddy/skills",
    "agents": ".agents/skills",
    "codebuddy": ".codebuddy/skills",
    "trae": ".trae/skills",
    "trae_cn": ".trae-cn/skills",
    "qoder": ".qoder/skills",
    "lingma": ".lingma/skills",
    "qoder_cn_cli": ".qoder-cn/skills",
    "kiro": ".kiro/skills",
    "antigravity": ".gemini/config/skills",
    "cline": ".cline/skills",
}

# 各 AI 工具所在的主目录。环境变量 XHS_WIKI_TOOLS_HOME 只给测试和隔离
# 安装用（正式安装不设）：设了它，所有位置的技能目录（包括 Claude Code，
# 除非另外用 XHS_WIKI_SKILLS_ROOT 单独指定）和检测本机工具的线索都换到它
# 下面，绝不碰用户真实的 ~/.claude、~/.workbuddy、~/.agents。
# XHS_WIKI_SKILLS_ROOT 单独改写 Claude Code 的目录（老的环境变量，保留）。
# 两者和 install.sh、uninstall.sh 的默认值完全一致。环境变量在模块导入时
# 读取（和 config.py 读 XHS_WIKI_HOME 的做法一致）。
_TOOLS_HOME_OVERRIDE = os.environ.get("XHS_WIKI_TOOLS_HOME")
_TOOLS_HOME = Path(_TOOLS_HOME_OVERRIDE or "~").expanduser()

# 按 store.SKILL_TARGETS 的顺序排（页面按这个顺序显示）；那边多了一个
# 位置、这里忘了写目录时，导入本模块就会报 KeyError，不会悄悄漏装。
TARGET_ROOTS = {target: _TOOLS_HOME / TARGET_DIRS[target] for target in SKILL_TARGETS}
if os.environ.get("XHS_WIKI_SKILLS_ROOT"):
    TARGET_ROOTS["claude_code"] = Path(os.environ["XHS_WIKI_SKILLS_ROOT"]).expanduser()

# 旧名字：以前只有 Claude Code 一个位置，叫 DEFAULT_SKILLS_ROOT。
DEFAULT_SKILLS_ROOT = TARGET_ROOTS["claude_code"]

# 系统的"应用程序"文件夹。检测本机装了哪些工具时要看它；测试换成临时目录，
# 结果不随开发者本机装了什么而变。
SYSTEM_APPLICATIONS = Path("/Applications")

# 检测"本机装了哪些工具"时用的线索：主目录下工具自己建的文件或文件夹，
# 或"应用程序"文件夹里的 App。只看这些路径存不存在，绝不读里面的内容。
# 每一项：(给用户看的工具名, 所属安装位置, 主目录下的线索, App 名)。
#
# 线索故意避开本工具自己会建的目录：写 Skill 时会建出 ~/.claude、
# ~/.workbuddy、~/.agents、~/.trae、~/.gemini/config 这些（见 TARGET_DIRS），
# 拿它们当线索就成了"自己装完自己检测到"——没装 Claude Code 的人完成设置
# 后也会看到"本机有 Claude Code"。所以 Claude Code 看 ~/.claude.json（它
# 第一次运行就会建的配置文件）或桌面版 Claude.app，WorkBuddy 看 App 或它的
# 默认工作文件夹 ~/WorkBuddy（官方 FAQ 写的 Mac 主文件夹位置），Gemini CLI
# 看 ~/.gemini 里它自己的配置文件；只有 App 的工具就只看 App。线索不全
# （比如 Cline 是编辑器插件，没有可靠的线索）时页面只是不显示"本机有"，
# 不影响勾选。
_TOOL_MARKERS = (
    ("Claude Code", "claude_code", (".claude.json",), ("Claude.app",)),
    ("WorkBuddy", "workbuddy", ("WorkBuddy",), ("WorkBuddy.app",)),
    ("Codex", "agents", (".codex",), ("Codex.app",)),
    ("Cursor", "agents", (".cursor",), ("Cursor.app",)),
    ("Gemini CLI", "agents", (".gemini/settings.json", ".gemini/oauth_creds.json"), ()),
    ("GitHub Copilot", "agents", (".copilot",), ()),
    ("OpenClaw", "agents", (".openclaw",), ()),
    ("Kimi Code", "agents", (".kimi-code",), ()),
    ("Qwen Code", "agents", (".qwen",), ()),
    ("Zed", "agents", (), ("Zed.app",)),
    ("Devin Desktop", "agents", (), ("Devin.app", "Windsurf.app")),
    ("OpenCode", "agents", (".config/opencode",), ()),
    ("Goose", "agents", (".config/goose",), ("Goose.app",)),
    ("CodeBuddy", "codebuddy", (), ("CodeBuddy.app", "CodeBuddy CN.app")),
    ("Trae", "trae", (), ("Trae.app",)),
    ("Trae CN", "trae_cn", (), ("Trae CN.app",)),
    ("Qoder", "qoder", (), ("Qoder.app",)),
    ("Qoder CN", "lingma", (), ("Qoder CN.app", "Lingma.app")),
    ("Kiro", "kiro", (), ("Kiro.app",)),
    ("Antigravity", "antigravity", (".gemini/antigravity", ".gemini/antigravity-cli"),
     ("Antigravity.app",)),
)

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


def ownership(skill_file: Path) -> str:
    """这份 SKILL.md 是谁的：返回 "ours"、"foreign" 或 "absent"。

    - absent：不存在（连失效的符号链接也没有）；
    - ours：普通文件，带归属标记；或者是旧版模板写的（见 _LEGACY_*）；
    - foreign：其余一切——别人写的同名技能、SKILL.md 本身是符号链接
      （本工具从不建链接）、不是普通文件，或者读不了（没权限时宁可当成
      别人的，也不冒险覆盖或删除）。

    只用 os.path 系列判断：它们遇到没权限之类的错误返回 False，不抛异常。
    """
    path = os.fspath(skill_file)
    if not os.path.lexists(path):
        return "absent"
    if os.path.islink(path) or not os.path.isfile(path):
        return "foreign"
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_OWNERSHIP_READ_LIMIT)
    except OSError:
        return "foreign"
    lines = raw.decode("utf-8", "replace").splitlines()
    if OWNER_MARKER in lines:
        return "ours"
    if (tuple(lines[:2]) == _LEGACY_FIRST_LINES
            and all(heading in lines for heading in _LEGACY_HEADINGS)):
        return "ours"
    return "foreign"


def _write(target_dir: Path, content: str) -> str:
    """把渲染好的全文写成 ``<target_dir>/SKILL.md``，返回结果代码。

    - installed：写好了；
    - kept_symlink：xhs-wiki 文件夹本身是符号链接，没写（不顺着链接写到
      别处，也不改链接那头的权限）；
    - kept_foreign：那里已经有一份不是本工具写的同名 SKILL.md（或者
      xhs-wiki 根本不是文件夹），没写、没改权限。
    写的过程中出错（目录不可写、磁盘满）照常抛 OSError，由调用方记成 failed。

    写法遵循项目规范：先写同目录临时文件，再 ``os.replace`` 原子替换；
    文件 0600（Skill 里有用户的知识库路径，算私有数据）。文件夹是本工具
    这次新建的才设成 0700；原来就有的文件夹不改权限。
    """
    folder = os.fspath(target_dir)
    if os.path.islink(folder):
        return "kept_symlink"
    existed = os.path.lexists(folder)
    if existed and not os.path.isdir(folder):
        return "kept_foreign"
    if existed and ownership(target_dir / "SKILL.md") == "foreign":
        return "kept_foreign"
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not existed:
        target_dir.chmod(0o700)
    descriptor, name = tempfile.mkstemp(prefix=".skill-", dir=target_dir)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        temporary.chmod(0o600)
        os.replace(temporary, target_dir / "SKILL.md")
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return "installed"


def install(settings: dict, *, skills_root: Path = None) -> Path:
    """渲染并写入 Skill，返回 SKILL.md 的路径。

    ``skills_root`` 是某个工具的技能目录；不传时写进 Claude Code 的目录
    （DEFAULT_SKILLS_ROOT），和以前只支持 Claude Code 时一样。那里是符号
    链接或已有别人的同名技能时不写，抛 ValueError("skill_kept_symlink")
    或 ValueError("skill_kept_foreign")。
    """
    root = Path(skills_root) if skills_root is not None else DEFAULT_SKILLS_ROOT
    outcome = _write(root / SKILL_NAME, render(settings))
    if outcome != "installed":
        raise ValueError(f"skill_{outcome}")
    return root / SKILL_NAME / "SKILL.md"


def remove(skills_root: Path) -> str:
    """从一个技能目录里撤掉本工具写的 Skill，返回结果代码。

    只删本工具写进去的东西：带归属标记（或旧版模板写的）的
    ``xhs-wiki/SKILL.md``，以及写入中断时可能留下的 ``.skill-*`` 临时文件；
    目录删空了才删目录本身，用户自己放进去的文件原样保留（和 uninstall.sh
    的做法一致）。

    结果代码：
    - removed：删了东西；absent：本来就没有；
    - kept_symlink：``xhs-wiki`` 本身是符号链接，一个文件都没删——那多半是
      用户自己把它链到了别处，顺着链接删会把那边正在用的 Skill 一起删掉；
    - kept_foreign：SKILL.md 不是本工具写的，整个文件夹都没动。
    """
    target = Path(skills_root) / SKILL_NAME
    if os.path.islink(target):
        return "kept_symlink"
    if not os.path.isdir(target):
        return "absent"
    owner = ownership(target / "SKILL.md")
    if owner == "foreign":
        return "kept_foreign"
    removed = False
    candidates = [target / "SKILL.md"] if owner == "ours" else []
    candidates.extend(target.glob(".skill-*"))
    for path in candidates:
        # 只删文件（或指向别处的链接本身），绝不进子目录。
        if os.path.islink(path) or os.path.isfile(path):
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


def marked_targets(*, roots: dict = None) -> set:
    """哪些位置里现在有一份带归属标记的 SKILL.md（本工具 v0.3 起写的）。

    控制台保存勾选时用：没勾选、但还留着本工具写的文件的位置（比如上次
    取消勾选时没删成），这次一并撤掉。只认标记、不认旧版模板：旧版
    没有标记的文件也可能是用户自己手动复制过去的副本，不主动去删。
    """
    found = set()
    for target, root in _roots(roots).items():
        skill_file = root / SKILL_NAME / "SKILL.md"
        if os.path.islink(root / SKILL_NAME) or ownership(skill_file) != "ours":
            continue
        try:
            with open(skill_file, "rb") as handle:
                lines = handle.read(_OWNERSHIP_READ_LIMIT).decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        if OWNER_MARKER in lines:
            found.add(target)
    return found


def sync(settings: dict, *, roots: dict = None, withdraw=()) -> dict:
    """按设置把 Skill 写进勾选的位置；``withdraw`` 里的位置撤掉。

    ``settings["skill_targets"]`` 是勾选的位置（store.SKILL_TARGETS 里的
    名字）。``withdraw`` 是要撤的位置（控制台里取消勾选的）：只有这些位置会删
    文件。改知识库位置、换引擎、完成设置、升级时只传设置、不传 withdraw，
    只更新勾选的位置，没勾选的位置一概不碰——那里要是有用户自己放的同名
    技能（比如以前手动复制过去的），不会因为一次例行更新就被删掉。

    返回 {位置: 结果代码}，结果代码：
    - installed：写好了（新装或者更新）；
    - removed / absent：撤掉了 / 本来就没有；
    - kept_foreign：那里有一份不是本工具写的同名技能，没覆盖、没删除；
    - kept_symlink：xhs-wiki 是指向别处的符号链接，没写、没删；
    - unchanged：没勾选，这次也没要求撤，没动；
    - failed：写入或删除失败（目录不可写、磁盘满、知识库路径不合法）。
    各位置互不影响：一个失败，其余照常处理。

    几个位置其实是同一个真实文件夹时（比如用户把 ~/.agents/skills 整个链接
    到了 ~/.claude/skills），按真实文件夹合并处理：
    - 写入：只通过"勾选了、而且 xhs-wiki 本身不是链接"的那个位置写一次，
      共用这个文件夹的位置结果都一样。xhs-wiki 本身是链接的位置，只有正好
      指向这样一个位置时才算装上；否则返回 kept_symlink，一个字节都不写。
    - 撤回：组里有任何一个还勾着就不撤；全都没勾，才对 withdraw 里的每个
      位置各自调用 remove（不借用组里别的位置的目录，链接照样不跟）。
    """
    selected = selected_targets(settings)
    withdrawn = {target for target in withdraw if target in SKILL_TARGETS}
    # 真实文件夹 → 共用它的位置：[(位置名, 技能目录, xhs-wiki 是不是链接)]。
    # os.path.realpath / islink 遇到没权限的目录也不会抛异常。
    groups = {}
    for target, root in _roots(roots).items():
        folder = root / SKILL_NAME
        groups.setdefault(os.path.realpath(folder), []).append(
            (target, root, os.path.islink(folder)))

    content = None
    results = {}
    for members in groups.values():
        chosen = [member for member in members if member[0] in selected]
        writers = [member for member in chosen if not member[2]]
        if writers:
            try:
                # 只渲染一次：同一份设置写到几个位置，内容完全一样。
                if content is None:
                    content = render(settings)
                outcome = _write(writers[0][1] / SKILL_NAME, content)
            except (OSError, ValueError, KeyError):
                outcome = "failed"
            for target, _root, _link in members:
                results[target] = outcome
        elif chosen:
            # 勾选了，但它的 xhs-wiki 是指向别处的链接，组里也没有一个可以
            # 安全写入的位置：不顺着链接写。
            for target, _root, _link in members:
                results[target] = "kept_symlink" if target in selected else "unchanged"
        else:
            for target, root, _link in members:
                if target not in withdrawn:
                    results[target] = "unchanged"
                    continue
                try:
                    results[target] = remove(root)
                except OSError:
                    results[target] = "failed"
    return results


def _same_directory(left: Path, right: Path) -> bool:
    """两个路径是不是同一个目录（按真实路径比较，读不了时当成不是）。"""
    try:
        return os.path.realpath(left) == os.path.realpath(right)
    except OSError:
        return False


def _detect_dirs(home: Path = None) -> tuple:
    """检测用的主目录和"应用程序"文件夹列表。

    调用方传了 home（测试）或者设了 XHS_WIKI_TOOLS_HOME 时，主目录和
    "应用程序"文件夹都换到它下面；否则看真实主目录、系统的"应用程序"
    （SYSTEM_APPLICATIONS）和 ~/Applications。XHS_WIKI_TOOLS_HOME 正好
    就是真实主目录时，按没设处理——不然连系统"应用程序"里的 App 都看不到。
    """
    if home is None and _TOOLS_HOME_OVERRIDE and not _same_directory(_TOOLS_HOME, Path.home()):
        home = _TOOLS_HOME
    if home is not None:
        return Path(home), (Path(home) / "Applications",)
    real_home = Path.home()
    return real_home, (SYSTEM_APPLICATIONS, real_home / "Applications")


def detect_tools(*, home: Path = None) -> list:
    """返回本机检测到的 AI 工具名（_TOOL_MARKERS 里的名字，按表中顺序）。

    判断方法：主目录下有这个工具自己建的文件或文件夹，或者"应用程序"
    文件夹里有它的 App（线索见 _TOOL_MARKERS）。只看路径存不存在，不读
    任何内容；结果只用来在页面上提示"本机有 xx"，不影响装不装（装哪些
    由用户勾选）。用 os.path.exists：遇到没有进入权限的目录（比如被 sudo
    建成 root 所有的 ~/.config）返回 False，不会抛异常拖垮状态接口。
    """
    home_dir, app_dirs = _detect_dirs(home)
    found = []
    for name, _target, home_markers, apps in _TOOL_MARKERS:
        paths = [home_dir / marker for marker in home_markers]
        paths.extend(app_dir / app for app_dir in app_dirs for app in apps)
        if any(os.path.exists(path) for path in paths):
            found.append(name)
    return found


def _display_path(path: Path, home: Path) -> str:
    """给页面看的路径：主目录开头的部分写成 ~，短一些。"""
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def targets_state(settings: dict, *, roots: dict = None, home: Path = None,
                  last_results: dict = None) -> list:
    """控制台要显示的各安装位置状态（/api/status 的 skill_targets）。

    每一项：
    - id（位置名）、selected（是否勾选）、path（Skill 文件夹，主目录写成 ~）；
    - detected_tools：本机检测到的、会读这个位置的工具；
    - state：installed（这里有本工具写的 SKILL.md）、foreign（有一份同名、
      但不是本工具写的）、absent（没有）；installed 布尔值和 state 一致；
    - symlink：xhs-wiki 文件夹本身是不是符号链接；
    - last_result：服务这次启动以来，这个位置最近一次同步的结果代码
      （没同步过是 None）；页面据此提示"上次没写成功"。
    所有判断都不会因为没权限抛异常（见 ownership、detect_tools）。
    """
    selected = selected_targets(settings)
    home_dir, _apps = _detect_dirs(home)
    detected = set(detect_tools(home=home))
    last_results = last_results or {}
    state = []
    for target, root in _roots(roots).items():
        folder = root / SKILL_NAME
        owner = ownership(folder / "SKILL.md")
        status = {"ours": "installed", "foreign": "foreign"}.get(owner, "absent")
        state.append({
            "id": target,
            "selected": target in selected,
            "detected_tools": [name for name, holder, _f, _a in _TOOL_MARKERS
                               if holder == target and name in detected],
            "path": _display_path(folder, home_dir),
            "state": status,
            "installed": status == "installed",
            "symlink": os.path.islink(folder),
            "last_result": last_results.get(target),
        })
    return state


def fallback_state(settings: dict) -> list:
    """targets_state 万一出错时给页面的最简状态：只有位置名和勾选情况。

    状态接口不能因为某个技能目录的问题整个失败（控制台会一直显示
    "暂时无法读取"）。这里不碰任何文件系统路径。
    """
    selected = selected_targets(settings)
    return [{"id": target, "selected": target in selected, "detected_tools": [],
             "path": "", "state": "absent", "installed": False, "symlink": False,
             "last_result": None}
            for target in SKILL_TARGETS]


def installer_sync(skip=(), *, roots: dict = None, stream=None) -> int:
    """安装脚本第 7 步：按设置把 Skill 写进勾选的位置（只写不撤），返回退出码。

    - ``skip``：这次不碰的位置名（没检测到 Claude Code 时，install.sh 传
      claude_code 进来）；
    - 勾选的位置里，写不成功的打印位置名和目录到标准错误（不带异常原文）；
      已有别人的同名技能、或者是符号链接没写的，也打印出来；
    - 只有 Claude Code 写入失败才返回 1，让安装停下（和以前只有 Claude Code
      一个位置时一样）；其他位置失败只提示，安装照常往下走，用户之后可以在
      控制台的"在哪些 AI 工具里提问"里检查后重新保存。
    设置里的 skill_targets 读法和控制台一致（selected_targets：坏值按默认）。
    """
    from . import config
    from .store import ServiceStore

    stream = stream or sys.stderr
    settings = ServiceStore(config.app_home()).load_settings()
    chosen = selected_targets(settings) - set(skip)
    results = sync({**settings, "skill_targets": sorted(chosen)}, roots=roots)
    resolved = _roots(roots)
    messages = {
        "failed": "技能没写成功（检查这个目录是否可写）",
        "kept_foreign": "那里已有一份不是 xhs-wiki 写的同名技能，没有覆盖",
        "kept_symlink": "xhs-wiki 是指向别处的符号链接，没有写",
    }
    for target in SKILL_TARGETS:
        code = results.get(target)
        if target in chosen and code in messages:
            folder = resolved[target] / SKILL_NAME
            print(f"    {target}：{messages[code]}：{folder}", file=stream)
    return 1 if results.get("claude_code") == "failed" and "claude_code" in chosen else 0
