#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""站点交互:登录、发现新帖、抓帖子内容、发帖。

所有出站请求都经 HttpClient(强制代理 + Cookie + 重试)。

设计要点
--------
- **不硬编码任何端点**:每次发帖前实时从帖子页解析表单 action / hidden 字段 /
  验证码参数 / PoW 难度。站点改版、换难度都不用改代码。
- **新帖检测走板块列表页**,不用 sitemap —— sitemap 实测有明显滞后
  (首页已到 21484,它最大只到 21474),靠它会漏新帖。
"""

import http.cookiejar
import json
import re
import time

from .captcha import get_cap_token
from .html import (extract_csrf, extract_div_inner, div_to_text, html_unescape,
                   parse_posts, solve_arithmetic, strip_tags)
from .http import HttpClient
from .log import log
from .paths import DEFAULT_UA


class LsbSite:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = (cfg.get("base_url") or "https://linux.sb").rstrip("/")
        self.http = HttpClient(proxy=cfg.get("proxy", ""),
                               timeout=cfg.get("timeout", 30),
                               ua=cfg.get("user_agent") or DEFAULT_UA)

    # ==================================================================
    # 登录
    # ==================================================================

    def login(self):
        """用账号密码登录;成功返回 True。"""
        if self.cfg.get("cookie"):
            self._load_cookie(self.cfg["cookie"])
            log("已载入 config 中手工指定的 Cookie")
            return self.check_login()

        user = (self.cfg.get("username") or "").strip()
        pwd = self.cfg.get("password") or ""
        if not user or not pwd:
            log("config.json 里没填 username/password,无法登录", "ERROR")
            return False

        code, html, _ = self.http.get(self.base + "/login")
        if code != 200:
            log("打开登录页失败: code=%s" % code, "ERROR")
            return False

        csrf = extract_csrf(html)
        if not csrf:
            log("登录页没找到 _csrf", "ERROR")
            return False

        # 人机验证:登录表单挂了自托管 Cap(challenge -> PoW -> instrumentation -> redeem)
        cap_token = ""
        m = re.search(r'data-cap-api-endpoint="([^"]+)"', html)
        if m:
            try:
                log("检测到 Cap 人机验证,开始求解(约 10-20 秒)……")
                t0 = time.time()
                cap_token = get_cap_token(
                    self.http, m.group(1), referer=self.base + "/login") or ""
                log("Cap 验证通过,耗时 %.1fs" % (time.time() - t0))
            except Exception as e:
                log("Cap 求解失败: %s" % e, "ERROR")
                return False

        data = {"_csrf": csrf, "username": user, "password": pwd}
        if cap_token:
            data["cap_token"] = cap_token
        code, body, final = self.http.post(self.base + "/login", data,
                                          referer=self.base + "/login")
        if code not in (200, 302):
            log("登录请求异常: code=%s" % code, "ERROR")
            return False

        if self.check_login():
            log("登录成功(账号 %s)" % user)
            return True

        # 账号级「登录邮箱验证」:服务端要求先过 6 位邮箱验证码
        if "user_review_login_email" in (final or "") or "登录邮箱验证" in body:
            log("被「登录邮箱验证」拦截:该账号登录需要邮箱验证码。", "WARN")
            log("处理方式:在浏览器手动登录一次完成验证(验证绑定在账号上,"
                "过一次即可),之后程序化登录就能通过", "WARN")
            return False

        tip = ""
        try:
            j = json.loads(body)
            tip = j.get("message") or j.get("tip") or ""
        except Exception:
            pass
        log("登录失败。服务端提示: %s" % (tip or "无"), "ERROR")
        return False

    def _load_cookie(self, cookie_str):
        for part in str(cookie_str).split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            c = http.cookiejar.Cookie(
                0, k.strip(), v.strip(), None, False, "linux.sb", False, False,
                "/", True, False, None, True, None, None, {})
            self.http.jar.set_cookie(c)

    def check_login(self):
        return self.http.get_cookie("bbs_auth") is not None

    # ==================================================================
    # 新帖发现
    # ==================================================================

    def fetch_sitemap(self):
        """返回 {tid: lastmod}。仅用于首次 bootstrap 灌存量帖,不做实时检测。

        为什么不做实时检测:实测首页已到 21484 而 sitemap 最大只到 21474,
        新帖根本没进去,靠它会漏帖。
        """
        code, text, _ = self.http.get(self.base + "/sitemap.xml")
        if code != 200:
            log("拉取 sitemap 失败: code=%s" % code, "WARN")
            return {}
        out = {}
        for m in re.finditer(
                r"<url>\s*<loc>https://linux\.sb/topic/(\d+)</loc>\s*"
                r"(?:<lastmod>([^<]+)</lastmod>)?", text):
            out[int(m.group(1))] = m.group(2) or ""
        return out

    def fetch_forum_topics(self, forum_id, pages=1):
        """抓板块列表页,返回该板块的帖子 id 列表(第 1 页起)。

        页面里除主列表外还有侧边栏的「每日热帖」等干扰链接,因此把范围限定在
        <ul class="post-list"> ... </ul> 之内。
        """
        tids = []
        for p in range(1, max(1, pages) + 1):
            url = "%s/forum/%d" % (self.base, forum_id)
            if p > 1:
                url += "?p=%d" % p
            code, html, _ = self.http.get(url)
            if code != 200:
                continue
            m = re.search(r'<ul[^>]*class="post-list"[^>]*>', html)
            if not m:
                continue
            seg = html[m.end():]
            end = seg.find("</ul>")
            if end > 0:
                seg = seg[:end]
            for mm in re.finditer(r'href="/topic/(\d+)"', seg):
                tid = int(mm.group(1))
                if tid not in tids:
                    tids.append(tid)
        return tids

    # ==================================================================
    # 帖子内容
    # ==================================================================

    def fetch_topic(self, tid):
        """抓取帖子页,返回结构化信息。选择器基于 2026-09-11 实测 DOM。"""
        url = "%s/topic/%d" % (self.base, tid)
        code, html, _ = self.http.get(url)
        if code != 200:
            return None

        info = {
            "tid": tid, "url": url, "html": html,
            "title": "", "forum_id": None, "forum_name": "",
            "author": "", "content": "", "images": [],
            "reply_count": 0, "post_count": 0,
            "can_reply": False, "closed": False,
            "is_gacha": False, "gacha_status": "",
            "posts": [], "op_name": "", "op_posts": [],
        }

        # 标题:<h1 class="post-content-title"><a>标题</a></h1>
        m = re.search(r'<h1[^>]*class="post-content-title"[^>]*>(.*?)</h1>', html, re.S)
        if m:
            info["title"] = html_unescape(strip_tags(m.group(1))).strip()
        if not info["title"]:
            m = re.search(r"<title>(.*?)</title>", html, re.S)
            if m:
                info["title"] = html_unescape(strip_tags(m.group(1))).split(" - ")[0].strip()

        # 板块:取面包屑里最后一个 /forum/N
        m = re.search(r'<div class="breadcrumb">(.*?)</div>', html, re.S)
        if m:
            links = re.findall(r'href="/forum/(\d+)"[^>]*>([^<]+)</a>', m.group(1))
            if links:
                info["forum_id"] = int(links[-1][0])
                info["forum_name"] = html_unescape(links[-1][1]).strip()

        # 发卡贴标记(站点自带的虚拟卡帖标志,比关键词可靠)
        m = re.search(r'class="virtual-card-title-status[^"]*"[^>]*>(.*?)</span>', html, re.S)
        if m:
            info["is_gacha"] = True
            info["gacha_status"] = html_unescape(strip_tags(m.group(1))).strip()

        # 回复数
        m = re.search(r'<div class="post-content-stats">(.*?)</div>', html, re.S)
        if m:
            nums = re.findall(r">(\d+)<", m.group(1))
            if len(nums) >= 2:
                info["reply_count"] = int(nums[1])

        info["post_count"] = len(re.findall(r'<li class="post-item post-entry"', html))

        # 楼主
        m = re.search(r'<a[^>]*class="post-title post-author"[^>]*>(.*?)</a>', html, re.S)
        if m:
            info["author"] = html_unescape(strip_tags(m.group(1))).strip()

        # 首楼正文
        pos = html.find("nb-editor-post-content")
        if pos == -1:
            pos = html.find('class="post-content"')
        if pos != -1:
            start = html.rfind("<div", 0, pos)
            seg = extract_div_inner(html, start)
            info["content"] = div_to_text(seg)[:6000]
            # 记录正文图片:纯图帖(只有截图没有文字)正文会抓成空串,需要单独识别
            info["images"] = [u for u in re.findall(r'<img[^>]+src="([^"]+)"', seg)
                              if "avatar" not in u and "emoji" not in u]

        # 解析全部楼层:生成回复时用「楼主在本帖的全部发言」做上下文,
        # 比只看首楼更能抓住楼主的完整诉求(追问、补充都在后面的楼层里)。
        info["posts"] = parse_posts(html)
        info["op_name"] = ((info["posts"][0].get("author") if info["posts"] else "")
                           or info["author"])
        info["op_posts"] = [p for p in info["posts"]
                            if p.get("author") and p["author"] == info["op_name"]]

        info["can_reply"] = ("reply-login-box" not in html)
        info["closed"] = ("data-topic-closed" in html or
                          "topic-management-locked" in html or
                          "topic-locked" in html)
        return info

    # ==================================================================
    # 发帖
    # ==================================================================

    def parse_reply_form(self, html):
        """从帖子页解析回复表单的 action、hidden 字段、验证码挑战与 PoW 参数。"""
        out = {"action": None, "fields": {}, "captcha": None}

        m = re.search(r'<form[^>]*class="[^"]*ajax-reply-form[^"]*"[^>]*>', html)
        if not m:
            m = re.search(r'<form[^>]*ajax-reply-form[^>]*>', html)
        if not m:
            return out
        tag = m.group(0)
        am = re.search(r'action="([^"]*)"', tag)
        if am:
            out["action"] = html_unescape(am.group(1)).strip()

        # 表单内 hidden 字段
        seg = html[m.end(): m.end() + 8000]
        for fm in re.finditer(r'<input[^>]*type="hidden"[^>]*>', seg):
            t = fm.group(0)
            nm = re.search(r'name="([^"]+)"', t)
            vm = re.search(r'value="([^"]*)"', t)
            if nm:
                out["fields"][nm.group(1)] = html_unescape(vm.group(1)) if vm else ""

        # 轻量验证码组件(PoW + 算术题 + 蜜罐)
        cm = re.search(r'<[^>]*data-native-captcha[^>]*>', html)
        if cm:
            wtag = cm.group(0)

            def attr(name, default=""):
                am2 = re.search(r'data-%s="([^"]*)"' % name, wtag)
                return html_unescape(am2.group(1)) if am2 else default

            captcha = {
                "token": "", "question": "",
                "prefix": attr("pow-prefix"),
                "zeros": attr("pow-zeroes", "3"),
                "required": attr("pow-required", "1"),
            }
            wseg = html[cm.end(): cm.end() + 4000]
            tm = re.search(r'name="native_captcha_token"[^>]*value="([^"]*)"', wseg)
            if not tm:
                tm = re.search(r'value="([^"]*)"[^>]*name="native_captcha_token"', wseg)
            if tm:
                captcha["token"] = html_unescape(tm.group(1))
            qm = re.search(r'class="native-captcha-question"[^>]*>(.*?)<', wseg, re.S)
            if qm:
                captcha["question"] = html_unescape(strip_tags(qm.group(1)))
            captcha["prefix"] = captcha["prefix"] or attr("pow")
            out["captcha"] = captcha
        return out

    @staticmethod
    def _resolve_action(action, base, topic_url):
        if action.startswith("/"):
            return base + action
        if not action.startswith("http"):
            return topic_url + "/" + action.lstrip("./")
        return action

    def _submit_reply_once(self, tid, text):
        """完整发帖流程:实时解析表单 -> 过验证 -> POST。返回 (ok, message)。"""
        url = "%s/topic/%d" % (self.base, tid)
        code, html, _ = self.http.get(url)
        if code != 200:
            return False, "打开发帖页失败 code=%s" % code

        form = self.parse_reply_form(html)
        if not form["action"]:
            return False, "页面里没有找到 ajax-reply-form(可能未登录 / 已锁帖)"

        action = self._resolve_action(form["action"], self.base, url)

        data = dict(form["fields"])
        data["body"] = text

        cap = form["captcha"]
        if cap:
            if cap["required"] != "0" and cap["prefix"]:
                from .captcha import solve_pow
                nonce = solve_pow(cap["prefix"], int(cap["zeros"] or 3))
                if nonce is None:
                    return False, "PoW 求解失败(prefix=%s)" % cap["prefix"]
                data["native_captcha_pow"] = nonce
            if cap["question"]:
                ans = solve_arithmetic(cap["question"])
                if ans is None:
                    return False, "算术题解析失败: %s" % cap["question"]
                data["native_captcha_answer"] = ans
            data["native_captcha_token"] = cap["token"]
            # 蜜罐字段:必须留空
            data["native_captcha_company"] = ""

        code, body, _ = self.http.post(
            action, data,
            extra_headers={"X-Requested-With": "XMLHttpRequest", "Origin": self.base},
            referer=url)

        if code == 0:
            return False, "请求失败: %s" % body
        try:
            j = json.loads(body)
        except Exception:
            if "cloudflare" in body.lower() or "turnstile" in body.lower():
                return False, "被 Cloudflare 拦截(Turnstile)"
            return False, "返回非 JSON(code=%s): %s" % (code, strip_tags(body)[:200])

        if j.get("ok"):
            return True, j.get("message") or j.get("tip") or "发布成功"
        return False, j.get("message") or j.get("tip") or str(j)[:200]

    # 判定为「网络层瞬时故障」的错误特征:这类才值得重试;
    # 业务层拒绝(内容不能为空、权限不足等)重试多少次都一样。
    RETRYABLE = ("请求失败", "打开发帖页失败", "返回非 JSON")

    def submit_reply(self, tid, text, max_attempt=3):
        """带重试的发帖。代理抖动在这环境里很常见,一次失败就放弃太浪费。"""
        msg = ""
        for attempt in range(1, max(1, max_attempt) + 1):
            ok, msg = self._submit_reply_once(tid, text)
            if ok:
                return True, msg
            if not any(k in (msg or "") for k in self.RETRYABLE):
                return False, msg            # 业务层拒绝,不重试
            if attempt < max_attempt:
                log("  提交失败(%s),%d 秒后重试" % (msg or "网络错误", 2 * attempt), "WARN")
                time.sleep(2 * attempt)
        return False, msg

    # ==================================================================
    # 打赏(点赞 + 积分)
    # ==================================================================

    def parse_donate_form(self, html):
        """从打赏弹层页解析 POST /donate 所需的全部字段。

        表单实测结构(2026-09-12):
            <form method="post" action="/donate" data-donate-form data-no-ajax="1">
              <input type="hidden" name="_csrf" value="...">
              <input type="hidden" name="topic_id" value="...">
              <input type="hidden" name="request_key" value="...">
              <input type="number" name="amount" min="1" max="99"
                     placeholder="不填写则只点赞">
        request_key 每次打开页面都变,必须现取。
        """
        m = re.search(r'<form method="post" action="/donate"[^>]*>(.*?)</form>',
                      html, re.S)
        if not m:
            return None
        seg = m.group(1)
        out = {}
        for name in ("_csrf", "topic_id", "request_key"):
            fm = re.search(r'name="%s"[^>]*value="([^"]*)"' % name, seg)
            if not fm:
                fm = re.search(r'value="([^"]*)"[^>]*name="%s"' % name, seg)
            out[name] = html_unescape(fm.group(1)) if fm else ""
        if not (out["_csrf"] and out["request_key"]):
            return None
        return out

    def donate_topic(self, tid, amount=1):
        """给帖子打赏 amount 积分(同时完成点赞)。

        流程:GET /donate?topic_id=N 拿弹层表单(含一次性 request_key)
             → POST /donate (multipart,与浏览器一致)
        返回 (ok, message)。余额不足等业务拒绝不重试。
        """
        url = "%s/donate?topic_id=%d" % (self.base, tid)
        code, html, _ = self.http.get(url)
        if code != 200:
            return False, "打赏页打开失败 code=%s" % code

        form = self.parse_donate_form(html)
        if not form:
            return False, "打赏页里没有找到表单(可能已登录失效/已打赏过/不可打赏)"

        fields = [("_csrf", form["_csrf"]),
                  ("topic_id", form["topic_id"] or str(tid)),
                  ("request_key", form["request_key"]),
                  ("amount", str(int(amount)))]
        code, body, _ = self.http.post_multipart(
            self.base + "/donate", fields,
            extra_headers={"Origin": self.base, "X-Requested-With": "XMLHttpRequest"},
            referer=url)
        if code == 0:
            return False, "打赏请求失败: %s" % body

        # 成功时服务端可能重定向回帖子页,或返回 JSON/HTML 提示
        try:
            j = json.loads(body)
            if j.get("ok") or j.get("success"):
                return True, j.get("message") or j.get("tip") or "打赏成功"
            if j.get("message") or j.get("tip"):
                return False, j.get("message") or j.get("tip")
        except (ValueError, TypeError):
            pass
        low = body.lower()
        if "不足" in body or "余额" in body:
            return False, "积分不足"
        if "已打赏" in body or "已点赞" in body:
            return False, "已打赏过(不能重复)"
        if code in (200, 302) and ("<html" in low or code == 302):
            # 重定向回帖子页 = 表单提交被接受
            return True, "打赏成功"
        return False, "返回异常 code=%s: %s" % (code, strip_tags(body)[:150])


# 向后兼容:早期代码用的是模块级 solve_pow
from .captcha import solve_pow  # noqa: E402,F401
