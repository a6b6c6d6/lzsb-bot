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
import json
import time
from datetime import datetime, timedelta

from .config import SEARCH_MODE_LABELS
from .filters import should_reply
from .log import log
from .llm import call_llm, extract_keywords
from .prompt import (DEFAULT_SYSTEM_PROMPT, DONATE_SYSTEM, NOTIFY_SYSTEM,
                     PLAN_SYSTEM, build_donate_prompt,
                     build_notify_user_prompt, build_user_prompt)
from .site import LsbSite
from .store import Store
from .search import search as web_search
from .search import format_block, build_query, describe_mode


class Bot:
    def __init__(self, cfg, dry_run=None, once=False, donate_only=False):
        self.cfg = cfg
        self.once = once
        self.donate_only = donate_only
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
    # 只打赏模式(不回复,AI 评分后按分打赏)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_score_json(text):
        """从模型输出里抠出 {"score":N,"reason":"..."}。失败返回 (None, "")。

        弱模型/推理模型的 JSON 常见毛病:包 ```围栏、前后带说明、score 写成
        浮点或字符串。三层兜底:整体 loads → 截取 {..} 再 loads → 正则抠字段。
        """
        t = (text or "").strip()
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I).strip()
        cands = [t]
        i, j = t.find("{"), t.rfind("}")
        if 0 <= i < j:
            cands.append(t[i:j + 1])
        for c in cands:
            try:
                obj = json.loads(c)
            except (ValueError, TypeError):
                continue
            if not isinstance(obj, dict) or obj.get("score") is None:
                continue
            try:
                sc = int(round(float(obj["score"])))
            except (TypeError, ValueError):
                continue
            return max(0, min(20, sc)), str(obj.get("reason") or "").strip()
        m = re.search(r'"score"\s*:\s*([0-9]+(?:\.[0-9]+)?)', t)
        if m:
            sc = int(round(float(m.group(1))))
            mr = re.search(r'"reason"\s*:\s*"([^"]*)"', t)
            return max(0, min(20, sc)), (mr.group(1) if mr else "")
        return None, ""

    def _score_topic(self, topic):
        """让 AI 给帖子评 1-20 分。返回 (score, reason) 或 (None, 错误说明)。"""
        text, err = call_llm(self.cfg, DONATE_SYSTEM,
                             build_donate_prompt(topic), strict_tail=False)
        if err or not text:
            return None, err or "模型空输出"
        score, reason = self._parse_score_json(text)
        if score is None:
            return None, "评分解析失败: %s" % (text or "")[:80]
        return score, reason

    def _claim_daily_rewards(self):
        """领一次广告位每日积分(失败不影响后续打赏)。"""
        try:
            ok, msg = self.site.claim_promotions_reward()
            log("  每日领积分: %s" % msg, "INFO" if ok else "WARN")
            return ok
        except Exception as e:
            log("  每日领积分异常: %s" % e, "WARN")
            return False

    def _try_claim_rewards(self):
        """尝试领每日积分(所有模式共用)。

        领积分走 Playwright 启动浏览器(十几秒起),开销远大于普通请求,不能每轮
        都试;但也不能「每天只试一次、失败就作罢」——登录/网络抽风时当天奖励就
        白丢了(2026-10-12 那次中断回退 HTTP 拿到 0 就是这种情况)。策略:
          * 今天已经领到过 → 跳过;
          * 今天已试过但没领到 → 隔 reward_retry_minutes 分钟补试,
            当天最多 reward_max_tries 次,既容忍偶发失败又不至于反复空转;
          * 领成功就记 claimed,当天不再动。

        dry 模式不领,避免测试时白耗当天机会。
        """
        if self.dry or self.store.claimed_reward_today():
            return
        tries, last = self.store.reward_tries_today()
        max_tries = int(self.cfg.get("reward_max_tries", 3) or 3)
        if tries >= max_tries:
            return
        cooldown = int(self.cfg.get("reward_retry_minutes", 30) or 0)
        if last is not None and cooldown > 0:
            if datetime.now() - last < timedelta(minutes=cooldown):
                return
        self.store.mark_reward_try()
        if self._claim_daily_rewards():
            self.store.mark_reward_claimed()

    def _donate_only_tick(self):
        """只打赏单轮:查余额 → 扫新帖 → AI 评分 → 按分打赏。

        打赏总额受「余额 - 保留线」约束,不会把积分打光;每帖只处理一次
        (评分过就记库),既防止重复评分刷 AI,也防止重复打赏。
        每日领积分已提到 run() 里对所有模式统一处理。
        """
        self.store.heartbeat()

        balance = self.site.get_my_points()
        if balance is None:
            log("  查不到积分余额,本轮跳过", "WARN")
            return
        reserve = int(self.cfg.get("donate_reserve", 200) or 0)
        budget = balance - reserve
        log("  当前积分 %d,保留底线 %d,可用打赏预算 %d" % (balance, reserve, budget))
        if budget < 1:
            log("  预算不足(余额已接近保留线),本轮不打赏", "WARN")
            return

        max_n = int(self.cfg.get("donate_max_per_tick", 10) or 10)
        min_score = int(self.cfg.get("donate_min_score", 1) or 1)
        done = 0
        for tid in self._collect_candidates():
            if not self.running or budget < 1 or done >= max_n:
                break
            if self.store.has_donated(tid):
                continue
            topic = self.site.fetch_topic(tid)
            if not topic:
                continue
            if (topic.get("op_name") or "") == (self.cfg.get("bot_username") or ""):
                continue                       # 不给自己打赏

            score, reason = self._score_topic(topic)
            if score is None:
                log("  评分失败 #%d: %s" % (tid, reason), "WARN")
                continue
            log("  #%d 「%s」AI 评分 %d/20 — %s"
                % (tid, (topic.get("title") or "")[:36], score, reason))

            amount = 0
            if score <= 0:
                log("  AI 判定无价值(0 分),不打赏")
            elif score < min_score:
                log("  评分低于阈值 %d,不打赏" % min_score)
            else:
                amount = min(score, budget)
                if self.dry:
                    log("  [DRY-RUN] 拟打赏 #%d %d 积分(未提交)" % (tid, amount))
                    budget -= amount
                    done += 1
                else:
                    try:
                        ok, msg = self.site.donate_topic(tid, amount)
                    except Exception as e:
                        ok, msg = False, "异常 %s" % e
                    if ok:
                        log("  已打赏 #%d %d 积分: %s" % (tid, amount, msg))
                        budget -= amount
                        done += 1
                    else:
                        log("  打赏 #%d 未成功: %s" % (tid, msg), "WARN")
                        amount = 0
            # 记库:避免同帖反复评分、反复尝试打赏(dry 模式只读,不写库)
            if not self.dry:
                self.store.save_donation(tid, score, amount)

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
    def _parse_notify_json(text):
        """从模型输出提取 {"channel","reply"}。

        弱模型的 JSON 常见毛病:包 ```json 围栏、前后带说明文字、换行没转义。
        三层兜底:整体 loads → 截取 {..} 再 loads → 正则抠 channel/reply 字段。
        全部失败返回 (None, None),调用方按公开回复兜底。
        """
        t = (text or "").strip()
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I).strip()
        candidates = [t]
        i, j = t.find("{"), t.rfind("}")
        if 0 <= i < j:
            candidates.append(t[i:j + 1])
        for c in candidates:
            try:
                obj = json.loads(c)
                if isinstance(obj, dict) and obj.get("reply"):
                    ch = str(obj.get("channel") or "post").strip().lower()
                    return ("dm" if ch == "dm" else "post"), str(obj["reply"]).strip()
            except (ValueError, TypeError):
                pass
        m_ch = re.search(r'"channel"\s*:\s*"([^"]*)"', t)
        m_re = re.search(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)"', t, re.S)
        if m_re:
            raw = m_re.group(1)
            # 弱模型经常输出非法 JSON(字符串里有真实换行等),json.loads 会失败;
            # 这里手工解转义,保住换行格式
            raw = raw.replace('\\\\', '\x00').replace('\\n', '\n') \
                     .replace('\\t', ' ').replace('\\"', '"') \
                     .replace('\\/', '/').replace('\x00', '\\')
            ch = (m_ch.group(1).strip().lower() if m_ch else "post")
            return ("dm" if ch == "dm" else "post"), raw.strip()
        return None, None

    @staticmethod
    def _strip_inner_mentions(text, names):
        """把正文里对他人的 @ 提及降级成纯文本。

        模型经常无视「不要 @ 任何人」,在正文里写 @某某 举例(实测回复里
        出现过 @Sam.Altman、@温庭筠),会给毫不相干的第三方刷通知。
        只匹配**已知的论坛用户名**(当事人 + 本帖楼层作者),不用通用的
        @\\w+ 正则 —— 否则 `@property`、装饰器、邮箱全都会被误伤。
        保留名字本身(只去掉 @ 和楼层号),句子读起来不会缺主语。
        """
        if not text:
            return text
        known = {(n or "").strip() for n in names}
        for nm in sorted((n for n in known if n), key=len, reverse=True):
            text = re.sub(r'@%s(?:\s*#\d+)?' % re.escape(nm), nm, text)
        return re.sub(r'[ \t]{2,}', ' ', text)

    @staticmethod
    def _clean_reply_text(reply):
        """回复正文最后清理:剥掉模型漏写的渠道标记/Markdown 加粗符号。"""
        if not reply:
            return reply
        r = reply.strip()
        r = re.sub(r'^\s*\[(?:DM|帖|公开|私信)\]\s*', '', r, flags=re.I)
        r = r.replace('**', '')
        r = r.replace('\\n', '\n')          # 漏解码的换行转义
        r = re.sub(r'\n{3,}', '\n\n', r)
        return r.strip()

    # 提示词泄漏/注入得手的特征:回复里出现内部提示词片段或元话语。
    # 模型被注入的概率无法降为零,这里是最后一道代码防线。
    LEAK_MARKERS = (
        "NOTIFY_SYSTEM", "DEFAULT_SYSTEM_PROMPT", "PLAN_SYSTEM",
        "ignore all previous", "ignore previous instructions",
        "disregard all previous",
    )

    # 只靠固定字符串对不上:实测那次泄漏(把渠道判断规则 1/2/3 复述进私信)
    # 一个 marker 都没命中。改成匹配「谈论自己的规则/身份」这种元话语句式,
    # 同时避开站里正常的技术用词(SQL 注入、shell 指令、聊提示词工程都不算)。
    LEAK_PATTERNS = tuple(re.compile(p, re.I) for p in (
        r'我(?:的|这边的|当前|现在)?(?:系统)?(?:提示词|人设)(?:是|有|包括|：|:)',
        r'(?:系统|内部|上面的)(?:提示词|提示|设定|规则)',
        r'(?:忽略|无视)(?:之前|上面|前面)(?:所有)?(?:的)?指令',
        r'我(?:会|只会|只能)(?:根据|按照)帖子内容',
        r'(?:私信还是公开|公开还是私信)',
        r'绝不(?:会)?主动发起(?:对话|聊天|私信)',
        r'(?:绕过|无视|忽略)(?:系统|安全)(?:的)?(?:设定|策略)',
        r'我(?:直接)?(?:已经?|就)?(?:忽略|无视|过滤)(?:了|掉)?(?:「|“)?(?:协议|指令)',
        r'(?:协议|指令)部(?:分|内容)',
        r'(?:系统|安全)测试或(?:恶作剧|试探)',
        r'老用户模式',
        r'作为(?:一个)?\s*(?:AI|ai|人工智能|语言模型|大模型)',
        r'我(?:是|只是)(?:一个)?(?:语言模型|大模型|AI\s*助手)',
    ))

    # 常见顶级域名:用于从回复里抠出"域名",与源材料比对
    DOMAIN_RE = re.compile(
        r'([a-z0-9][a-z0-9-]*\.(?:com|net|org|cn|io|me|cc|xyz|top|app|dev|site|online))')

    @classmethod
    def _strip_unknown_links(cls, text, source=""):
        """移除回复中引用了源材料里不存在的域名/链接的行(疑似注入打广告)。

        注入得手的典型形态:模型被诱导在回复末尾加"访问 xxx.com 领取奖励"。
        源材料(帖子正文+楼层+搜索结果)里出现过的域名视为合法,
        其余域名的整行删掉;删完太短(<30字)交由调用方按失败处理。
        返回 (清理后文本, 是否删过东西)。
        """
        if not text:
            return text, False
        src = (source or '').lower()
        out, removed = [], False
        for line in text.split('\n'):
            domains = cls.DOMAIN_RE.findall(line.lower())
            bad = [d for d in domains if d not in src and d != 'linux.sb']
            if bad:
                removed = True
                continue
            out.append(line)
        cleaned = '\n'.join(out).strip()
        # 注意:即使删完剩得很短也返回清理后的文本 —— 短而干净好过长而带毒,
        # 是否可接受由调用方的 poisoned/长度检查决定
        return cleaned, removed

    @classmethod
    def _reply_poisoned(cls, text):
        """生成结果的注入兜底检查。True = 疑似被注入/泄漏,不应发布。"""
        if not text or len(text.strip()) < 5:
            return True
        low = text.lower()
        for m in cls.LEAK_MARKERS:
            if m.lower() in low:
                return True
        for pat in cls.LEAK_PATTERNS:
            if pat.search(text):
                return True
        if text.count("http") > 3:            # 疑似被利用发垃圾链接
            return True
        return False

    # 句末标点:切句用。半角 ! . ; 和换行必须算在内 —— 旧实现的停止集只有
    # 全角几个字符,删问句时会一路吃穿前面整段(实测 36 字删剩 10 字)。
    _SENT_ENDERS = "。！？…；!?;\n"
    _QUESTION_RE = re.compile(r"[？?]")

    @classmethod
    def _split_sentences(cls, text):
        """无损切句:拼回去与原文完全一致(标点、换行都留在句尾)。"""
        out, start, n = [], 0, len(text)
        for i, ch in enumerate(text):
            if ch in cls._SENT_ENDERS and (i + 1 >= n
                                           or text[i + 1] not in cls._SENT_ENDERS):
                out.append(text[start:i + 1])
                start = i + 1
        if start < n:
            out.append(text[start:])
        return out

    @classmethod
    def _tame_questions(cls, text, keep=1):
        """收敛问句:删掉结尾的问句,正文里最多留 keep 句。

        站点氛围下回帖以提问收尾容易显得挑事,但「一个问号都不许有」是做不到的
        硬规则,模型必然违反。旧实现用一条正则从「上一个句号」删到「?」,停止集
        缺换行和半角标点,会把前面整段一起删掉(日志里那些以「不过既然你问了」
        开头的回复就是被删剩的残句)。现在先无损切句,再只删该删的那一句。
        """
        if not text or not cls._QUESTION_RE.search(text):
            return text
        sents = cls._split_sentences(text)
        is_q = [bool(cls._QUESTION_RE.search(s)) for s in sents]

        while sents and is_q[-1]:             # 不要以提问收尾
            sents.pop()
            is_q.pop()

        kept, out = 0, []
        for s, q in zip(sents, is_q):
            if q:
                kept += 1
                if kept > keep:               # 超出配额的问句整句丢掉
                    continue
            out.append(s)

        cleaned = re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()
        return cleaned if len(cleaned) >= 10 else text

    @classmethod
    def _truncate(cls, text, limit):
        """硬上限截断,但尽量断在句子边界,不切在半句上。"""
        if not text or len(text) <= limit:
            return text
        head = text[:limit]
        cut = max(head.rfind(c) for c in cls._SENT_ENDERS)
        # 砍掉超过一半内容就不值得凑整了,宁可保内容按原长截断
        return head[:cut + 1].strip() if cut >= limit // 2 else head.strip()

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
            if not n.get("mentioned"):
                # 「一定要检测到@才回复」:通知内容里没有真的 @ bot 就不插嘴
                # (有人在帖子里回复其他人也会产生通知,不能去接话)
                self.store.save_notif(rid, tid, from_user, kind, "skip:无@提及")
                continue
            if from_user == bot_name:
                self.store.save_notif(rid, tid, from_user, kind, "skip:自己")
                continue
            if handled >= maxn:
                break                         # 留给下一轮,不标记
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

        # 先定位对方那条回复,拿到**全文**再生成 —— 通知页的摘要只截到第一个
        # </p>,多段留言喂给模型就只剩第一段。楼层号顺手拿到,公开回复要用。
        floor, their_text_full = self.site.locate_floor(tid, rid)
        their_text = (their_text_full or "").strip() or n["text"]

        # 渠道(dm=私信 / post=帖子内回复)与回复内容由模型判断,输出 JSON。
        # strict_tail=False:这里的输出是 JSON,句尾永远是 `"}`,开着必然误报
        text, err = call_llm(
            self.cfg, NOTIFY_SYSTEM,
            build_notify_user_prompt(topic, from_user, their_text, n["text"],
                                     bot_name=self.cfg.get("bot_username") or ""),
            strict_tail=False)
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

        channel, reply = self._parse_notify_json(text)
        if not reply:
            channel, reply = "post", text
            log("  通知接话: JSON 解析失败,按公开回复兜底", "WARN")
        reply = self._clean_reply_text(self._tame_questions(reply.strip()))
        # 正文里 @ 别人会给第三方刷通知(模型屡教不改),降级成纯名字
        reply = self._strip_inner_mentions(
            reply, [from_user, topic.get("op_name")]
            + [p.get("author") for p in (topic.get("posts") or [])])
        # 注入兜底:回复中出现源材料里不存在的域名 → 删该行;泄漏/过短 → 重试
        # ⚠ 白名单必须**排除 @ 你的那个人写的楼层** —— 注入文本里的垃圾域名混进
        #   白名单就会让清除逻辑整个失效。posts 是全部楼层,天然包含他那一楼,
        #   所以这里按作者剔除(他可能把载荷放在同帖的另一楼,不只是 rid 那层)
        source = ((topic.get("content") or "") + " "
                  + " ".join((p.get("content") or "")
                             for p in (topic.get("posts") or [])
                             if (p.get("author") or "") != from_user
                             and p.get("pid") != str(rid)))
        reply, removed = self._strip_unknown_links(reply, source)
        if removed:
            log("  已移除回复中来源不明的链接内容(疑似注入)", "WARN")
        # 长度下限只用来挡空回复:提示词要的就是简短回应,
        # 卡到 30 字会把「这个我踩过,重启就行」这种正常短回复当注入丢掉
        if not reply or len(reply) < 10 or self._reply_poisoned(reply):
            # 注入兜底:疑似泄漏/异常内容不发布。不消费通知,重试 3 次才放弃
            n_fail = self.notif_llm_fail.get(rid, 0) + 1
            self.notif_llm_fail[rid] = n_fail
            if n_fail >= 3:
                self.store.save_notif(rid, tid, from_user, n["kind"],
                                      "fail:疑似注入/空回复 x3")
                self.notif_llm_fail.pop(rid, None)
                log("  通知接话放弃 #%d:回复疑似注入或为空,连续 3 次" % tid, "ERROR")
            else:
                log("  通知接话: 回复疑似注入泄漏或为空(第 %d 次),丢弃重试" % n_fail, "WARN")
            return False

        if channel == "dm" and not self.cfg.get("notify_allow_dm", True):
            log("  模型选择私信渠道,但 notify_allow_dm=false,改为帖子内回复", "WARN")
            channel = "post"

        if channel == "dm":
            # ---- 私信渠道:不需要楼层号,直接发给对方 ----
            log("  ---- 通知接话 #%d(%s 要求/适合私信) ----\n%s\n  ----------------"
                % (tid, from_user, reply))
            if self.dry:
                self.store.save_notif(rid, tid, from_user, n["kind"], "ok(dry:dm)")
                log("  [DRY-RUN] 私信未发送")
                return True
            delay = random.randint(self.cfg.get("reply_delay_min", 10),
                                   self.cfg.get("reply_delay_max", 30))
            log("  随机等待 %d 秒后发送私信……" % delay)
            for _ in range(delay):
                if not self.running:
                    return False
                time.sleep(1)
            ok, msg = self.site.send_dm(n.get("from_uid") or 0,
                                        self._truncate(reply, 480))
            if not ok and ("未找到私信表单" in (msg or "") or "私信页打开失败" in (msg or "")):
                # 会话失效类症状:不消费,强制重登,下轮重试,连续 3 次才放弃
                n_fail = self.notif_submit_fail.get(rid, 0) + 1
                self.notif_submit_fail[rid] = n_fail
                self._maybe_relogin("私信发送会话失效")
                if n_fail >= 3:
                    self.store.save_notif(rid, tid, from_user, n["kind"],
                                          "fail:x3 私信会话失效 %s" % msg)
                    self.notif_submit_fail.pop(rid, None)
                    log("  通知接话放弃 #%d:私信会话失效连续 3 次" % tid, "ERROR")
                else:
                    log("  私信发送失败(%s),重登后下轮重试(第 %d 次)" % (msg, n_fail), "WARN")
                return True
            self.notif_submit_fail.pop(rid, None)
            self.store.save_notif(rid, tid, from_user, n["kind"],
                                  "ok(dm)" if ok else "fail:dm %s" % msg)
            if ok:
                self.store.incr_today()
                log("  已私信 %s: %s" % (from_user, msg))
            else:
                log("  私信发送失败 #%d: %s(不重试,防止重复发送)" % (tid, msg), "WARN")
            return True

        # ---- 公开渠道:盖楼回复(楼层号 = 对方那条回复所在的楼,生成前已定位) ----
        if floor is None:
            log("  通知接话: 未能定位 %s 的回复楼层(审核隐藏/已删除),改用纯 @ 提及"
                % from_user, "WARN")
        mention = "@%s" % from_user + (" #%d" % floor if floor else "")
        text = "%s %s" % (mention, self._truncate(reply, 600))
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
            log("  已回复 %s(帖子 #%d): %s" % (from_user, tid, msg))
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
        text = self._tame_questions(self._clean_reply_text(text))
        text = self._strip_inner_mentions(
            text, [topic.get("op_name")]
            + [p.get("author") for p in (topic.get("posts") or [])])
        # 注入兜底:回复中来源不明的域名整行移除(源材料 = 帖子 + 搜索结果)
        source = ((topic.get("content") or "") + " "
                  + " ".join((p.get("content") or "") for p in (topic.get("posts") or []))
                  + " " + (search_block or ""))
        text, removed = self._strip_unknown_links(text, source)
        if removed:
            log("  已移除回复中来源不明的链接内容(疑似注入)", "WARN")
        if self._reply_poisoned(text):
            # 注入兜底:疑似提示词泄漏/垃圾内容,丢弃不发布(帖子保持已见,不重试)
            log("  回复疑似提示词泄漏或垃圾内容,已丢弃不发布: #%d" % tid, "WARN")
            return
        text = self._truncate(text, 600)     # 公开回复硬上限,防被注入刷长文
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
        if self.donate_only:
            log("  运行模式  : 只打赏(不回复,AI 评分 0-20,0 分无价值不打赏)")
            log("  打赏策略  : 预算 = 余额 - 保留 %d;单轮最多 %s 帖,低于 %s 分不打"
                % (int(cfg.get("donate_reserve", 200) or 0),
                   cfg.get("donate_max_per_tick", 10),
                   cfg.get("donate_min_score", 1)))
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

            # 每次运行都尝试领一下每日积分(所有模式共用;今天已领到就跳过)
            self._try_claim_rewards()

            try:
                if self.donate_only:
                    # 只打赏模式:不回复,只扫新帖 AI 评分后按分打赏
                    self._donate_only_tick()
                elif self.cfg.get("passive_mode"):
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
