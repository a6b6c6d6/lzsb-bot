#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主循环编排。

一轮 tick 的顺序:
    1. 心跳(单实例锁续期)
    2. 从各板块列表页收集候选帖(实时,不用 sitemap)
    3. 找出没见过的 → 安全阀判断 → 单轮限量
    4. 逐个:抓帖子页 → 过滤 → 联网搜索 → 生成回复 → 随机等待 → 提交
    5. 记录结果(成功写 ok;网络类失败撤销「已见」留待下轮重试)

两道安全阀是本项目最重要的健壮性设计,详见 tick() 注释。
"""

import random
import re
import time
from datetime import datetime

from .config import SEARCH_MODE_LABELS
from .filters import should_reply
from .log import log
from .llm import call_llm, extract_keywords
from .prompt import (DEFAULT_SYSTEM_PROMPT, NOTIFY_SYSTEM, PLAN_SYSTEM,
                     build_notify_user_prompt, build_user_prompt)
from .site import LsbSite
from .store import Store
from .search import search as web_search
from .search import format_block, build_query, describe_mode


class Bot:
    def __init__(self, cfg, dry_run=None, once=False):
        self.cfg = cfg
        self.once = once
        self.dry = cfg.get("dry_run", True) if dry_run is None else dry_run
        self.store = Store()
        self.site = LsbSite(cfg)
        self.running = True
        self.fail_count = {}          # tid -> 已失败次数(仅内存,重启清零)
        self.notif_fail = {}          # rid -> 通知对应帖子打不开的次数(仅内存)
        self.notif_llm_fail = {}      # rid -> LLM 连续失败次数(仅内存)
        self.notif_submit_fail = {}   # rid -> 提交时会话失效的次数(仅内存)
        self.last_relogin = 0.0       # 上次强制重登的时间戳(10 分钟内最多一次)
        self.system_prompt = cfg.get("system_prompt") or DEFAULT_SYSTEM_PROMPT

    # ------------------------------------------------------------------

    def stop(self, *_):
        self.running = False
        log("收到停止信号,正在退出……", "WARN")

    def _maybe_relogin(self, reason):
        """会话失效的兜底。

        check_login() 只验证 cookie 存在;cookie 被服务端过期/踢掉时察觉不到,
        bot 会进入「假运行」——每轮都在但什么都发不出去。靠发帖/接话时的
        「未登录」症状在这里强制重登,10 分钟内最多一次,避免触发登录风控。
        """
        if time.time() - self.last_relogin < 600:
            return False
        self.last_relogin = time.time()
        log("  检测到会话失效(%s),尝试重新登录……" % reason, "WARN")
        try:
            return self.site.login()
        except Exception as e:
            log("  重新登录失败: %s" % e, "ERROR")
            return False

    def sleep(self, seconds):
        """可被 Ctrl+C 提前打断的睡眠。"""
        for _ in range(int(seconds)):
            if not self.running:
                return
            time.sleep(1)

    # ------------------------------------------------------------------
    # 首次 bootstrap
    # ------------------------------------------------------------------

    def bootstrap(self):
        """首次运行:把存量帖子标记为已读,避免一上线就爆量回复。

        用「一次查全量 + 单事务批量写」,否则上万条逐条 commit 会卡好几分钟
        (早期逐条 commit 时进程被杀,导致 7586 个历史帖被当新帖,差点挖坟)。
        """
        sm = self.site.fetch_sitemap()
        if not sm:
            return False
        log("sitemap 共 %d 条帖子" % len(sm))
        known = self.store.all_seen_ids()
        now = datetime.now().isoformat(timespec="seconds")
        rows = [(tid, lm, now, "") for tid, lm in sm.items() if tid not in known]
        if rows:
            self.store.mark_seen_many(rows)
        log("已将 %d 条存量帖子标记为已见(不会回复它们)" % len(rows))
        return True

    # ------------------------------------------------------------------
    # 收集候选
    # ------------------------------------------------------------------

    def _collect_candidates(self):
        """从各板块列表页收集候选帖 id(去重,保持板块顺序)。"""
        forums = self.cfg.get("scan_forums") or [1, 3, 4, 5, 6, 9, 10]
        deny = set(self.cfg.get("deny_forums") or [])
        pages = int(self.cfg.get("scan_pages", 1) or 1)

        candidates, seen_now = [], set()
        for fid in forums:
            if fid in deny:
                continue
            for tid in self.site.fetch_forum_topics(fid, pages):
                if tid not in seen_now:
                    seen_now.add(tid)
                    candidates.append(tid)
        return candidates

    # ------------------------------------------------------------------
    # 联网搜索
    # ------------------------------------------------------------------

    def _search_block(self, topic):
        """给帖子做联网搜索,返回格式化好的提示词段落(失败返回空串)。"""
        if not self.cfg.get("enable_search", True):
            return ""
        mode = self.cfg.get("search_mode", 1)
        topk = self.cfg.get("search_topk", 5)
        timeout = self.cfg.get("search_timeout", 12)
        proxy = self.cfg.get("search_proxy") or self.cfg.get("proxy") or ""
        try:
            queries = extract_keywords(self.cfg, topic, PLAN_SYSTEM, max_queries=2)
            if not queries:
                queries = [build_query(topic)]
            hits, seen = [], set()
            for qq in queries:
                got = web_search(qq, topk=topk, timeout=timeout,
                                 proxy=proxy, mode=mode)
                log("  联网搜索[模式%d]「%s」→ %d 条" % (mode, qq, len(got)))
                for h in got:
                    key = (h.get("url") or h.get("title") or "").strip()
                    if key and key not in seen:
                        seen.add(key)
                        hits.append(h)
            return format_block(hits[:topk])
        except Exception as e:
            log("  联网搜索失败(忽略,继续生成): %s" % e, "WARN")
            return ""

    def _donate_after_reply(self, topic):
        """回帖成功后给楼主打赏(点赞 + 积分)。

        只尽力而为:打赏失败只告警,不影响回帖结果,也不重试 ——
        重试有双倍打赏的风险,宁可少打不能多打。
        """
        if not self.cfg.get("donate_enabled", True):
            return
        amount = int(self.cfg.get("donate_amount", 1) or 0)
        if amount <= 0:
            return
        # 不给自己打赏(理论上 bot 不会回自己的帖,兜个底)
        if (topic.get("op_name") or "") == (self.cfg.get("bot_username") or ""):
            return
        try:
            ok, msg = self.site.donate_topic(topic["tid"], amount)
            if ok:
                log("  已打赏 #%d 楼主 %d 积分: %s"
                    % (topic["tid"], amount, msg))
            else:
                log("  打赏 #%d 未成功: %s (不重试,避免双倍)"
                    % (topic["tid"], msg), "WARN")
        except Exception as e:
            log("  打赏 #%d 异常: %s" % (topic["tid"], e), "WARN")

    # ------------------------------------------------------------------
    # 跨端同步(个人主页回复)
    # ------------------------------------------------------------------

    def _sync_replies_from_profile(self):
        """从个人主页「回复」标签页同步站点上的已有回复到本地库。

        解决跨端问题:手机和电脑各有一份 state.db 互不同步。新设备启动时
        本地没有历史回复记录,靠它把站点上的真实回复拉下来标记好,
        一开机就和另一端对齐(页面对是公开的,不依赖登录态)。
        与 filters 里的「楼层作者检查」互为双保险:后者是逐帖精确判断,
        这里是启动时批量对账。
        """
        pages = int(self.cfg.get("sync_replies_pages", 2) or 0)
        if pages <= 0:
            return
        uid = self.site.get_own_uid()
        if not uid:
            log("跨端同步跳过:无法确定自己的 uid", "WARN")
            return
        try:
            items = self.site.fetch_user_replies(uid, pages)
        except Exception as e:
            log("跨端同步失败(忽略): %s" % e, "WARN")
            return
        if not items:
            log("跨端同步:站点上没有拉到回复(新号或页面结构变化)")
            return
        new_marked = 0
        for tid, _rid, title, _ex in items:
            if not self.store.has_replied(tid):
                # title/正文留空:同步记录只用于去重判断,不参与其他逻辑
                self.store.save_reply(tid, title, "", "ok(站点同步)")
                new_marked += 1
            self.store.mark_seen(tid, "", title)
        log("跨端同步:站点最近 %d 条回复已对账(本地新增标记 %d 条)"
            % (len(items), new_marked))

    # ------------------------------------------------------------------
    # 通知接话(水他模式:有人回复/提及 bot 就去接话)
    # ------------------------------------------------------------------

    @staticmethod
    def _drop_question_sentences(text):
        """兜底防线:删掉含问号的短语。

        弱模型经常无视"不要问号"的指令;站点氛围下回帖带问号容易显得挑事。
        策略:把「上一句终点(。！…~”)之后一直到 ？(含)」的片段整段删掉,
        只伤发问的那半句,保住其余内容;删完太短才保底返回原文。
        """
        if "？" not in text and "?" not in text:
            return text
        out = re.sub(r"[^。！…~”]*[？?]\s*", "", text).strip()
        return out if len(out) >= 10 else text

    def _handle_notifications(self):
        """处理「我的通知」:有人回复/提及 bot 时,针对对方的话生成一条
        接话回复(等同水贴脚本的「水他」)。

        去重键 = 触发通知的回复 id(rid),处理过就永不再碰;
        只处理 提及/回复 两类,其余类型(系统通知等)标记跳过。
        回复正文自动加 @对方,让对方收到通知,形成正常对话。
        """
        if not self.cfg.get("notify_reply_enabled", True):
            return
        maxn = int(self.cfg.get("notify_max_per_tick", 2) or 0)
        if maxn <= 0:
            return
        if not self.dry and self.store.today_count() >= self.cfg.get("daily_limit", 20):
            return

        try:
            items = self.site.fetch_notifications()
        except Exception as e:
            log("拉取通知失败(忽略): %s" % e, "WARN")
            return
        fresh = [n for n in items if not self.store.has_notif(n["rid"])]
        if not fresh:
            return
        log("通知: %d 条新(提及/回复)" % len(fresh))

        bot_name = self.cfg.get("bot_username") or ""
        handled = 0
        for n in fresh:
            if not self.running:
                return
            rid, tid, from_user, kind = n["rid"], n["tid"], n["from_user"], n["kind"]

            if kind not in ("提及", "回复"):
                self.store.save_notif(rid, tid, from_user, kind, "skip:类型")
                continue
            if from_user == bot_name:
                self.store.save_notif(rid, tid, from_user, kind, "skip:自己")
                continue
            if handled >= maxn:
                continue                      # 留给下一轮,不标记
            if not self.dry and self.store.today_count() >= self.cfg.get("daily_limit", 20):
                return

            try:
                if self._handle_one_notification(n):
                    handled += 1              # 只有真的尝试接话才占名额
            except Exception as e:
                self.store.save_notif(rid, tid, from_user, kind, "fail:%s" % e)
                log("  通知处理异常 #%s: %s" % (rid, e), "ERROR")

    def _handle_one_notification(self, n):
        """处理单条通知。返回 True = 真的尝试了接话;False = 跳过(不占名额)。"""
        rid, tid, from_user = n["rid"], n["tid"], n["from_user"]
        topic = self.site.fetch_topic(tid)
        if not topic:
            # 多半是代理抖动:不消费,重试 3 次后才放弃,防止坏帖占死队列
            n_fail = self.notif_fail.get(rid, 0) + 1
            self.notif_fail[rid] = n_fail
            if n_fail >= 3:
                self.store.save_notif(rid, tid, from_user, n["kind"], "skip:帖子打不开x3")
                self.notif_fail.pop(rid, None)
                log("  通知接话放弃 #%d:连续 3 次打不开" % tid, "WARN")
            else:
                log("  通知接话: #%d 打不开(第 %d 次),下轮重试" % (tid, n_fail), "WARN")
            return False
        self.notif_fail.pop(rid, None)
        if topic.get("closed"):
            self.store.save_notif(rid, tid, from_user, n["kind"], "skip:已锁帖")
            return False

        # 楼层定位:接话必须用「@用户 #对方所在楼层」才能盖在对方的楼里,
        # 只 @ 用户名会变成一条独立新评论。楼层号是**对方那条回复**所在的楼
        # (站点约定:回复从 #1 起,不含楼主;通知摘要里的 #N 是对方回复的
        # 目标楼层,不能拿来用!),定位不到就退化为纯 @ 提及,宁可不盖楼
        # 也不能盖错楼回错人。
        floor, their_text_full = self.site.locate_floor(tid, rid)
        their_text = their_text_full or n["text"] or ""
        if floor is None:
            log("  通知接话: 未能定位 %s 的回复楼层(审核隐藏/已删除),改用纯 @ 提及"
                % from_user, "WARN")
        mention = "@%s" % from_user + (" #%d" % floor if floor else "")

        text, err = call_llm(
            self.cfg, NOTIFY_SYSTEM,
            build_notify_user_prompt(topic, from_user, their_text, n["text"]))
        if err or not text:
            # LLM 临时故障(限速/无渠道)不该消费掉通知:不标记,留待下轮重试;
            # 连续 3 次都失败才标记放弃,防止接口长期宕机时空转
            n_fail = self.notif_llm_fail.get(rid, 0) + 1
            self.notif_llm_fail[rid] = n_fail
            if n_fail >= 3:
                self.store.save_notif(rid, tid, from_user, n["kind"],
                                      "fail:LLM x3 %s" % (err or "空"))
                self.notif_llm_fail.pop(rid, None)
                log("  通知接话放弃 #%d:LLM 连续 3 次失败" % tid, "WARN")
            else:
                log("  通知接话:LLM 暂不可用(第 %d 次),下轮重试 #%d" % (n_fail, tid), "WARN")
            return False
        text = "%s %s" % (mention, self._drop_question_sentences(text.strip()))
        log("  ---- 通知接话 #%d(%s 在 %s 提到你) ----\n%s\n  ----------------"
            % (tid, from_user, ("#%d 楼" % floor) if floor else "主题", text))

        if self.dry:
            self.store.save_notif(rid, tid, from_user, n["kind"], "ok(dry)")
            log("  [DRY-RUN] 未提交")
            return True

        delay = random.randint(self.cfg.get("reply_delay_min", 10),
                               self.cfg.get("reply_delay_max", 30))
        log("  随机等待 %d 秒后提交……" % delay)
        for _ in range(delay):
            if not self.running:
                return False
            time.sleep(1)

        ok, msg = self.site.submit_reply(tid, text)
        if not ok and ("未登录" in (msg or "") or "ajax-reply-form" in (msg or "")):
            # 会话失效:不消费通知,强制重登,下轮重试;连续 3 次才放弃
            n_fail = self.notif_submit_fail.get(rid, 0) + 1
            self.notif_submit_fail[rid] = n_fail
            self._maybe_relogin("接话提交会话失效")
            if n_fail >= 3:
                self.store.save_notif(rid, tid, from_user, n["kind"],
                                      "fail:x3 会话失效 %s" % msg)
                self.notif_submit_fail.pop(rid, None)
                log("  通知接话放弃 #%d:会话失效连续 3 次" % tid, "ERROR")
            else:
                log("  提交失败(%s),重登后下轮重试(第 %d 次)" % (msg, n_fail), "WARN")
            return True
        self.notif_submit_fail.pop(rid, None)
        self.store.save_notif(rid, tid, from_user, n["kind"],
                              "ok" if ok else "fail:%s" % msg)
        if ok:
            self.store.incr_today()
            log("  已回复 %s 在 #%d: %s" % (from_user, tid, msg))
        else:
            log("  通知接话提交失败 #%d: %s(不重试,防止重复接话)" % (tid, msg), "WARN")
        return True

    # ------------------------------------------------------------------
    # 单轮
    # ------------------------------------------------------------------

    def tick(self):
        self.store.heartbeat()

        candidates = self._collect_candidates()
        if not candidates:
            log("本轮没抓到任何候选帖(板块页拉取失败?检查代理是否可用)", "WARN")
            return

        fresh = [t for t in candidates if not self.store.is_seen(t)]
        if not fresh:
            return

        # 【安全阀 1】候选里「没见过」的数量异常多 → 首次运行或长期停摆
        # (例如代理挂了几天、bootstrap 被中断)。此时绝不能逐个回复,
        # 否则等于把近期帖子挨个挖坟。只标记为已见。
        threshold = int(self.cfg.get("bootstrap_threshold", 20) or 20)
        if len(fresh) > threshold:
            log("本轮发现 %d 个未见过帖子(超过阈值 %d),判定为首次运行或长期停摆:"
                "只标记为已见、不回复" % (len(fresh), threshold), "WARN")
            now = datetime.now().isoformat(timespec="seconds")
            self.store.mark_seen_many([(t, "", now, "") for t in fresh])
            return

        # 【安全阀 2】单轮最多处理 N 个,避免突发爆量
        max_per_tick = int(self.cfg.get("max_per_tick", 5) or 5)
        pending = sorted(fresh)[:max_per_tick]
        if len(fresh) > max_per_tick:
            log("本轮 %d 个新帖,按单轮上限先处理 %d 个,其余下轮继续"
                % (len(fresh), max_per_tick))

        log("发现 %d 个新帖: %s" % (len(pending), ", ".join(str(t) for t in pending)))

        limit = self.cfg.get("daily_limit", 20)
        for tid in pending:
            if not self.running:
                return
            if not self.dry and self.store.today_count() >= limit:
                log("已达今日回复上限 %d,本轮跳过余下新帖" % limit, "WARN")
                return
            self._handle_topic(tid)

    def _handle_topic(self, tid):
        topic = self.site.fetch_topic(tid)
        if not topic:
            # 不标记为已见:多半是代理抖动,下轮还要再试
            log("抓取帖子 %d 失败(代理抖动?),下轮重试" % tid, "WARN")
            return

        self.store.mark_seen(tid, "", topic.get("title", ""))

        ok, reason = should_reply(self.cfg, topic, self.store)
        if not ok:
            log("  跳过 #%d 「%s」 — %s" % (tid, topic.get("title", "")[:40], reason))
            if "未登录" in reason:
                # 会话可能已被服务端踢掉:强制重登,并把该帖撤销「已见」,
                # 重登成功后下轮重试(10 分钟内最多重登一次,不受控时保持跳过)
                if self._maybe_relogin(reason):
                    self.store.unmark_seen(tid)
            return

        log("  生成回复: #%d 「%s」(%s) 楼主发言 %d 条"
            % (tid, topic.get("title", "")[:40],
               topic.get("forum_name") or "?", len(topic.get("op_posts") or [])))

        search_block = self._search_block(topic)

        text, err = call_llm(
            self.cfg, self.system_prompt,
            build_user_prompt(topic, self.cfg.get("context_scope", "op"),
                              search_block=search_block))
        if err:
            log("  LLM 失败: %s" % err, "ERROR")
            return
        text = re.sub(r"\s+\n", "\n", text).strip()
        log("  ---- 拟回复 ----\n%s\n  ----------------" % text)

        if self.dry:
            log("  [DRY-RUN] 未提交(把 config.json 的 dry_run 改为 false 才会真的发)")
            return

        delay = random.randint(self.cfg.get("reply_delay_min", 10),
                               self.cfg.get("reply_delay_max", 30))
        log("  随机等待 %d 秒后提交……" % delay)
        for _ in range(delay):
            if not self.running:
                return
            time.sleep(1)

        ok2, msg = self.site.submit_reply(tid, text)
        if ok2:
            self.store.save_reply(tid, topic.get("title", ""), text, "ok")
            self.store.incr_today()
            log("  已发布 #%d: %s" % (tid, msg))
            self._donate_after_reply(topic)
            return

        retryable = any(k in (msg or "") for k in LsbSite.RETRYABLE)
        n = self.fail_count.get(tid, 0) + 1
        self.fail_count[tid] = n
        # 网络类失败(代理抖动)值得重试:撤销「已见」标记,下一轮重新处理;
        # 但最多重试 3 次,免得某个帖一直失败就无限占用额度。
        if retryable and n <= 3:
            self.store.unmark_seen(tid)
            log("  发布失败 #%d: %s —— 下轮重试(第 %d/3 次)" % (tid, msg, n), "WARN")
        else:
            self.store.save_reply(tid, topic.get("title", ""), text, "fail: " + msg)
            log("  发布失败 #%d: %s(不再重试)" % (tid, msg), "ERROR")

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _banner(self):
        cfg = self.cfg
        st = self.store.stats()
        log("=" * 60)
        log("linux.sb 自动回帖机器人")
        log("  模式      : %s" % ("DRY-RUN(只生成不提交)" if self.dry else "实际发布"))
        log("  代理      : %s" % (cfg.get("proxy") or "(未设置)"))
        log("  轮询间隔  : %s 秒" % cfg.get("poll_interval", 60))
        log("  每日上限  : %s" % cfg.get("daily_limit", 20))
        log("  扫描板块  : %s (跳过 %s)" % (cfg.get("scan_forums"),
                                          cfg.get("deny_forums") or []))
        if cfg.get("enable_search", True):
            log("  联网搜索  : 开启,模式 %s = %s"
                % (cfg.get("search_mode", 1), describe_mode(cfg.get("search_mode", 1))))
        else:
            log("  联网搜索  : 关闭")
        if cfg.get("donate_enabled", True):
            log("  自动打赏  : 开启,回帖成功后给楼主打赏 %d 积分"
                % int(cfg.get("donate_amount", 1) or 0))
        else:
            log("  自动打赏  : 关闭")
        if cfg.get("notify_reply_enabled", True):
            log("  通知接话  : 开启,每轮最多处理 %d 条提及/回复"
                % int(cfg.get("notify_max_per_tick", 2) or 0))
        else:
            log("  通知接话  : 关闭")
        if cfg.get("passive_mode"):
            log("  回复模式  : 被动(不主动回新帖,只在被 @ 时回应)")
        log("  AI 接口   : %s" % ", ".join(
            p.get("name") or p.get("model") or "?"
            for p in (cfg.get("llm", {}).get("providers") or [])[:4]))
        log("  历史状态  : 已见 %d 帖 / 已回 %d 帖 / 今日 %d 帖"
            % (st["seen"], st["replied"], st["today"]))
        log("=" * 60)

    def run(self):
        self._banner()

        # 单实例检测:同一台机器双开会让两个进程抢同一个 state.db,
        # 造成重复回复和数据库锁冲突
        ok, info = self.store.acquire_lock()
        if not ok:
            log("拒绝启动:%s" % info, "ERROR")
            log("如果确认那个进程已经不在了,等 2 分钟(心跳过期)后重试即可", "WARN")
            return 4
        log("已获取运行锁(主机 %s)" % info)

        # 代理掉线是常态(Clash 换节点、机场抖动),所以登录失败不退出进程,
        # 而是按退避策略重试;代理恢复后能自愈,无需人工重启。
        fail = 0
        bootstrapped = False
        while self.running:
            if not self.site.check_login():
                if not self.site.login():
                    fail += 1
                    if self.once:
                        log("登录失败,--once 模式直接退出", "ERROR")
                        return 2
                    wait = min(300, 30 * fail)
                    log("登录失败(连续第 %d 次)。常见原因:Clash 代理未开启 / "
                        "节点掉线 / 账号密码有误。%d 秒后重试。" % (fail, wait), "WARN")
                    self.sleep(wait)
                    continue
            fail = 0

            if not bootstrapped:
                self._sync_replies_from_profile()
                if self.store.is_empty():
                    self.bootstrap()
                bootstrapped = True

            try:
                if self.cfg.get("passive_mode"):
                    # 被动模式:不主动回新帖,只在被 @ 时回应
                    self._handle_notifications()
                else:
                    self.tick()
                    self._handle_notifications()
            except Exception as e:
                log("本轮异常: %s: %s" % (type(e).__name__, e), "ERROR")

            if self.once:
                break
            # 轮询节奏加 ±15% 抖动:固定整点间隔的行为特征太明显
            interval = int(self.cfg.get("poll_interval", 60) or 60)
            jitter = max(1, int(interval * 0.15))
            self.sleep(max(5, interval + random.randint(-jitter, jitter)))

        self.store.close()
        log("机器人已停止")
        return 0
