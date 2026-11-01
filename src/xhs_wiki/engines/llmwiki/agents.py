# -*- coding: utf-8 -*-
"""engines/llmwiki/agents.py —— 给 LLM Wiki 后台用的 claude / codex 包装脚本。

这个文件做什么：在 APP_HOME/llmwiki-bin/ 里生成两个 bash 包装脚本
（claude、codex）和一个 model.conf。后台子进程的 PATH 把这个目录放在
最前面（backend.py），于是后台"调用 claude/codex"实际执行的是我们的
包装脚本。规则如下：

- claude：命令带 --print（或 -p）时追加 `--model <模型>`；模型名形如
  claude-*-5-5 时自动补 `[1m]` 后缀（老版本命令行不认识 5.5 完整型号
  名，会把上下文当 20 万处理；加后缀后按 100 万 token 窗口请求）；
  恢复 HOME 和 USER（后台只给极少环境变量，没有 HOME 时 Claude 报
  "Not logged in"）；去掉 CLAUDE_CONFIG_DIR（设了它 Claude 会找另一个
  钥匙串条目，同样显示未登录）。
- codex：`codex exec` 时追加 `-m <模型>`；模型为空就不追加（用用户
  自己配置的默认模型）。
- 模型名从同目录 model.conf 读，每次调用都现读——改了立即生效，
  不用重启后台。
- 真实 claude / codex 的绝对路径在生成脚本时写死（复用 xhs_wiki.llm
  里的 find_claude / find_codex 查找逻辑）。
- 不写任何调试日志、不落任何提示词（避免敏感内容进日志或磁盘）。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from xhs_wiki import config
from xhs_wiki.llm.claude_code import find_claude
from xhs_wiki.llm.codex import find_codex


def default_bin_dir() -> Path:
    """包装脚本目录：APP_HOME/llmwiki-bin/（backend.py 把它放进 PATH）。"""

    return config.app_home() / "llmwiki-bin"


def model_conf_path(bin_dir=None) -> Path:
    """model.conf 的路径（MODEL=一行，shell 可 source）。"""

    return (bin_dir or default_bin_dir()) / "model.conf"


def _atomic_write(path: Path, content: str, *, mode: int) -> None:
    """先写临时文件再 os.replace；脚本 0700、配置 0600（私有内容）。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
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


def _claude_script(real_path: str, home: str, user: str) -> str:
    """生成 claude 包装脚本的正文。

    HOME/USER 在生成时写死：这个文件只落在用户自己的 APP_HOME 里，
    值就取自当前用户自己的环境，不进仓库。
    """

    return f"""#!/bin/bash
# xhs-wiki 生成的 claude 包装脚本（见 engines/llmwiki/agents.py）。
# 后台带着 --print 调用 claude 整理 Wiki 时追加 --model；模型每次从
# model.conf 现读。不读、不写、不打印任何凭据或提示词。
REAL='{real_path}'
HERE="$(cd "$(dirname "$0")" && pwd)"
CONF="$HERE/model.conf"
MODEL=''
[ -r "$CONF" ] && . "$CONF"
export HOME='{home}'
export USER='{user}'
unset CLAUDE_CONFIG_DIR
HAS_PRINT=0
for arg in "$@"; do
  if [ "$arg" = "--print" ] || [ "$arg" = "-p" ]; then
    HAS_PRINT=1
  fi
done
if [ "$HAS_PRINT" = 1 ] && [ -n "$MODEL" ]; then
  case "$MODEL" in
    *"[1m]"*) ;;
    claude-*-5-5)
      MODEL="${{MODEL}}[1m]"
      export CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000
      ;;
  esac
  exec "$REAL" --model "$MODEL" "$@"
fi
exec "$REAL" "$@"
"""


def _codex_script(real_path: str) -> str:
    """生成 codex 包装脚本的正文。只在 `codex exec`（后台生成 Wiki 页面
    用的子命令）时追加 -m；其余用法原样转发。"""

    return f"""#!/bin/sh
# xhs-wiki 生成的 codex 包装脚本（见 engines/llmwiki/agents.py）。
# 模型每次从 model.conf 现读；为空就不追加 -m（用用户默认模型）。
REAL='{real_path}'
HERE="$(cd "$(dirname "$0")" && pwd)"
CONF="$HERE/model.conf"
MODEL=''
[ -r "$CONF" ] && . "$CONF"
if [ "${{1:-}}" = "exec" ]; then
  shift
  if [ -n "$MODEL" ]; then
    exec "$REAL" exec -m "$MODEL" "$@"
  fi
  exec "$REAL" exec "$@"
fi
exec "$REAL" "$@"
"""


def write_model_config(model: str, bin_dir=None) -> Path:
    """写 model.conf（MODEL=xxx，一行、可被 sh source）。"""

    path = model_conf_path(bin_dir)
    _atomic_write(path, f"MODEL={model}\n", mode=0o600)
    return path


def install_wrappers(*, claude_path=None, codex_path=None, bin_dir=None) -> dict:
    """生成 claude / codex 两个包装脚本，返回它们的路径。

    claude_path / codex_path 留空时用 xhs_wiki.llm 的查找逻辑
    （设置里指定的路径 → PATH → 常见安装位置）；找不到就抛 ValueError
    ——没有真实可执行文件时生成的脚本没有意义。
    """

    directory = Path(bin_dir) if bin_dir else default_bin_dir()
    resolved_claude = claude_path or find_claude()
    resolved_codex = codex_path or find_codex()
    if not resolved_claude:
        raise ValueError("claude_executable_not_found")
    if not resolved_codex:
        raise ValueError("codex_executable_not_found")
    claude = Path(resolved_claude)
    codex = Path(resolved_codex)
    if not claude.is_file():
        raise ValueError("claude_executable_not_found")
    if not codex.is_file():
        raise ValueError("codex_executable_not_found")
    home = os.environ.get("HOME", "")
    user = os.environ.get("USER", "")
    paths = {
        "claude": directory / "claude",
        "codex": directory / "codex",
    }
    _atomic_write(paths["claude"], _claude_script(str(claude), home, user), mode=0o700)
    _atomic_write(paths["codex"], _codex_script(str(codex)), mode=0o700)
    return paths
