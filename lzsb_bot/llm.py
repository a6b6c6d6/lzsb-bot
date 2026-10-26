#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenAI 兼容接口调用,支持多 provider 自动故障切换。

故障切换的意义:中转站限速 / 上游 5xx / 模型返回空内容都是常态,
配两个 provider 互为备份,一个挂了自动切下一个,回帖不会因为单点故障中断。
"""

import gzip
import ipaddress
import json
import re
import time
import urllib.error
import urllib.request

from .http import build_opener
from .log import log
from .paths import DEFAULT_UA


# 本机/局域网目标:代理节点根本不可达(节点上没有你的回环地址,
# 也摸不到你的内网),必须直连。覆盖回环、RFC1918 私网、link-local,
# 以及 .local/.lan/.internal 这类内网主机名。
_LOCAL_HOSTNAMES = ("localhost",)
_LOCAL_SUFFIXES = (".local", ".lan", ".internal")
_LOCAL_BYPASS_LOGGED = set()


def _is_local_target(base):
    """base_url 是否指向本机/局域网地址(本机部署的反代/模型服务、
    内网其他机器上的服务等)。

    这类地址走代理必失联:请求被转给代理节点,而节点上既没有你的
    回环地址、也路由不到你的内网,连接直接挂到超时(实测:同一请求
    直连约 1 秒返回,经代理则直到超时也无响应)。
    """
    b = (base or "").strip().lower()
    if "://" not in b:
        return False
    host = b.split("://", 1)[1].split("/", 1)[0]      # 去路径
    if "@" in host:                                    # 去 userinfo
        host = host.rsplit("@", 1)[1]
    if host.startswith("["):                           # IPv6 [::1]:端口
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:                         # 主机:端口
        host = host.rsplit(":", 1)[0]
    if not host:
        return False
    if host in _LOCAL_HOSTNAMES or host.endswith(_LOCAL_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False                                   # 域名:无法静态判断,按外部处理
    return (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_reserved or ip.is_unspecified)


def _chat_once(cfg, provider, system_prompt, user_prompt, opener, strict_tail=True):
    """对单个 provider 发起一次请求;成功返回文本,失败抛异常。"""
    llm = cfg.get("llm") or {}
    base = (provider.get("base_url") or "").rstrip("/")
    key = provider.get("api_key") or ""
    model = provider.get("model") or ""
    if not (base and key and model):
        raise ValueError("provider 配置不完整(base_url/api_key/model)")

    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": llm.get("temperature", 0.8),
        "max_tokens": llm.get("max_tokens", 2400),
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
            # 不少中转站前面挂了 Cloudflare;不设 UA 会发 Python-urllib/x.y,
            # 被 Error 1010(browser signature)直接拦掉
            "User-Agent": cfg.get("user_agent") or DEFAULT_UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "close",
        })
    resp = opener.open(req, timeout=llm.get("timeout", 180))
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    j = json.loads(raw.decode("utf-8", "replace"))
    choice = (j.get("choices") or [{}])[0]
    text = choice.get("message", {}).get("content", "") or ""
    # 推理模型有时会把思考过程塞在 <thinking> 里,去掉
    text = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text,
                  flags=re.S | re.I).strip()
    if not text:
        raise ValueError("模型返回空内容")

    # 截断检测:finish_reason=length 说明 token 用尽,句子会被切断在半句上
    if choice.get("finish_reason") == "length":
        log("  ⚠ 输出被 max_tokens 截断!请调大 config.json 的 llm.max_tokens", "WARN")
    elif strict_tail and not text.rstrip().endswith(
            ("。", "！", "？", "!", ".", "…", "~", "”", '"')):
        # 只在生成回帖时检查句尾 —— 提炼关键词那种任务输出本来就不带句号
        log("  ⚠ 回复结尾疑似不完整:%s" % text[-24:], "WARN")
    return text


def call_llm(cfg, system_prompt, user_prompt, strict_tail=True):
    """依次尝试 llm.providers 里的每个接口,全部失败才返回错误。"""
    llm = cfg.get("llm") or {}
    providers = [dict(p) for p in (llm.get("providers") or [])]
    providers = [p for p in providers
                 if p.get("base_url") and p.get("api_key") and p.get("model")]
    if not providers:
        return None, ("config.json 的 llm 配置不完整"
                      "(providers / base_url+api_key+model 均为空)")

    proxy = cfg.get("llm_proxy") or cfg.get("proxy") or ""
    ua = cfg.get("user_agent") or DEFAULT_UA
    opener = build_opener(proxy, ua=ua)
    # 本机/局域网 provider 专用直连 opener,见 _is_local_target 注释
    direct_opener = build_opener("", ua=ua)

    retry = max(1, int(llm.get("max_retry", 2) or 1))
    errs = []
    for p in providers:
        name = p.get("name") or p.get("model")
        base = (p.get("base_url") or "").rstrip("/")
        if _is_local_target(base):
            if name not in _LOCAL_BYPASS_LOGGED:
                _LOCAL_BYPASS_LOGGED.add(name)
                log("  「%s」指向本机/局域网(%s),强制直连、不走代理"
                    % (name, base), "INFO")
            p_opener = direct_opener
        else:
            p_opener = opener
        for attempt in range(1, retry + 1):
            try:
                return _chat_once(cfg, p, system_prompt, user_prompt, p_opener,
                                  strict_tail=strict_tail), None
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = (e.read().decode("utf-8", "replace")[:160]
                              .replace("\n", " "))
                except Exception:
                    pass
                errs.append("%s HTTP%s %s" % (name, e.code, detail))
            except Exception as e:
                errs.append("%s %s: %s" % (name, type(e).__name__, str(e)[:80]))
            if attempt < retry:
                time.sleep(1.5 * attempt)
        log("  AI 接口「%s」不可用,自动切换到下一个" % name, "WARN")

    return None, "所有 AI 接口均失败 -> " + " | ".join(errs[-4:])


def extract_keywords(cfg, topic, system_prompt, max_queries=2):
    """让模型从帖子里提炼搜索关键词。

    直接拿标题去搜效果很差(Bing 会把长句切出无意义单字),所以先提炼再搜。
    提炼失败则返回空列表,由调用方回退到标题。
    """
    title = topic.get("title") or ""
    ops = topic.get("op_posts") or []
    head = "\n".join((p.get("content") or "")[:400] for p in ops[:3])
    user = "【标题】%s\n【楼主发言节选】\n%s" % (title, head[:1000])

    out, err = call_llm(cfg, system_prompt, user, strict_tail=False)
    if err or not out:
        return []
    qs = []
    for line in out.splitlines():
        s = line.strip().lstrip("0123456789.、-—•*# ").strip()
        s = s.strip('"\'《》「」')
        if 2 <= len(s) <= 40:
            qs.append(s)
    return qs[:max_queries]
