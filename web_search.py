#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
联网搜索(免 API Key)
=====================

移植自 lzsb-smart-water-sticker 的「客户端直搜」思路,改用 Python 标准库实现。

选取的来源(按对脚本友好的程度排序):
  google_news  Google News RSS —— 标准 RSS,专供程序消费,中英通吃,最稳
  bing         www.bing.com/search —— 网页版,解析 li.b_algo;需解 ck/a 跳转壳
  hn           HackerNews Algolia 官方 API —— JSON,技术向观点
  github       GitHub 搜索 API —— JSON,找仓库/轮子

特点:多源并行、单源独立超时、任一源失败不影响整体、按 URL 去重。
注意:Google News / HN / GitHub 在部分网络下需走代理,统一由 proxy 参数控制。
"""

import gzip
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:142.0) '
      'Gecko/20100101 Firefox/142.0')


def _get(url, proxy='', timeout=12):
    handlers = [urllib.request.ProxyHandler(
        {'http': proxy, 'https': proxy} if proxy else {})]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip',
        'Connection': 'close',
    })
    resp = opener.open(req, timeout=timeout)
    raw = resp.read()
    if resp.headers.get('Content-Encoding') == 'gzip':
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    return raw.decode('utf-8', 'replace')


def _clean(s):
    s = re.sub(r'<[^>]+>', '', s or '')
    return re.sub(r'\s+', ' ', s).strip()


# ---------------------------------------------------------------------------
# 各来源
# ---------------------------------------------------------------------------

def _google_news(q, proxy, timeout, k):
    url = ('https://news.google.com/rss/search?q=' + urllib.parse.quote(q)
           + '&hl=zh-CN&gl=CN&ceid=CN:zh-Hans')
    root = ET.fromstring(_get(url, proxy, timeout))
    out = []
    for it in root.iter('item'):
        title = _clean(it.findtext('title'))
        if not title:
            continue
        out.append({
            'title': title,
            'url': (it.findtext('link') or '').strip(),
            'snippet': _clean(it.findtext('description'))[:220],
            'date': (it.findtext('pubDate') or '').strip()[:31],
            'source': 'GoogleNews',
        })
        if len(out) >= k:
            break
    return out


def _unwrap_bing_url(href):
    """Bing 结果链接常是 https://www.bing.com/ck/a?...&u=a1<base64url> 跳转壳,需解出真实地址。"""
    if 'bing.com/ck/a' not in href:
        return href
    m = re.search(r'[?&]u=a1([^&]+)', href)
    if not m:
        return href
    raw = m.group(1)
    pad = '=' * (-len(raw) % 4)
    try:
        import base64
        return base64.urlsafe_b64decode(raw + pad).decode('utf-8', 'replace')
    except Exception:
        return href


def _bing(q, proxy, timeout, k):
    url = ('https://www.bing.com/search?q=' + urllib.parse.quote(q)
           + '&setlang=zh-CN&ensearch=0&count=25')
    html = _get(url, proxy, timeout)
    out = []
    for m in re.finditer(r'<li class="b_algo".*?(?=<li class="b_algo"|</ol>)', html, re.S):
        blk = m.group(0)
        a = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
        if not a:
            continue
        title = _clean(a.group(2))
        if not title:
            continue
        cap = re.search(r'<p[^>]*>(.*?)</p>', blk, re.S)
        out.append({
            'title': title,
            'url': _unwrap_bing_url(a.group(1)),
            'snippet': _clean(cap.group(1))[:220] if cap else '',
            'date': '',
            'source': 'Bing',
        })
        if len(out) >= k:
            break
    return out


def _hn(q, proxy, timeout, k):
    url = ('https://hn.algolia.com/api/v1/search?query=' + urllib.parse.quote(q)
           + '&tags=story&hitsPerPage=%d' % k)
    j = json.loads(_get(url, proxy, timeout))
    out = []
    for it in (j.get('hits') or []):
        out.append({
            'title': _clean(it.get('title')),
            'url': it.get('url') or ('https://news.ycombinator.com/item?id=%s'
                                     % it.get('objectID')),
            'snippet': '%s 分 · %s 评论' % (it.get('points'), it.get('num_comments')),
            'date': '',
            'source': 'HN',
        })
    return out


def _github(q, proxy, timeout, k):
    url = ('https://api.github.com/search/repositories?q=' + urllib.parse.quote(q)
           + '&sort=stars&per_page=%d' % k)
    j = json.loads(_get(url, proxy, timeout))
    out = []
    for it in (j.get('items') or []):
        out.append({
            'title': '%s ★%s' % (it.get('full_name'), it.get('stargazers_count')),
            'url': it.get('html_url') or '',
            'snippet': _clean(it.get('description'))[:200],
            'date': '',
            'source': 'GitHub',
        })
    return out


SOURCES = {
    'google_news': _google_news,
    'bing': _bing,
    'hn': _hn,
    'github': _github,
}
DEFAULT_SOURCES = ['google_news', 'bing', 'hn']


# ---------------------------------------------------------------------------

def search(query, topk=5, timeout=12, proxy='', sources=None):
    """并行搜索多个来源,返回 [{title,url,snippet,date,source}, ...](已去重)。

    任一时间点所有来源都失败时返回空列表 —— 调用方应把"搜不到"当成正常情况。
    """
    q = (query or '').strip()
    if not q:
        return []
    names = [s for s in (sources or DEFAULT_SOURCES) if s in SOURCES]
    if not names:
        return []

    results = []
    with ThreadPoolExecutor(max_workers=len(names)) as ex:
        futs = {ex.submit(SOURCES[n], q, proxy, timeout, topk): n for n in names}
        try:
            for f in as_completed(futs, timeout=timeout + 8):
                try:
                    results.extend(f.result() or [])
                except Exception:
                    pass          # 单源失败静默:搜索引擎经常限流/改版
        except Exception:
            pass                  # 整体超时:已拿到的照常用

    seen, out = set(), []
    for r in results:
        u = (r.get('url') or '').strip()
        key = u or (r.get('title') or '')
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out[:topk]


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
