#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTML 解析工具(纯正则,不依赖 BeautifulSoup)。

站点的 DOM 结构实测于 2026-09-11,关键选择器:
    标题      h1.post-content-title
    面包屑    div.breadcrumb
    楼层容器  li.post-item post-entry   id="post-{pid}"
    作者      a.post-title post-author
    正文      div.post-readability-content > .nb-editor-post-content
    发卡标记  span.virtual-card-title-status
    统计      div.post-content-stats
"""

import re
from datetime import datetime, timedelta, timezone


def html_unescape(s):
    return (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


def extract_div_inner(html, pos):
    """从 pos(指向某个 <div 的起始)开始,按标签配对返回该 div 的内部 HTML。

    正文 div 内部有嵌套(如 <div class=post-readability-content>),非贪婪正则会被
    第一个 </div> 提前截断,所以必须做括号配对。
    """
    if pos < 0:
        return ""
    i = html.find(">", pos)
    if i < 0:
        return ""
    i += 1
    depth = 1
    while depth > 0 and i < len(html):
        no = html.find("<div", i)
        nc = html.find("</div>", i)
        if nc < 0:
            return html[i:]
        if 0 <= no < nc:
            depth += 1
            i = no + 4
        else:
            depth -= 1
            if depth == 0:
                return html[i:nc]
            i = nc + 6
    return html[i:]


def div_to_text(seg):
    """把一段 HTML 里的正文抽成纯文本(保留段落换行)。"""
    seg = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", seg, flags=re.S)
    seg = re.sub(r"<br\s*/?>", "\n", seg, flags=re.I)
    seg = re.sub(r"</(p|div|li|blockquote)>", "\n", seg, flags=re.I)
    return html_unescape(strip_tags(seg)).strip()


def is_fresh(lastmod, max_age_minutes):
    """sitemap 的 lastmod 是否为最近 max_age_minutes 内(UTC)。

    解析不出来时返回 True —— 宁可放行也不要因为格式变化就全都不回。
    """
    if not lastmod:
        return True
    try:
        dt = datetime.strptime(lastmod.strip(), "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, AttributeError):
        return True
    dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt) <= timedelta(minutes=max_age_minutes)


def parse_posts(html):
    """解析帖子页里的全部楼层,返回 [{pid, author, content}, ...]。

    楼层容器是 <li class="post-item post-entry" id="post-{pid}">;
    HTML 里没有 data-floor,楼层顺序即文档顺序。
    """
    marks = [m.start() for m in re.finditer(r'<li[^>]*class="post-item post-entry"', html)]
    if not marks:
        return []
    marks.append(len(html))
    out = []
    for i in range(len(marks) - 1):
        seg = html[marks[i]:marks[i + 1]]
        pid = re.search(r'id="post-(\d+)"', seg)
        au = re.search(r'<a[^>]*class="post-title post-author"[^>]*>(.*?)</a>', seg, re.S)
        text = ""
        pos = seg.find("nb-editor-post-content")
        if pos >= 0:
            start = seg.rfind("<div", 0, pos)
            text = div_to_text(extract_div_inner(seg, start))
        out.append({
            "pid": pid.group(1) if pid else "",
            "author": html_unescape(strip_tags(au.group(1))).strip() if au else "",
            "content": text,
        })
    return out


def extract_csrf(html):
    m = re.search(r'name="_csrf"\s+value="([0-9a-fA-F]+)"', html)
    return m.group(1) if m else None


def solve_arithmetic(question):
    """解析 '4 × 7 = ?' 这类算术题,返回答案字符串。"""
    if not question:
        return None
    s = (str(question).replace("？", "").replace("?", "").replace("=", "")
         .replace("×", "*").replace("÷", "/")
         .replace("−", "-").replace("–", "-").replace("—", "-").strip())
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*([+\-*/])\s*(-?\d+(?:\.\d+)?)$", s)
    if not m:
        return None
    a, op, b = float(m.group(1)), m.group(2), float(m.group(3))
    if op == "/" and b == 0:
        return None
    val = {"+": a + b, "-": a - b, "*": a * b, "/": a / b if b else 0}[op]
    return str(int(val)) if float(val).is_integer() else str(round(val, 4))
