#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简日志:同时打屏 + 追加写文件。"""

import sys
from datetime import datetime

from .paths import LOG_PATH

# Windows 控制台默认 GBK,直接 print 中文可能炸;这里强制 UTF-8。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] [%s] %s" % (ts, level, msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
