#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 客户端:强制代理 + Cookie 管理 + 网络层自动重试。

为什么必须带重试:本项目部署环境里代理抖动是常态(Clash 换节点、机场闪断),
一次 TCP 失败就会漏掉一个帖子或把发帖结果丢掉,代价太大。
"""

import gzip
import http.cookiejar
import time
import urllib.error
import urllib.parse
import urllib.request

from .log import log
from .paths import DEFAULT_UA


class HttpClient:
    """带 Cookie 管理与强制代理的极简 HTTP 客户端。"""

    def __init__(self, proxy="", timeout=30, ua=DEFAULT_UA, retry=3):
        self.timeout = timeout
        self.ua = ua or DEFAULT_UA
        self.retry = max(0, int(retry))
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            # 显式清空:避免环境变量里的 http_proxy 把请求导到不可用的出口
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)
        try:
            self.opener.addheaders = [("User-Agent", self.ua)]
        except Exception:
            pass

    def request(self, method, url, data=None, extra_headers=None, referer=None):
        """返回 (status_code, text, final_url)。

        status_code 为 0 表示网络层彻底失败(此时 text 是错误说明)。
        """
        headers = {
            "User-Agent": self.ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
        }
        if referer:
            headers["Referer"] = referer
        if extra_headers:
            headers.update(extra_headers)

        body = None
        if data is not None:
            body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        last_err = ""
        for attempt in range(self.retry + 1):
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                resp = self.opener.open(req, timeout=self.timeout)
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    try:
                        raw = gzip.decompress(raw)
                    except OSError:
                        pass
                return resp.getcode(), raw.decode("utf-8", "replace"), resp.geturl()
            except urllib.error.HTTPError as e:
                raw = e.read()
                if e.headers.get("Content-Encoding") == "gzip":
                    try:
                        raw = gzip.decompress(raw)
                    except OSError:
                        pass
                text = raw.decode("utf-8", "replace")
                if e.code >= 500 and attempt < self.retry:      # 只有 5xx 值得重试
                    last_err = "HTTP %s" % e.code
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return e.code, text, url
            except Exception as e:
                last_err = "%s: %s" % (type(e).__name__, e)
                if attempt < self.retry:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
        return 0, "", last_err

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, data, **kw):
        return self.request("POST", url, data=data, **kw)

    def get_cookie(self, name):
        for c in self.jar:
            if c.name == name:
                return c.value
        return None


def build_opener(proxy="", ua=DEFAULT_UA):
    """给不走 Cookie 的场景(如 LLM 调用)用的裸 opener。"""
    handlers = [urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {})]
    return urllib.request.build_opener(*handlers)
