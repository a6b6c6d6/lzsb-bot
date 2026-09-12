#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""各搜索来源的实现。

统一的返回结构:
    {'title': str, 'url': str, 'snippet': str, 'date': str, 'source': str}

新增一个来源只需写一个函数 + 注册到 SOURCES,引擎层不用改。
函数签名统一为 `fn(query, proxy, timeout, k) -> list`。
"""

import json
import re
import xml.etree.ElementTree as ET

from ._http import get, q, clean, unwrap_bing_url, filter_spam


# ---------------------------------------------------------------------------
# Google News RSS —— 标准 RSS,专供程序消费,中英通吃,最稳
# ---------------------------------------------------------------------------

def google_news(query, proxy, timeout, k):
    url = ('https://news.google.com/rss/search?q=' + q(query)
           + '&hl=zh-CN&gl=CN&ceid=CN:zh-Hans')
    root = ET.fromstring(get(url, proxy, timeout))
    out = []
    for it in root.iter('item'):
        title = clean(it.findtext('title'))
        if not title:
            continue
        out.append({
            'title': title,
            'url': (it.findtext('link') or '').strip(),
            'snippet': clean(it.findtext('description'))[:220],
            'date': (it.findtext('pubDate') or '').strip()[:31],
            'source': 'GoogleNews',
        })
        if len(out) >= k:
            break
    return out


# ---------------------------------------------------------------------------
# Bing 网页 —— 解析 li.b_algo;需解 ck/a 跳转壳
# ---------------------------------------------------------------------------

def bing(query, proxy, timeout, k):
    url = ('https://www.bing.com/search?q=' + q(query)
           + '&setlang=zh-CN&ensearch=0&count=25')
    html = get(url, proxy, timeout)
    out = []
    for m in re.finditer(r'<li class="b_algo".*?(?=<li class="b_algo"|</ol>)', html, re.S):
        blk = m.group(0)
        a = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
        if not a:
            continue
        title = clean(a.group(2))
        if not title:
            continue
        cap = re.search(r'<p[^>]*>(.*?)</p>', blk, re.S)
        out.append({
            'title': title,
            'url': unwrap_bing_url(a.group(1)),
            'snippet': clean(cap.group(1))[:220] if cap else '',
            'date': '',
            'source': 'Bing',
        })
        if len(out) >= k:
            break
    return out


# ---------------------------------------------------------------------------
# HackerNews Algolia 官方 API —— JSON,技术向观点
# ---------------------------------------------------------------------------

def hn(query, proxy, timeout, k):
    url = ('https://hn.algolia.com/api/v1/search?query=' + q(query)
           + '&tags=story&hitsPerPage=%d' % k)
    j = json.loads(get(url, proxy, timeout))
    out = []
    for it in (j.get('hits') or []):
        out.append({
            'title': clean(it.get('title')),
            'url': it.get('url') or ('https://news.ycombinator.com/item?id=%s'
                                     % it.get('objectID')),
            'snippet': '%s 分 · %s 评论' % (it.get('points'), it.get('num_comments')),
            'date': '',
            'source': 'HN',
        })
    return out


# ---------------------------------------------------------------------------
# StackExchange 官方 API —— JSON,中文技术问题覆盖有限,但英文质量高
# ---------------------------------------------------------------------------

def stackoverflow(query, proxy, timeout, k):
    url = ('https://api.stackexchange.com/2.3/search/advanced?order=desc'
           '&sort=relevance&site=stackoverflow&pagesize=%d&q=' % k + q(query))
    j = json.loads(get(url, proxy, timeout))
    out = []
    for it in (j.get('items') or []):
        tags = '/'.join((it.get('tags') or [])[:3])
        out.append({
            'title': clean(it.get('title')),
            'url': it.get('link') or '',
            'snippet': '得分%s%s' % (it.get('score'),
                                   (' · 标签:' + tags) if tags else ''),
            'date': '',
            'source': 'StackOverflow',
        })
    return out


# ---------------------------------------------------------------------------
# GitHub 搜索 API —— JSON,找仓库/轮子
# ---------------------------------------------------------------------------

def github(query, proxy, timeout, k):
    url = ('https://api.github.com/search/repositories?q=' + q(query)
           + '&sort=stars&per_page=%d' % k)
    j = json.loads(get(url, proxy, timeout))
    out = []
    for it in (j.get('items') or []):
        out.append({
            'title': '%s ★%s' % (it.get('full_name'), it.get('stargazers_count')),
            'url': it.get('html_url') or '',
            'snippet': clean(it.get('description'))[:200],
            'date': '',
            'source': 'GitHub',
        })
    return out


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

SOURCES = {
    'google_news': google_news,
    'bing': bing,
    'hn': hn,
    'stackoverflow': stackoverflow,
    'github': github,
}

ALL_SOURCES = list(SOURCES)

# 各模式默认使用的来源(顺序即优先级,结果按此排序)
MODE_SOURCES = {
    # smart 在 engine 里按语言分流,这里的顺序只作为兜底
    1: ['google_news', 'bing', 'hn'],
    # multi 只用官方 JSON/RSS 接口:无改版、无反爬、无品牌霸屏
    2: ['google_news', 'stackoverflow', 'github', 'hn'],
    # bing 单源
    3: ['bing'],
}

__all__ = ['SOURCES', 'ALL_SOURCES', 'MODE_SOURCES', 'filter_spam']
