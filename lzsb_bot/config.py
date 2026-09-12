#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置加载与默认值合并。

config.json 里允许写 `_xxx说明` 这类注释键(JSON 不支持注释),
本模块会统一剔除,不做兼容性报错。

向后兼容:
  - `llm.providers` 是主写法;若只写了单个 base_url/api_key/model,会被
    包装成一个 provider。
  - `enable_search` 与 `search_mode` 是新增的联网开关;缺失时按
    "开启 + 智能路由" 处理(即本项目历史行为)。
"""

import json
import os

from .log import log
from .paths import (CONFIG_PATH, CONFIG_EXAMPLE_PATH,
                    LLM_CONFIG_PATH, LLM_EXAMPLE_PATH)

# 联网搜索模式:与 userscript「水贴」脚本的三个选项一一对应
SEARCH_MODE_SMART = 1   # 智能路由:中英分流,多主源并行,DDG 兜底(默认)
SEARCH_MODE_MULTI = 2   # 官方 API 多源聚合:GN + SO + GitHub + HN
SEARCH_MODE_BING = 3    # Bing 单源直连

SEARCH_MODE_LABELS = {
    SEARCH_MODE_SMART: "智能路由(中英分流·多主源并行)",
    SEARCH_MODE_MULTI: "多源聚合(GoogleNews+SO+GitHub+HN)",
    SEARCH_MODE_BING: "Bing 单源直连",
}

DEFAULTS = {
    "base_url": "https://linux.sb",
    "proxy": "",
    "timeout": 30,
    "user_agent": "",

    "username": "",
    "password": "",
    "cookie": "",
    "bot_username": "",

    "poll_interval": 60,
    "reply_delay_min": 10,
    "reply_delay_max": 30,
    "daily_limit": 20,

    "scan_forums": [1, 3, 4, 5, 6, 9, 10],
    "scan_pages": 1,
    "deny_forums": [2, 8],
    "allow_forums": [],
    "deny_keywords": [],

    "bootstrap_threshold": 20,
    "max_per_tick": 5,
    "max_topic_age_minutes": 1440,

    "skip_gacha": True,
    "context_scope": "op",
    "system_prompt": "",

    # ---- 联网搜索 ----
    "enable_search": True,
    "search_mode": SEARCH_MODE_SMART,
    "search_topk": 5,
    "search_timeout": 12,
    "search_proxy": "",

    "dry_run": True,
}


def _strip_comments(obj):
    """递归剔除 `_开头` 的说明键(JSON 里用来当注释)。"""
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items()
                if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def ensure_config():
    """config.json / llm.json 不存在时从示例复制。返回是否可用。"""
    ok = True
    for path, example, label in ((CONFIG_PATH, CONFIG_EXAMPLE_PATH, "config.json"),
                                 (LLM_CONFIG_PATH, LLM_EXAMPLE_PATH, "llm.json")):
        if os.path.exists(path) or not os.path.exists(example):
            continue
        try:
            with open(example, encoding="utf-8") as f:
                text = f.read()
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            log("已从示例生成 %s,请填好后再运行" % label)
            ok = ok and (label == "llm.json")   # llm.json 缺失不阻塞(config 里可内嵌)
        except OSError as e:
            log("生成 %s 失败: %s" % (label, e), "ERROR")
            ok = False
    return ok


def _read_json(path, label):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError as e:
        log("读取 %s 失败: %s" % (label, e), "ERROR")
    except ValueError as e:
        log("解析 %s 失败(JSON 格式有误): %s" % (label, e), "ERROR")
    return None


def load_llm_config(cfg):
    """载入 AI 接口配置。

    优先级:项目根目录的 llm.json > config.json 里的 llm 段。
    拆成独立文件的好处是可以单独替换接口配置,而不用动主配置。
    """
    llm = None
    if os.path.exists(LLM_CONFIG_PATH):
        raw = _read_json(LLM_CONFIG_PATH, "llm.json")
        if raw:
            llm = _strip_comments(raw)
            log("已载入 llm.json 的接口配置")

    if not llm:
        llm = dict(cfg.get("llm") or {})

    # 单个 base_url 的旧写法兼容成 providers
    if not llm.get("providers") and llm.get("base_url"):
        llm["providers"] = [{
            "name": "default",
            "base_url": llm.get("base_url"),
            "api_key": llm.get("api_key"),
            "model": llm.get("model"),
        }]
    llm.setdefault("providers", [])
    llm.setdefault("temperature", 0.8)
    llm.setdefault("max_tokens", 2400)
    llm.setdefault("timeout", 180)
    llm.setdefault("max_retry", 2)

    # 只保留配置完整的 provider(model 必填,不猜、不列举)
    usable, dropped = [], []
    for p in llm["providers"]:
        if p.get("base_url") and p.get("api_key") and p.get("model"):
            usable.append(p)
        else:
            dropped.append(p.get("name") or p.get("base_url") or "(未命名)")
    if dropped:
        log("以下 AI 接口因配置不完整被忽略(base_url/api_key/model 缺一不可): %s"
            % ", ".join(str(d) for d in dropped), "WARN")
    llm["providers"] = usable

    if llm.get("proxy"):
        cfg["llm_proxy"] = llm["proxy"]
    return llm


def load_config(path=None):
    """读配置,填默认值,做基础校验。返回 dict;不可用时返回 None。"""
    path = path or CONFIG_PATH
    if not os.path.exists(path):
        log("找不到 config.json,请先从 config.example.json 复制一份并填写", "ERROR")
        return None

    raw = _read_json(path, "config.json")
    if raw is None:
        return None

    cfg = _strip_comments(raw)
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)

    if not cfg.get("user_agent"):
        from .paths import DEFAULT_UA
        cfg["user_agent"] = DEFAULT_UA

    # 搜索模式归一化:允许写成 "1"/"smart" 这类宽松形式
    cfg["search_mode"] = normalize_search_mode(cfg.get("search_mode"))

    # AI 接口:独立文件的优先
    cfg["llm"] = load_llm_config(cfg)

    if not cfg.get("bot_username"):
        cfg["bot_username"] = cfg.get("username", "")

    return cfg


_MODE_ALIASES = {
    "smart": SEARCH_MODE_SMART, "智能": SEARCH_MODE_SMART,
    "1": SEARCH_MODE_SMART, 1: SEARCH_MODE_SMART,
    "multi": SEARCH_MODE_MULTI, "聚合": SEARCH_MODE_MULTI,
    "2": SEARCH_MODE_MULTI, 2: SEARCH_MODE_MULTI,
    "bing": SEARCH_MODE_BING,
    "3": SEARCH_MODE_BING, 3: SEARCH_MODE_BING,
}


def normalize_search_mode(value):
    """把各种写法归一成 1/2/3;认不出来就用默认(1 智能路由)。"""
    if value in _MODE_ALIASES:
        return _MODE_ALIASES[value]
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _MODE_ALIASES:
            return _MODE_ALIASES[key]
    return SEARCH_MODE_SMART
