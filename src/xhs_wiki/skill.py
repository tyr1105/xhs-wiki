# -*- coding: utf-8 -*-
"""skill.py —— 渲染、安装和卸载 Claude Code Skill。

这个文件做什么：把 ``skill/SKILL.md.tmpl`` 模板渲染成实际的 SKILL.md
（填入用户自己的知识库路径和引擎类型），写到
``<skills_root>/xhs-wiki/SKILL.md``；也能把它删掉（卸载）。

在整体里的位置：docs/architecture.md §12。被 daemon.py 调用（首次设置
完成、修改 kb_root 或 engine 后自动更新 Skill），也被 install.sh 间接
调用（安装时写"尚未完成设置"版本）。

红线自查：模板和渲染结果里只有知识库路径和引擎名——没有任何领域词、
主题分类、笔记编号或签名链接。description 必须保持通用，不列具体领域
（否则会变成暗示用户的收藏范围）。

测试支持：``skills_root`` 参数注入临时目录，安装/卸载绝不碰真实
``~/.claude``。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

# 模板随包分发（src/xhs_wiki/skill/SKILL.md.tmpl），只读。
TEMPLATE_PATH = Path(__file__).resolve().parent / "skill" / "SKILL.md.tmpl"

# Skill 的名字（也是 skills_root 下的目录名）。
SKILL_NAME = "xhs-wiki"

# Skill 默认安装位置：Claude Code 的用户级技能目录。
# 环境变量 XHS_WIKI_SKILLS_ROOT 可以改写它：做隔离测试时，服务用真实主目录运行（Claude 登录态要用），
# 但 Skill 必须写进测试目录，不能写进用户真实的 ~/.claude/skills。
DEFAULT_SKILLS_ROOT = Path(os.environ.get("XHS_WIKI_SKILLS_ROOT") or "~/.claude/skills").expanduser()

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


def render(settings: dict) -> str:
    """把模板渲染成 SKILL.md 全文。

    ``settings`` 是 store.load_settings() 的形状，用到三个键：
    - ``kb_root``：知识库根目录（渲染时 expanduser 成绝对路径）；
    - ``engine``：引擎名（markdown / llmwiki），转成中文说明；
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
    notice = "" if settings.get("setup_completed") else _SETUP_PENDING_NOTICE
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    return (template
            .replace("{{KB_ROOT}}", kb_root)
            .replace("{{ENGINE_LABEL}}", engine_label)
            .replace("{{SETUP_NOTICE}}", notice))


def install(settings: dict, *, skills_root: Path = None) -> Path:
    """渲染并写入 Skill，返回 SKILL.md 的路径。

    写法遵循项目规范：先写同目录临时文件，再 ``os.replace`` 原子替换；
    目录权限 0700、文件 0600（Skill 里有用户的知识库路径，算私有数据）。
    """
    root = Path(skills_root) if skills_root is not None else DEFAULT_SKILLS_ROOT
    target_dir = root / SKILL_NAME
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target_dir.chmod(0o700)
    target = target_dir / "SKILL.md"
    content = render(settings)
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


def uninstall(skills_root: Path = None) -> bool:
    """删除已安装的 Skill 目录，返回是否删了东西（本来就没有则 False）。

    只删 ``<skills_root>/xhs-wiki`` 这一个目录，绝不碰 skills_root 下的
    其他技能。
    """
    root = Path(skills_root) if skills_root is not None else DEFAULT_SKILLS_ROOT
    target = root / SKILL_NAME
    if not target.exists():
        return False
    shutil.rmtree(target)
    return True
