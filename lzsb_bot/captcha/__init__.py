#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cap 人机验证求解。

linux.sb 的登录表单挂了自托管的 Cap(https://cap.linux.sb)验证,分三层:
    1. PoW 挑战          —— 见 pow.py
    2. instrumentation   —— 见 instrumentation.py(需要系统里有 node)
    3. redeem            —— 见 solver.py

入口:`from lzsb_bot.captcha import get_cap_token`
"""

from .solver import get_cap_token
from .pow import fnv1a32, derive, solve_pow, solve_challenge
from .instrumentation import solve_instrumentation, find_node

__all__ = [
    "get_cap_token",
    "fnv1a32", "derive", "solve_pow", "solve_challenge",
    "solve_instrumentation", "find_node",
]
