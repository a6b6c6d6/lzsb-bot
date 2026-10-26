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
import time

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


def _cmd_gacha(args, cfg):
    """抽卡模式:连续抽若干次后退出。"""
    site = LsbSite(cfg)
    if not site.login():
        log("登录失败,无法抽卡", "ERROR")
        return 3

    if args.gacha is not None:
        endpoint, times = "/gacha_pull", args.gacha
    elif args.gacha10 is not None:
        endpoint, times = "/gacha_pull_10", args.gacha10
    else:
        endpoint, times = "/gacha_pull_100", args.gacha100

    per, cost, label = site.GACHA_ENDPOINTS[endpoint]
    total_pulls, total_cost = times * per, times * cost
    order = {"N": 0, "R": 1, "SR": 2, "SSR": 3, "UR": 4}

    points = site.get_gacha_points()
    log("当前积分: %s" % ("未知" if points is None else points))
    log("抽卡计划: %s × %d 次 = 共 %d 抽,预计消耗 %d 积分"
        % (label, times, total_pulls, total_cost))

    if args.gacha_dry:
        log("(--gacha-dry)只显示计划,不实际抽卡")
        return 0
    if times <= 0:
        return 0
    if points is not None and points < cost:
        log("积分不足(%d < %d),中止" % (points, cost), "ERROR")
        return 4

    delay = max(0.0, float(args.gacha_delay or 0))
    stats, by_tier, by_title = {"ok": 0, "empty": 0, "cost": 0}, {}, {}
    t0 = time.time()

    done, fail = 0, 0
    while done < times:
        if points is not None and points < cost:
            log("剩余积分 %d 不足一次%s(%d),提前停止" % (points, label, cost))
            break
        r = site.gacha_pull_once(endpoint)
        if not r.get("ok"):
            fail += 1
            log("第 %d 抽失败(不计数,累计 %d 次失败): %s"
                % (done + 1, fail, r.get("message")), "ERROR")
            if "会话" in (r.get("message") or ""):
                break
            if fail >= 20:
                log("连续失败过多,中止", "ERROR")
                break
            time.sleep(max(1.5, delay))
            continue

        done += 1
        i = done

        stats["ok"] += 1
        stats["cost"] += cost
        if points is not None:
            points -= cost
        if r.get("empty"):
            stats["empty"] += 1
            body = "%-4s %s" % ("空手", "什么都没抽到")
        else:
            by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + 1
            if r.get("title_id"):
                by_title[r["title_id"]] = by_title.get(r["title_id"], 0) + 1
            body = "%-4s %-8s ×%-2d %s" % (
                r["tier"], r["name"], r.get("quantity", 1),
                "NEW" if r.get("is_new") else "重复")
        log("  #%-3d %s │ 余 %s" % (i, body,
                                    points if points is not None else "?"))

        if done < times:
            time.sleep(delay)

    log("-" * 60)
    log("完成 %d 次(失败重试 %d 次),消耗 %d 积分%s" % (
        stats["ok"], fail, stats["cost"],
        "" if points is None else ",剩余 %d" % points))
    if stats["ok"]:
        log("空手 %d 次(%.0f%%)" % (
            stats["empty"], stats["empty"] / stats["ok"] * 100))
    if by_tier:
        log("命中分布: " + "  ".join(
            "%s×%d" % (k, by_tier[k])
            for k in sorted(by_tier, key=lambda x: order.get(x, 9))))
    if by_title:
        parts = []
        for tid in sorted(by_title, key=lambda t: (
                order.get(site.GACHA_TITLES.get(t, ("--",))[0], 9), t)):
            tier, name = site.GACHA_TITLES.get(tid, ("??", str(tid)))
            parts.append("%s %s×%d" % (tier, name, by_title[tid]))
        log("掉落明细: " + "  ".join(parts))
    log("耗时 %.1fs" % (time.time() - t0))
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
    ap.add_argument("--dm", nargs=2, metavar=("UID", "内容"),
                    help="给指定 uid 发一条私信后退出(最长 500 字)")
    ap.add_argument("--gacha", type=int, metavar="N",
                    help="抽卡:连续单抽 N 次(每次 10 积分)后退出")
    ap.add_argument("--gacha10", type=int, metavar="N",
                    help="抽卡:连续十连 N 次(每次 90 积分 = 10 抽)后退出")
    ap.add_argument("--gacha100", type=int, metavar="N",
                    help="抽卡:连续百连 N 次(每次 800 积分 = 100 抽)后退出")
    ap.add_argument("--gacha-delay", type=float, default=0.6,
                    help="抽卡每次之间的间隔秒数(默认 0.6)")
    ap.add_argument("--gacha-dry", action="store_true",
                    help="抽卡:只看积分和计划,不真的抽")
    ap.add_argument("--donate-only", action="store_true",
                    help="只打赏模式:不回复,AI 评分后按分打赏积分为帖子")
    args = ap.parse_args(argv)

    # --search 不需要 config,允许在没有 config.json 时先验证搜索链路
    if args.search:
        return _cmd_search(args)

    # --dm:发一条私信就退出
    if args.dm:
        if not ensure_config():
            return 1
        cfg = load_config(args.config)
        if not cfg:
            return 1
        site = LsbSite(cfg)
        if not site.login():
            log("登录失败,无法发私信", "ERROR")
            return 3
        ok, msg = site.send_dm(int(args.dm[0]), args.dm[1])
        log("%s (发给 uid=%s:%s)" % (msg, args.dm[0],
                                     (args.dm[1] or "")[:40]))
        return 0 if ok else 4

    if not ensure_config():
        log("没有 config.json 也没有 config.example.json,无法启动", "ERROR")
        return 1

    cfg = load_config(args.config)
    if not cfg:
        return 1
    if args.proxy is not None:
        cfg["proxy"] = args.proxy

    if (args.gacha is not None or args.gacha10 is not None
            or args.gacha100 is not None):
        return _cmd_gacha(args, cfg)

    if args.login_only:
        site = LsbSite(cfg)
        if site.login():
            st = site.fetch_forum_topics((cfg.get("scan_forums") or [1])[0], 1)
            log("登录可用;板块页探测到 %d 个帖子" % len(st))
            return 0
        return 3

    bot = Bot(cfg, dry_run=True if args.dry else None, once=args.once,
              donate_only=args.donate_only)
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
