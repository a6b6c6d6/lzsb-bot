#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容入口(保留旧路径)。

真正的代码已经模块化到 `lzsb_bot/` 包里。这个文件只是为了不破坏
已有的 `启动机器人.bat` / `start.sh` / 各种文档里的 `python bot.py` 写法。

新用法(推荐):
    python -m lzsb_bot [--once|--dry|--login-only|--search 关键词]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lzsb_bot.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
