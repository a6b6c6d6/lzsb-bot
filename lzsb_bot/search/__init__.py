#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""联网搜索(免 API Key)。

对外只暴露 `search()` 和 `format_block()`,调用方不关心底下用了哪个引擎。

三种模式(与 userscript「水贴」脚本的三个选项对应):
    1 = smart  智能路由 —— 按查询语言分流,多主源并行,末位兜底(默认)
    2 = multi  多源聚合 —— 只打官方 JSON/RSS 接口,无反爬、无品牌霸屏
    3 = bing   Bing 单源直连 —— 通用兜底,结果偏杂

所有来源都是免 Key 的公开端点。任一源失败只当该源失败,不拖垮整次搜索;
全部失败时返回空列表 —— 调用方必须把「搜不到」当成正常情况处理。
"""

from .base import search, format_block, build_query, describe_mode
from .sources import SOURCES, ALL_SOURCES
from .engine import search_smart, search_multi, search_bing

__all__ = [
    "search", "format_block", "build_query", "describe_mode",
    "search_smart", "search_multi", "search_bing",
    "SOURCES", "ALL_SOURCES",
]
