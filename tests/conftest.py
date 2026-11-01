# -*- coding: utf-8 -*-
"""tests/conftest.py —— 整个测试会话的临时环境。

做什么：在 pytest 收集任何测试之前，把 XHS_WIKI_HOME、XHS_WIKI_LOG_DIR
和 XHS_WIKI_SKILLS_ROOT 指到本会话专属的临时目录（已存在则不动，方便
手动覆盖复现问题），并去掉开发者终端里可能设置的 HF_ENDPOINT。

为什么需要：claude/codex 提供方启动子进程时会调用
config.llm_work_dir()，它会在 APP_HOME 下建目录。如果某个测试文件忘了
设置 XHS_WIKI_HOME（比如早期的 test_llm.py），就会在真实的
~/Library/Application Support/xhs-wiki 下建目录，违反测试规矩
（不在真实主目录、~/Library 下创建任何东西）。在这里统一兜底，
单个测试仍可用 monkeypatch 覆盖成自己的临时目录。
"""

import os
import tempfile

# 会话级临时根目录：pytest 退出时不自动清理（保持和各测试自带
# tempfile.TemporaryDirectory 的行为一致），路径在系统临时区。
_SESSION_TMP = tempfile.mkdtemp(prefix="xhs-wiki-tests-")

if not os.environ.get("XHS_WIKI_HOME"):
    os.environ["XHS_WIKI_HOME"] = os.path.join(_SESSION_TMP, "app-home")
if not os.environ.get("XHS_WIKI_LOG_DIR"):
    os.environ["XHS_WIKI_LOG_DIR"] = os.path.join(_SESSION_TMP, "logs")
# Skill 的默认安装位置同样兜底到临时目录。xhs_wiki.skill 在
# 被导入时读这个变量；万一有测试忘了传 skills_root，也不会写进真实的
# ~/.claude/skills（直接运行 pytest tests -q、没有手动设置时同样安全）。
if not os.environ.get("XHS_WIKI_SKILLS_ROOT"):
    os.environ["XHS_WIKI_SKILLS_ROOT"] = os.path.join(_SESSION_TMP, "skills")

# 开发者终端里常会设 HF_ENDPOINT（Hugging Face 镜像地址）。
# 语音模型下载会优先尝试它，测试里假下载器断言的请求顺序就会被打乱。
# 统一在测试会话开始时去掉，需要它的测试自己用 monkeypatch.setenv 设置。
os.environ.pop("HF_ENDPOINT", None)
