# -*- coding: utf-8 -*-
"""给 LLM Wiki 后台生成 claude / codex 包装脚本及模型配置。

后台用 PATH 查找整理工具，本文件在 APP_HOME/llmwiki-bin 中生成同名
脚本，让所选模型及真实账户的 HOME/USER 在精简子进程环境下仍然生效。
只要求当前提供方可用；Claude 打印模式在支持且未传入时补齐
--safe-mode 和 --no-session-persistence，隔离个人定制并避免会话落盘。
脚本不记录凭据或提示词。
"""

from __future__ import annotations

import os
import pwd
import re
import shlex
import tempfile
from pathlib import Path

from xhs_wiki import config
from xhs_wiki.llm.claude_code import build_env, find_claude, supported_print_flags
from xhs_wiki.llm.codex import find_codex

# 只允许 CLI 模型号的普通字符；空串表示沿用工具默认模型。
_MODEL_RE = re.compile(r"[A-Za-z0-9._:\[\]-]{0,100}")


def default_bin_dir() -> Path:
    """返回后台 PATH 最前面的私有包装目录。"""

    return config.app_home() / "llmwiki-bin"


def model_conf_path(bin_dir=None) -> Path:
    """返回可由 shell 读取的模型配置文件路径。"""

    return (Path(bin_dir) if bin_dir is not None else default_bin_dir()) / "model.conf"


def _atomic_write(path: Path, content: str, *, mode: int) -> None:
    """内容不变时不重写；变更时用私有临时文件加原子替换发布。

    原子替换是一次性切换完整文件，避免后台读到半份 shell 脚本。
    同时修正已有文件权限，权限修正不会改变内容的修改时间。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.is_file() and not path.is_symlink() and path.read_text(encoding="utf-8") == content:
        os.chmod(path, mode)
        return
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _claude_script(real_path: str, home: str, user: str, *, safe_mode=False,
                   no_session_persistence=False) -> str:
    """生成 Claude 脚本；所有宿主字符串都用 shell 引号安全编码。

    shlex.quote 把字符串编码成一个 shell 参数，即使含空格、单引号、
    美元符号或反引号也保持原文，不会变成 shell 命令。
    HOME/USER 由安装函数从系统账户数据库取得，不信任环境变量。
    后台可能已经带上隔离参数，脚本只在打印模式补缺失项，不重复添加。
    """

    safe_line = ('  [ "$HAS_SAFE_MODE" = 1 ] || set -- --safe-mode "$@"\n'
                 if safe_mode else "")
    session_line = ('  [ "$HAS_NO_SESSION" = 1 ] || set -- --no-session-persistence "$@"\n'
                    if no_session_persistence else "")
    return f'''#!/bin/bash
# xhs-wiki 生成的 claude 包装脚本（见 engines/llmwiki/agents.py）。
# 打印模式追加模型和受支持的隔离参数，其他调用原样转发。
# model.conf 每次现读，不读、不写、不打印凭据或提示词。
REAL={shlex.quote(real_path)}
HERE="$(cd "$(dirname "$0")" && pwd)"
CONF="$HERE/model.conf"
MODEL=''
[ -r "$CONF" ] && . "$CONF"
export HOME={shlex.quote(home)}
export USER={shlex.quote(user)}
unset CLAUDE_CONFIG_DIR
HAS_PRINT=0
HAS_SAFE_MODE=0
HAS_NO_SESSION=0
for arg in "$@"; do
  case "$arg" in
    --print|-p) HAS_PRINT=1 ;;
    --safe-mode) HAS_SAFE_MODE=1 ;;
    --no-session-persistence) HAS_NO_SESSION=1 ;;
  esac
done
if [ "$HAS_PRINT" = 1 ]; then
{session_line}{safe_line}  if [ -n "$MODEL" ]; then
    case "$MODEL" in
      *"[1m]"*) ;;
      claude-*-5-5)
        MODEL="${{MODEL}}[1m]"
        export CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000
        ;;
    esac
    set -- --model "$MODEL" "$@"
  fi
fi
exec "$REAL" "$@"
'''


def _codex_script(real_path: str, home: str, user: str) -> str:
    """只在 codex exec 下追加模型，恢复精简环境中缺失的账户信息。"""

    return f'''#!/bin/sh
# xhs-wiki 生成的 codex 包装脚本（见 engines/llmwiki/agents.py）。
# 模型为空时不追加 -m；所有原始参数都按原边界转发。
REAL={shlex.quote(real_path)}
HERE="$(cd "$(dirname "$0")" && pwd)"
CONF="$HERE/model.conf"
MODEL=''
[ -r "$CONF" ] && . "$CONF"
export HOME={shlex.quote(home)}
export USER={shlex.quote(user)}
if [ "${{1:-}}" = "exec" ]; then
  shift
  if [ -n "$MODEL" ]; then
    exec "$REAL" exec -m "$MODEL" "$@"
  fi
  exec "$REAL" exec "$@"
fi
exec "$REAL" "$@"
'''


def write_model_config(model: str, bin_dir=None) -> Path:
    """校验模型名后安全写入配置，不把输入当成可执行的 shell 文本。"""

    if not isinstance(model, str) or _MODEL_RE.fullmatch(model) is None:
        raise ValueError("llm_model_invalid")
    path = model_conf_path(bin_dir)
    _atomic_write(path, f"MODEL={shlex.quote(model)}\n", mode=0o600)
    return path


def install_wrappers(*, provider="claude_code", claude_path=None, codex_path=None,
                     bin_dir=None, runner=None) -> dict:
    """为已安装的 CLI 生成脚本，只强制要求 provider 对应的那个。

    provider 是设置里的 claude_code 或 codex；默认值兼容旧调用。
    路径、帮助探测 runner 均可注入；发现路径后还要确认可执行，避免
    给用户生成永远无法运行的包装脚本。帮助探测每次生成只执行一次。
    """

    required = {"claude_code": "claude", "codex": "codex"}.get(provider)
    if required is None:
        raise ValueError("llmwiki_provider_unsupported")
    directory = Path(bin_dir) if bin_dir is not None else default_bin_dir()
    candidates = {
        "claude": claude_path if claude_path is not None else find_claude(),
        "codex": codex_path if codex_path is not None else find_codex(),
    }
    available = {}
    for name, value in candidates.items():
        if value and Path(value).is_file() and os.access(value, os.X_OK):
            # 只补成绝对路径、不解析符号链接：claude 的安装入口通常是指向
            # versions/<版本号> 的链接，升级后旧版本文件会被清理；写死解析后
            # 的版本文件，升级后包装脚本就找不到程序了。
            available[name] = Path(value).absolute()
    if required not in available:
        # 保留安装函数既有的具体错误码；引擎边界统一为 agent_missing。
        raise ValueError(f"{required}_executable_not_found")

    account = pwd.getpwuid(os.getuid())
    home, user = account.pw_dir, account.pw_name
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    paths = {}
    for name, executable in available.items():
        if name == "claude":
            # 帮助命令也用真实账户信息；显式工作目录避免读取别的项目。
            env = build_env({**os.environ, "HOME": home, "USER": user})
            supported = supported_print_flags(str(executable), runner=runner,
                                              env=env, cwd=directory)
            content = _claude_script(
                str(executable), home, user, safe_mode="--safe-mode" in supported,
                no_session_persistence="--no-session-persistence" in supported)
        else:
            content = _codex_script(str(executable), home, user)
        paths[name] = directory / name
        _atomic_write(paths[name], content, mode=0o700)
    # 旧版本留下的包装脚本可能仍指向已卸载 CLI，删除本工具拥有的同名
    # 脚本，避免后台误把它识别成另一个仍可用的提供方。
    for name in candidates.keys() - available.keys():
        stale = directory / name
        if stale.is_file() or stale.is_symlink():
            stale.unlink()
    return paths
