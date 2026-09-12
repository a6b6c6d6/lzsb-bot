#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对外统一入口:调用方只跟这两个函数打交道。"""

import re

from . import engine
from .sources import MODE_SOURCES

DEFAULT_MODE = 1


def search(query, topk=5, timeout=12, proxy='', sources=None, mode=None):
    """搜索,返回 [{title,url,snippet,date,source}, ...](已去重)。

    mode      1/2/3(见 config.SEARCH_MODE_*);None 时用默认 = 1 智能路由
    sources   显式指定来源列表,会覆盖 mode 的选择(给 advanced 用户留的口子)

    任一时间点所有来源都失败时返回空列表 —— 调用方应把「搜不到」当成正常情况。
    """
    q = (query or '').strip()
    if not q:
        return []

    if sources:
        return _search_explicit(q, list(sources), topk, timeout, proxy)

    return engine.run(mode or DEFAULT_MODE, q, topk=topk,
                      timeout=timeout, proxy=proxy)


def _search_explicit(query, names, topk, timeout, proxy):
    """显式指定来源:走 multi 的合并逻辑,但用调用方给的顺序。"""
    from ._http import filter_spam
    from .sources import SOURCES
    names = [n for n in names if n in SOURCES]
    if not names:
        return []
    groups = []
    for name, items in engine._run_sources(names, query, proxy, timeout, topk):
        groups.append(filter_spam(items))
    order = {n: i for i, n in enumerate(names)}

    def key(item):
        return order.get(engine._src_of(item), 99)
    return engine._merge([sorted(g, key=key) for g in groups], topk)


def build_query(topic, max_len=60):
    """从帖子标题提炼搜索词:去掉各种括号装饰与多余空白。"""
    t = topic.get('title') or ''
    t = re.sub(r'[【】\[\]（）()「」《》]', ' ', t)
    t = re.sub(r'[#@*`~|]+', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()[:max_len]


def format_block(results):
    """把搜索结果格式化成提示词里的一段。"""
    if not results:
        return ''
    lines = []
    for i, r in enumerate(results, 1):
        seg = '%d. %s' % (i, r.get('title') or '')
        if r.get('snippet'):
            seg += ' —— ' + r['snippet']
        meta = [x for x in (r.get('source'), (r.get('date') or '')[:16]) if x]
        if meta:
            seg += ' (%s)' % ', '.join(meta)
        lines.append(seg)
    return ('【联网搜索到的相关背景资料】\n' + '\n'.join(lines)
            + '\n(以上资料仅作背景参考,不必逐条引用,也不要编造资料里没有的信息)')


# ---------------------------------------------------------------------------

def describe_mode(mode):
    """给启动横幅用的一行说明。"""
    from ..config import SEARCH_MODE_LABELS
    label = SEARCH_MODE_LABELS.get(mode, '未知')
    srcs = MODE_SOURCES.get(mode) or []
    return '%s [%s]' % (label, ', '.join(srcs) if srcs else '按语言分流')
