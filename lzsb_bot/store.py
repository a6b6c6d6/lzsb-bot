#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 状态库:去重、回复记录、每日计数、单实例心跳。

设计说明
--------
- `seen`    : 见过的帖子 id。首次运行会把全站存量帖一次性灌进来(见 bot.bootstrap)。
- `replied` : 发过的回复。只有 result 以 "ok" 开头才算成功,失败的留着重试。
- `daily`   : 每日回复计数,配合 daily_limit 防刷屏。
- `runtime` : 单实例心跳。同一台机器双开会抢同一个 db,造成重复回复。

WAL + synchronous=NORMAL 是必须的:首次 bootstrap 要写上万条记录,
默认的 journal_mode=delete + synchronous=FULL 每条都落盘同步,会慢到分钟级。
"""

import os
import socket
import sqlite3
from datetime import datetime, date

from .log import log
from .paths import DB_PATH


class Store:
    def __init__(self, path=DB_PATH):
        new = not os.path.exists(path)
        self.db = sqlite3.connect(path, timeout=15)
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        self.db.execute("""CREATE TABLE IF NOT EXISTS seen (
            tid INTEGER PRIMARY KEY, lastmod TEXT, first_seen TEXT, title TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS replied (
            tid INTEGER PRIMARY KEY, title TEXT, reply_text TEXT,
            created_at TEXT, result TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS daily (
            day TEXT PRIMARY KEY, count INTEGER)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS runtime (
            id INTEGER PRIMARY KEY CHECK(id=1), pid INTEGER, host TEXT, last_seen TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS notifs (
            rid INTEGER PRIMARY KEY, tid INTEGER, from_user TEXT, kind TEXT,
            processed_at TEXT, result TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS donations (
            tid INTEGER PRIMARY KEY, score INTEGER, amount INTEGER, created_at TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS kv (
            k TEXT PRIMARY KEY, v TEXT)""")
        self.db.commit()
        if new:
            log("已创建状态库 state.db")

    def close(self):
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    # ---- 单实例锁 ---------------------------------------------------------

    def acquire_lock(self, stale_after=120):
        """单实例锁。返回 (是否可以启动, 说明)。

        只在「同一台机器」上拦截重复实例 —— 跨机器没法可靠判断对方是否还活着,
        那种情况交给 filters.should_reply 里的站点侧去重兜底。
        """
        host = socket.gethostname()
        pid = os.getpid()
        row = self.db.execute(
            "SELECT pid, host, last_seen FROM runtime WHERE id=1").fetchone()
        if row:
            pid0, host0, last_seen = row
            fresh = False
            try:
                fresh = (datetime.now()
                         - datetime.fromisoformat(last_seen)).total_seconds() < stale_after
            except (TypeError, ValueError):
                fresh = False
            if fresh and host0 == host and pid0 and pid0 != pid:
                return False, ("本机已有实例在运行(PID %s,最后心跳 %s)"
                               % (pid0, last_seen))
        self.db.execute(
            "INSERT OR REPLACE INTO runtime(id,pid,host,last_seen) VALUES(1,?,?,?)",
            (pid, host, datetime.now().isoformat(timespec="seconds")))
        self.db.commit()
        return True, host

    def heartbeat(self):
        """每轮更新一次,表示「我还活着」。"""
        self.db.execute(
            "UPDATE runtime SET pid=?, host=?, last_seen=? WHERE id=1",
            (os.getpid(), socket.gethostname(),
             datetime.now().isoformat(timespec="seconds")))
        self.db.commit()

    # ---- seen -------------------------------------------------------------

    def mark_seen(self, tid, lastmod="", title=""):
        self.db.execute(
            "INSERT OR IGNORE INTO seen(tid,lastmod,first_seen,title) VALUES(?,?,?,?)",
            (tid, lastmod, datetime.now().isoformat(timespec="seconds"), title))
        self.db.commit()

    def unmark_seen(self, tid):
        """撤销「已见」标记,让该帖在下一轮重新进入候选(用于失败重试)。"""
        self.db.execute("DELETE FROM seen WHERE tid=?", (tid,))
        self.db.commit()

    def all_seen_ids(self):
        """一次取回全部已见 tid,避免逐条 SELECT(首次 bootstrap 要查上万次)。"""
        return {r[0] for r in self.db.execute("SELECT tid FROM seen")}

    def mark_seen_many(self, rows):
        """批量写入 + 单事务,用于首次 bootstrap。"""
        if not rows:
            return
        self.db.executemany(
            "INSERT OR IGNORE INTO seen(tid,lastmod,first_seen,title) VALUES(?,?,?,?)",
            rows)
        self.db.commit()

    def touch_seen(self, tid, lastmod):
        self.db.execute("UPDATE seen SET lastmod=? WHERE tid=?", (lastmod, tid))
        self.db.commit()

    def is_seen(self, tid):
        return self.db.execute("SELECT 1 FROM seen WHERE tid=?", (tid,)).fetchone() is not None

    def lastmod_of(self, tid):
        row = self.db.execute("SELECT lastmod FROM seen WHERE tid=?", (tid,)).fetchone()
        return row[0] if row else None

    def is_empty(self):
        return not self.db.execute("SELECT 1 FROM seen LIMIT 1").fetchone()

    # ---- replied ----------------------------------------------------------

    def has_replied(self, tid):
        """只认成功的记录 —— 失败的要留着重试,否则一次网络抖动就永久放弃这帖。"""
        row = self.db.execute("SELECT result FROM replied WHERE tid=?", (tid,)).fetchone()
        return bool(row) and str(row[0]).startswith("ok")

    def save_reply(self, tid, title, text, result):
        self.db.execute(
            "INSERT OR REPLACE INTO replied(tid,title,reply_text,created_at,result) VALUES(?,?,?,?,?)",
            (tid, title, text, datetime.now().isoformat(timespec="seconds"), result))
        self.db.commit()

    # ---- daily ------------------------------------------------------------

    def today_count(self):
        d = date.today().isoformat()
        row = self.db.execute("SELECT count FROM daily WHERE day=?", (d,)).fetchone()
        return row[0] if row else 0

    def incr_today(self):
        d = date.today().isoformat()
        self.db.execute("INSERT INTO daily(day,count) VALUES(?,1) "
                        "ON CONFLICT(day) DO UPDATE SET count=count+1", (d,))
        self.db.commit()

    # ---- notifs(通知处理记录,rid = 触发通知的回复 id) --------------------

    def has_notif(self, rid):
        return self.db.execute(
            "SELECT 1 FROM notifs WHERE rid=?", (rid,)).fetchone() is not None

    def save_notif(self, rid, tid, from_user, kind, result):
        self.db.execute(
            "INSERT OR REPLACE INTO notifs(rid,tid,from_user,kind,processed_at,result) "
            "VALUES(?,?,?,?,?,?)",
            (rid, tid, from_user, kind,
             datetime.now().isoformat(timespec="seconds"), result))
        self.db.commit()

    # ---- donations(只打赏模式:给帖子打赏的记录) --------------------------

    def has_donated(self, tid):
        """该帖是否已打赏过(避免重复打赏,站点也会拒,但本地先挡一道)。"""
        return self.db.execute(
            "SELECT 1 FROM donations WHERE tid=?", (tid,)).fetchone() is not None

    def save_donation(self, tid, score, amount):
        self.db.execute(
            "INSERT OR REPLACE INTO donations(tid,score,amount,created_at) "
            "VALUES(?,?,?,?)",
            (tid, int(score), int(amount),
             datetime.now().isoformat(timespec="seconds")))
        self.db.commit()

    def donations_today_count(self):
        d = date.today().isoformat()
        row = self.db.execute(
            "SELECT COUNT(*) FROM donations WHERE substr(created_at,1,10)=?",
            (d,)).fetchone()
        return row[0] if row else 0

    # ---- kv(杂项状态,如「今天是否已领过积分」) ---------------------------

    def get_kv(self, k, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def set_kv(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, str(v)))
        self.db.commit()

    def claimed_reward_today(self):
        """今天是否已领过每日积分(避免重复领,每天只领一次)。"""
        return self.get_kv("reward_claimed") == date.today().isoformat()

    def mark_reward_claimed(self):
        self.set_kv("reward_claimed", date.today().isoformat())

    def reward_tries_today(self):
        """今天已尝试领奖的次数 + 最近一次尝试时间(今天没试过则 (0, None))。

        领积分要走 Playwright 启动浏览器(十几秒起),开销远大于普通请求,不能每轮
        都试;但也不该「试一次就作罢」——登录/网络一旦抽风当天奖励就白丢了。所以
        把次数和时间记下来,由调用方按冷却时间补试。
        """
        if self.get_kv("reward_tries_date") != date.today().isoformat():
            return 0, None
        tries = int(self.get_kv("reward_tries") or 0)
        last = self.get_kv("reward_last_try")
        try:
            return tries, (datetime.fromisoformat(last) if last else None)
        except ValueError:
            return tries, None

    def mark_reward_try(self):
        tries, _ = self.reward_tries_today()
        self.set_kv("reward_tries_date", date.today().isoformat())
        self.set_kv("reward_tries", tries + 1)
        self.set_kv("reward_last_try",
                    datetime.now().isoformat(timespec="seconds"))

    # ---- 统计 -------------------------------------------------------------

    def stats(self):
        """给启动横幅/调试用的一行汇总。"""
        def one(sql):
            r = self.db.execute(sql).fetchone()
            return r[0] if r else 0
        return {
            "seen": one("SELECT COUNT(*) FROM seen"),
            "replied": one("SELECT COUNT(*) FROM replied WHERE result LIKE 'ok%'"),
            "today": self.today_count(),
        }
