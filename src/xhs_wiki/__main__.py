# -*- coding: utf-8 -*-
"""__main__.py —— 让 `python -m xhs_wiki` 等价于 `xhs-wiki` 命令。

为什么需要它：安装脚本就绪前（或测试里）直接用解释器运行最方便；
入口统一走 cli.main，参数和行为与 `xhs-wiki` 完全一致。
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
