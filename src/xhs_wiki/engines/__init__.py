"""引擎公共工厂：守护进程和一次运行共用同一份引擎选择规则。

这里只负责按设置构造实例，不启动应用、不检查模型、不读取密钥。
提供方由上层注入；LLM Wiki 引擎对接 nashsu/llm_wiki 桌面应用，
不需要 xhs-wiki 自己的模型提供方。
"""

from pathlib import Path


def make_engine(settings, provider, app_home):
    """按引擎名建立实例；延迟导入使未选择的引擎不增加启动负担。"""

    name = settings.get("engine")
    if name == "llmwiki":
        from .llmwiki_app.engine import LlmWikiAppEngine

        return LlmWikiAppEngine(Path(settings["kb_root"]).expanduser(),
                                app_home, settings)
    if name == "markdown":
        from .markdown.engine import MarkdownEngine

        return MarkdownEngine(settings["kb_root"], provider)
    raise ValueError("engine_not_available:" + str(name))
