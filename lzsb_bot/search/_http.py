#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""底层 HTTP 与文本清洗:搜索各源共用的部分。"""

import gzip
import re
import urllib.parse
import urllib.request

from ..http import build_opener

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:142.0) '
      'Gecko/20100101 Firefox/142.0')

DEFAULT_HEADERS = {
    'User-Agent': UA,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip',
    'Connection': 'close',
}


def get(url, proxy='', timeout=12, headers=None):
    """GET 一个 URL,返回解码后的文本。失败会抛异常(由调用方兜住)。"""
    opener = build_opener(proxy, ua=UA)
    h = dict(DEFAULT_HEADERS)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    resp = opener.open(req, timeout=timeout)
    raw = resp.read()
    if resp.headers.get('Content-Encoding') == 'gzip':
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    return raw.decode('utf-8', 'replace')


def q(s):
    """URL 编码。"""
    return urllib.parse.quote(s or '')


def clean(s):
    """去标签 + 压空白,用于把 HTML 摘要变成一行纯文本。"""
    s = re.sub(r'<[^>]+>', '', s or '')
    return re.sub(r'\s+', ' ', s).strip()


def unwrap_bing_url(href):
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


# 博彩/引流垃圾的特征词。必须在截 topK 之前过滤,
# 否则垃圾会先占满名额,把真正有用的结果挤掉。
_SPAM_RE = re.compile(
    r'(百家乐|澳门|博彩|赌场|娱乐城|真人视讯|彩票|棋牌|AG旗舰|捕鱼|棋牌游戏|'
    r'开户|投注|老虎机|色情|av片|成人|菠菜|代充|充值优惠|日赚|月入过万|'
    r'加微信|加V|QQ群|点击领取|限时秒杀|优惠券|淘宝优惠|best casino|'
    r'online casino|slot|betting|porn|escort)',
    re.I)


def is_spam(title, url='', site=''):
    """粗略判定结果是否为垃圾(博彩/引流/成人)。"""
    blob = ' '.join([title or '', url or '', site or ''])
    return bool(_SPAM_RE.search(blob))


def filter_spam(items):
    out = []
    for it in items or []:
        if is_spam(it.get('title'), it.get('url'), it.get('site')):
            continue
        out.append(it)
    return out


def has_cjk(text):
    """含中日韩表意字符 → 视为中文查询。"""
    return bool(re.search(r'[\u3400-\u9fff]', text or ''))
