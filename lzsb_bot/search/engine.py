#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三种搜索模式的调度实现。

设计原则:每个模式都是一次「并行发起多源 → 合并去重 → 按优先级排序」,
差异只在**打哪些源、按什么顺序**。所有模式都遵守:
  - 单源独立超时,失败静默(搜索引擎经常限流/改版,不能拖垮整次搜索)
  - 结果按来源优先级排序(并行完成顺序是随机的,不排序噪音源会挤到前面)
  - 按 URL 去重
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from ._http import filter_spam, has_cjk
from .sources import SOURCES, MODE_SOURCES


def _run_sources(names, query, proxy, timeout, k):
    """并行跑一组来源,任一失败静默;返回 [(源名, items), ...]。"""
    names = [n for n in names if n in SOURCES]
    if not names:
        return []
    out = []
    with ThreadPoolExecutor(max_workers=len(names)) as ex:
        futs = {ex.submit(SOURCES[n], query, proxy, timeout, k): n for n in names}
        try:
            for f in as_completed(futs, timeout=timeout + 8):
                name = futs[f]
                try:
                    items = f.result() or []
                except Exception:
                    items = []            # 单源失败:当作该源空结果
                out.append((name, items))
        except Exception:
            pass                          # 整体超时:已拿到的照常用
    return out


def _merge(groups, k):
    """按 groups 的先后顺序合并、按 URL 去重、滤垃圾、截 topK。

    groups 是 [[items...], [items...]] 有序列表 —— 顺序即优先级。
    """
    seen, out = set(), []
    for items in groups:
        for it in items:
            key = (it.get('url') or it.get('title') or '').strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(it)
            if len(out) >= k:
                return out
    return out


# ---------------------------------------------------------------------------
# 模式 1:智能路由
# ---------------------------------------------------------------------------

def search_smart(query, topk=5, timeout=12, proxy=''):
    """按查询语言分流:

    中文词:GoogleNews + Bing 两路并行 → HN 垫底补技术视角
    英文词:HN + StackOverflow + GitHub(技术垂直)→ GoogleNews + Bing 通用兜底

    参考 userscript 的 smart 引擎思路,但在 Python 里简化为「一次并行 + 优先级合并」——
    多级竞速在本地单进程下收益有限,反而让超时难控。
    """
    cjk = has_cjk(query)
    if cjk:
        # 中文:新闻源最相关,通用网页补上下文,技术源垫底
        ordered = ['google_news', 'bing', 'hn']
        primary = ['google_news', 'bing']
    else:
        # 英文:技术垂直源优先(问题正好是技术向的可能性高)
        ordered = ['hn', 'stackoverflow', 'github', 'google_news', 'bing']
        primary = ['hn', 'stackoverflow', 'github']

    by_name = {}
    got_any = False
    for name, items in _run_sources(primary, query, proxy, timeout, topk):
        items = filter_spam(items)
        if items:
            got_any = True
        by_name[name] = items

    # 主源全空才去打兜底源,省一次网络往返
    if not got_any:
        rest = [n for n in ordered if n not in primary]
        for name, items in _run_sources(rest, query, proxy, timeout, topk):
            by_name[name] = filter_spam(items)

    # 严格按 ordered 的顺序拼接,而不是按并行完成顺序 ——
    # 否则噪音源的结果会挤到前面,盖掉高质量来源。
    return _merge([by_name.get(n, []) for n in ordered], topk)


def _src_of(item):
    """把 source 标签映射回内部源名,用于排序。"""
    label = (item.get('source') or '').lower()
    table = {
        'googlenews': 'google_news', 'news': 'google_news',
        'bing': 'bing',
        'hn': 'hn', 'hackernews': 'hn',
        'stackoverflow': 'stackoverflow',
        'github': 'github',
    }
    return table.get(label, label)


# ---------------------------------------------------------------------------
# 模式 2:多源聚合(只打官方 API/RSS)
# ---------------------------------------------------------------------------

def search_multi(query, topk=5, timeout=12, proxy=''):
    """只用官方 JSON/RSS 接口:不受改版、反爬、品牌霸屏影响。

    缺点是非技术类的中文话题覆盖弱 —— 想要通用网页覆盖请用模式 1 或 3。
    """
    names = MODE_SOURCES[2]
    groups = []
    pairs = _run_sources(names, query, proxy, timeout, topk)
    order = {n: i for i, n in enumerate(names)}
    for name, items in sorted(pairs, key=lambda p: order.get(p[0], 99)):
        groups.append(filter_spam(items))
    return _merge(groups, topk)


# ---------------------------------------------------------------------------
# 模式 3:Bing 单源
# ---------------------------------------------------------------------------

def search_bing(query, topk=5, timeout=12, proxy=''):
    """Bing 网页直连。通用兜底,结果偏杂,但中文覆盖率不错。"""
    try:
        items = SOURCES['bing'](query, proxy, timeout, topk) or []
    except Exception:
        return []
    return _merge([filter_spam(items)], topk)


# ---------------------------------------------------------------------------
# 调度表
# ---------------------------------------------------------------------------

ENGINES = {
    1: search_smart,
    2: search_multi,
    3: search_bing,
}


def run(mode, query, topk=5, timeout=12, proxy=''):
    """按模式号调度。认不出的模式回退到智能路由。"""
    fn = ENGINES.get(mode) or search_smart
    return fn(query, topk=topk, timeout=timeout, proxy=proxy)
