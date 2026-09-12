#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回复前的过滤规则。

顺序有意义:先做最便宜、最确定的判断(本地记录 / 去重),再做需要解析内容的判断。
"""


def should_reply(cfg, topic, store):
    """返回 (是否回复, 原因)。"""
    tid = topic["tid"]

    if store.has_replied(tid):
        return False, "已回过(本地记录)"

    # 【跨端去重】本地 state.db 在手机 / 电脑各有一份、互不同步,
    # 只靠本地记录会在两端都跑时把同一个帖子回复两遍。
    # 这里直接查站点:该帖的楼层里是否已经出现过本 bot 的发言。
    # 这是唯一不依赖本地状态、能真实反映「我是否已回过」的判据。
    bot_name = (cfg.get("bot_username") or "").strip()
    if bot_name:
        authors = {p.get("author") for p in (topic.get("posts") or [])}
        if bot_name in authors:
            return False, "站点上已有本 bot 的回复(跨端去重)"

    if not topic.get("can_reply"):
        return False, "页面显示未登录或不可回复"
    if topic.get("closed"):
        return False, "帖子已关闭/锁定"

    if not (topic.get("content") or "").strip():
        if topic.get("images"):
            return False, "纯图片帖(正文只有截图,没有文字可依)"
        return False, "抓不到正文"

    if cfg.get("skip_gacha", True) and topic.get("is_gacha"):
        return False, "发卡/虚拟卡帖(%s)" % (topic.get("gacha_status") or "无状态")

    fid = topic.get("forum_id")
    deny_forums = cfg.get("deny_forums") or []
    if fid is not None and fid in deny_forums:
        return False, "板块 %s 在黑名单(推广/福利区)" % fid
    allow_forums = cfg.get("allow_forums") or []
    if allow_forums and fid not in allow_forums:
        return False, "板块 %s 不在白名单" % fid

    title = topic.get("title") or ""
    for kw in (cfg.get("deny_keywords") or []):
        if kw and kw in title:
            return False, "标题命中屏蔽词「%s」(发卡/推广类)" % kw

    return True, "可以回复"


def is_retryable_send_error(msg, retryable_keys=None):
    """发帖失败是否属于「网络层瞬时故障」,值得重试。"""
    keys = retryable_keys or ("请求失败", "打开发帖页失败", "返回非 JSON")
    return any(k in (msg or "") for k in keys)
