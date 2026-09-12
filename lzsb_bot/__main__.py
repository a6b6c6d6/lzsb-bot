#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行入口。

    python -m lzsb_bot               正常循环运行
    python -m lzsb_bot --once        只跑一轮就退出(调试用)
    python -m lzsb_bot --dry         强制只读,不提交
    python -m lzsb_bot --login-only  只验证登录能不能成功
    python -m lzsb_bot --search 测试词   只测联网搜索(三种模式都试)
"""

import argparse
import signal
import sys

from .bot import Bot
from .config import (SEARCH_MODE_LABELS, ensure_config, load_config,
                     normalize_search_mode)
from .log import log
from .search import describe_mode, search as web_search
from .site import LsbSite


def _cmd_search(args):
    """只测搜索,不碰站点。用于验证代理/模式是否可用。"""
    mode = normalize_search_mode(args.mode)
    print("搜索模式 %d = %s" % (mode, describe_mode(mode)))
    print("关键词: %s" % args.search)
    print("-" * 60)
    hits = web_search(args.search, topk=args.topk or 5, timeout=15,
                      proxy=args.proxy or "", mode=mode)
    if not hits:
        print("(没有结果 —— 检查代理是否可用,或换个模式试试)")
        return 1
    for i, h in enumerate(hits, 1):
        print("%d. %s" % (i, h.get("title")))
        print("   %s" % h.get("url"))
        if h.get("snippet"):
            print("   %s" % h["snippet"][:120])
        print("   来源: %s" % h.get("source"))
    print("-" * 60)
    print("共 %d 条" % len(hits))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="lzsb_bot", description="linux.sb 自动回帖机器人")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--dry", action="store_true", help="强制只读,不提交")
    ap.add_argument("--login-only", action="store_true", help="只验证登录")
    ap.add_argument("--search", metavar="KEYWORD", help="只测联网搜索")
    ap.add_argument("--mode", metavar="1|2|3", default=None,
                    help="配合 --search:指定搜索模式(1 智能路由 / 2 多源聚合 / 3 Bing)")
    ap.add_argument("--topk", type=int, default=None, help="配合 --search:结果条数")
    ap.add_argument("--proxy", default=None, help="覆盖 config 里的代理")
    ap.add_argument("--config", default=None, help="指定配置文件路径")
    args = ap.parse_args(argv)

    # --search 不需要 config,允许在没有 config.json 时先验证搜索链路
    if args.search:
        return _cmd_search(args)

    if not ensure_config():
        log("没有 config.json 也没有 config.example.json,无法启动", "ERROR")
        return 1

    cfg = load_config(args.config)
    if not cfg:
        return 1
    if args.proxy is not None:
        cfg["proxy"] = args.proxy

    if args.login_only:
        site = LsbSite(cfg)
        if site.login():
            st = site.fetch_forum_topics((cfg.get("scan_forums") or [1])[0], 1)
            log("登录可用;板块页探测到 %d 个帖子" % len(st))
            return 0
        return 3

    bot = Bot(cfg, dry_run=True if args.dry else None, once=args.once)
    signal.signal(signal.SIGINT, bot.stop)
    try:
        signal.signal(signal.SIGTERM, bot.stop)
    except (AttributeError, ValueError):
        pass
    try:
        return bot.run()
    except KeyboardInterrupt:
        bot.stop()
        return 0


if __name__ == "__main__":
    sys.exit(main())
