# -*- coding: utf-8 -*-
"""app.py —— nashsu/llm_wiki 桌面应用的安装位置、版本、全局状态与启动。

这个文件做什么：把所有跟"应用本身"打交道的操作集中到一个模块——
找应用包（.app）、读版本（Info.plist）、读全局状态 app-state.json、
判断整理用的提供方是否可用、检测进程是否在运行、后台启动应用。

在整体里的位置：engines/llmwiki_app 包的地基之一，被同包 engine.py
和 cli.py 的 doctor 调用。

真实验证过的事实（2026-11-19，样例 .dev/fixtures/nashsu-0.6.13/）：
- 应用装在 /Applications/LLM Wiki.app 或 ~/Applications/LLM Wiki.app，
  bundle id 是 com.llmwiki.app；
- 全局状态在 ~/Library/Application Support/com.llmwiki.app/app-state.json，
  ``lastProject.path`` 是当前打开的项目；``taskModelRouting.ingestPresetId``
  （为 null 时用 ``activePresetId``）指向 ``providerConfigs`` 里的一项；
  提供方配置里可能有明文 apiKey。

安全红线：
- app-state.json **只读**，绝不修改；
- apiKey 只判断"是否为空"，绝不把它的值放进日志、异常、返回值；
- 所有外部依赖（应用目录、状态文件路径、进程检测、启动命令、等待）
  都可以注入，测试不碰真实的 ~/Library。
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

# 应用包名与 bundle id（样例核实）。
BUNDLE_NAME = "LLM Wiki.app"
BUNDLE_ID = "com.llmwiki.app"
# 支持的最低版本（CFBundleShortVersionString 低于它视为太旧）。
MIN_VERSION = (0, 6, 13)

# 默认去找应用包的两个位置（/Applications 和用户自己的 ~/Applications）。
DEFAULT_APP_DIRS = (Path("/Applications"), Path.home() / "Applications")

# 全局状态文件的默认位置。
DEFAULT_STATE_PATH = (Path.home() / "Library" / "Application Support"
                      / BUNDLE_ID / "app-state.json")

# 不需要 API 密钥的提供方类型：两个命令行助手和本机 ollama。
# 判断依据是提供方配置里的 provider / apiMode 字段（样例 app-state）；
# 字段读不出来时按"需要密钥而没有"处理（任务卡的约定：读不出当缺失）。
NO_KEY_KINDS = frozenset({"claude-code", "codex-cli", "ollama"})


def find_bundle(app_dirs=None) -> Path | None:
    """找应用包：/Applications 和 ~/Applications 里找 LLM Wiki.app。

    返回 ``Contents`` 的父目录（即 ``…/LLM Wiki.app``）；两处都没有
    返回 None。"""

    for base in (app_dirs if app_dirs is not None else DEFAULT_APP_DIRS):
        candidate = Path(base) / BUNDLE_NAME
        if candidate.is_dir():
            return candidate
    return None


def read_bundle_version(bundle: Path):
    """读应用包的 CFBundleShortVersionString，返回 (主, 次, 修订) 元组。

    读不了（包损坏、没有 Info.plist、字段不是数字版本）返回 None——
    版本无法确认时按"太旧"处理，保守拒绝而不是放行。"""

    try:
        with (Path(bundle) / "Contents" / "Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        raw = info.get("CFBundleShortVersionString")
        parts = str(raw).split(".")
        numbers = tuple(int(part) for part in parts[:3])
        if len(numbers) != 3:
            return None
        return numbers
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def version_ok(version) -> bool:
    """版本是否不低于 MIN_VERSION。None（读不出）不算通过。"""

    return version is not None and tuple(version) >= MIN_VERSION


def version_text(version) -> str:
    """版本元组转成给人看的字符串（读不出返回 "未知"）。"""

    return ".".join(str(part) for part in version) if version else "未知"


class AppController:
    """应用的进程与全局状态控制器（全部依赖可注入，测试用假实现）。

    - ``process_running``：() -> bool，应用是否在运行。默认用
      ``pgrep -x llm-wiki``（参数列表启动，不用 shell）；
    - ``launch``：() -> 启动应用（默认 ``open -g -j -b com.llmwiki.app``，
      -g 不切到前台、-j 启动为后台应用，不打扰用户）；
    - ``state_path``：app-state.json 的路径（默认
      ~/Library/Application Support/com.llmwiki.app/app-state.json）。
    """

    def __init__(self, *, app_dirs=None, state_path=None,
                 process_running=None, launch=None) -> None:
        self.app_dirs = app_dirs
        self.state_path = Path(state_path) if state_path is not None \
            else DEFAULT_STATE_PATH
        if process_running is None:
            def _pgrep() -> bool:
                # pgrep 按名字精确匹配（-x）进程；返回码 0 表示找到了。
                result = subprocess.run(["pgrep", "-x", "llm-wiki"],
                                        check=False,
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
                return result.returncode == 0
            process_running = _pgrep
        self.process_running = process_running
        if launch is None:
            def _open() -> None:
                subprocess.run(["open", "-g", "-j", "-b", BUNDLE_ID],
                               check=False,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            launch = _open
        self.launch = launch

    # ---------------- 状态文件（只读） ----------------

    def read_state(self):
        """读 app-state.json，返回 dict；读不了返回 None。

        只读。文件里的 apiKey 等敏感字段只在本模块的
        ingest_provider_ok 里被"是否为空"地判断，绝不外传。"""

        try:
            import json
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def last_project(self, state=None):
        """应用当前打开的项目 (path, name)；读不出返回 (None, None)。

        path 之后由调用方 resolve 规范化再比较（字面相等不可靠）。"""

        if state is None:
            state = self.read_state()
        if not isinstance(state, dict):
            return None, None
        project = state.get("lastProject")
        if not isinstance(project, dict):
            return None, None
        path = project.get("path")
        name = project.get("name")
        return (str(path) if path else None,
                str(name) if name else None)

    def ingest_provider_ok(self, state=None) -> bool:
        """整理用的提供方是否已配置且可用（不花钱、不碰密钥内容）。

        规则（任务卡）：``taskModelRouting.ingestPresetId``（为空用
        ``activePresetId``）必须能在 ``providerConfigs`` 里找到；找不到、
        状态文件读不了，或该提供方需要密钥而 apiKey 为空 → 不可用。
        命令行类（claude-code / codex-cli）和本机 ollama 不需要密钥。
        判断依据是提供方配置的 ``provider`` / ``apiMode`` 字段，两个字段
        都读不出时按"需要密钥而没有"处理。
        """

        if state is None:
            state = self.read_state()
        if not isinstance(state, dict):
            return False
        routing = state.get("taskModelRouting")
        preset_id = (routing.get("ingestPresetId")
                     if isinstance(routing, dict) else None)
        if not preset_id:
            preset_id = state.get("activePresetId")
        if not preset_id:
            return False
        configs = state.get("providerConfigs")
        preset = configs.get(preset_id) if isinstance(configs, dict) else None
        if not isinstance(preset, dict):
            return False
        kind = preset.get("apiMode") or preset.get("provider")
        if isinstance(kind, str) and kind.strip().lower() in NO_KEY_KINDS:
            return True
        # 其余类型（Anthropic / OpenAI 兼容等）都要有密钥：只判断有没有。
        key = preset.get("apiKey")
        return isinstance(key, str) and bool(key.strip())
