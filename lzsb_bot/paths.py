#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行路径与全局常量。

所有可写文件(config.json / state.db / bot.log)一律放在**项目根目录**,
而不是包目录里 —— 这样包本身是纯代码,可直接被 import 或复制,
运行产生的数据不会混进源码树。
"""

import os

# lzsb_bot/paths.py -> lzsb_bot -> 项目根
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(PKG_DIR)

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CONFIG_EXAMPLE_PATH = os.path.join(BASE_DIR, "config.example.json")
LLM_CONFIG_PATH = os.path.join(BASE_DIR, "llm.json")
LLM_EXAMPLE_PATH = os.path.join(BASE_DIR, "llm.example.json")
DB_PATH = os.path.join(BASE_DIR, "state.db")
LOG_PATH = os.path.join(BASE_DIR, "bot.log")

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:142.0) "
              "Gecko/20100101 Firefox/142.0")
