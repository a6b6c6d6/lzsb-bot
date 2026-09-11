#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cap 人机验证(linux.sb 登录用)求解器
=====================================

linux.sb 的登录表单挂了自托管的 Cap(https://cap.linux.sb)验证,分三层:

1. **PoW 挑战** —— 服务端下发 {c, s, d},要求解 c 个子挑战。
   每个子挑战的 (salt, target) 由 `derive()` 生成:
     - 种子:FNV-1a(offset basis 2166136261,乘数 16777619)
     - 输出:xorshift32(13,17,5),每步取 8 位 hex,截断到指定长度
   然后暴力找 nonce,使 sha256(salt + str(nonce)) 以 target 开头。
   (算法取自 cap 官方 wasm/src/rust/src/lib.rs 与 widget 源码,已实测复现)

2. **instrumentation 行为检测** —— 服务端下发一段 deflate 压缩的 HTML+JS,
   在 sandbox iframe 里执行。它做两件事:
     a. 反自动化检测(查 node:internal 栈、Playwright 泄漏变量等),不通过则返回 null;
     b. DOM 布局指纹计算(往 DOM 插嵌套 div,靠 innerText 遍历求和),产出 4 个数字。
   服务端只校验 (b) 的结果,所以这里剥掉 (a)、用 Node + 极简 DOM shim 真实执行 (b)。

3. **redeem** —— 提交 {token, solutions, instr},换取 cap_token,随登录表单提交。

注意:redeem 请求必须带浏览器特征头(Origin/Referer/Sec-Fetch-*),
否则会被 Cloudflare 以 error 1010 拦下。
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import zlib

M = 0xFFFFFFFF

# ---------------------------------------------------------------------------
# 1) PoW
# ---------------------------------------------------------------------------

def fnv1a32(s):
    """JS: let t=2166136261; for(...) t^=c, t+=(t<<1)+(t<<4)+(t<<7)+(t<<8)+(t<<24)"""
    h = 2166136261
    for ch in s:
        h = ((h ^ ord(ch)) * 16777619) & M
    return h


def derive(seed, length):
    """JS 的 d(e,t):FNV-1a 播种 + xorshift32,输出 length 个 hex 字符。"""
    x = fnv1a32(seed)
    buf, total = [], 0
    while total < length:
        x ^= (x << 13) & M
        x ^= x >> 17
        x ^= (x << 5) & M
        x &= M
        buf.append(format(x, '08x'))
        total += 8
    return ''.join(buf)[:length]


def solve_pow(salt, target, limit=200_000_000):
    """nonce 从 0 起按十进制字符串拼接;返回第一个满足前缀匹配的 nonce。"""
    n = 0
    while n < limit:
        if hashlib.sha256((salt + str(n)).encode()).hexdigest().startswith(target):
            return n
        n += 1
    return None


def solve_challenge(token, ch):
    """解出全部 c 个子挑战,返回 solutions 列表。i 从 1 开始(对齐 JS 的 e++)。"""
    sols = []
    for i in range(ch['c']):
        salt = derive('%s%d' % (token, i + 1), ch['s'])
        target = derive('%s%dd' % (token, i + 1), ch['d'])
        n = solve_pow(salt, target)
        if n is None:
            raise RuntimeError('PoW 求解失败(第 %d 个)' % i)
        sols.append(n)
    return sols


# ---------------------------------------------------------------------------
# 2) instrumentation
# ---------------------------------------------------------------------------

NODE_CANDIDATES = [
    r'C:\Users\user\.mytools\binaries\node\versions\22.22.2-3\node.exe',
    r'C:\Users\user\.mytools\binaries\node\versions\24.19.0\node.exe',
    r'D:\mytools\NodeTools\node.exe',
    'node',
]

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:142.0) '
       'Gecko/20100101 Firefox/142.0')

# 极简 DOM:只需支持脚本用到的 appendChild / removeChild / children /
# lastElementChild / innerText / parentNode。innerText 是纯文本节点,
# 与 children(元素节点)分离 —— 这正是原脚本能靠"删元素后读 innerText"计数的原因。
DOM_SHIM = """
class El {
  constructor(tag){ this.tagName=tag; this.style={}; this.children=[]; this.parentNode=null; this._text=''; }
  appendChild(c){ c.parentNode=this; this.children.push(c); return c; }
  removeChild(c){ const i=this.children.indexOf(c); if(i>=0) this.children.splice(i,1); c.parentNode=null; return c; }
  get lastElementChild(){ return this.children.length? this.children[this.children.length-1] : null; }
  set innerText(v){ this._text = String(v); }
  get innerText(){ return this._text; }
}
let captured = null;
globalThis.window = globalThis;
globalThis.self = globalThis;
globalThis.parent = { postMessage: (m)=>{ captured = m; } };
globalThis.navigator = { userAgent: %s };
globalThis.document = { createElement:(t)=>new El(t), body: new El('body') };
""" % json.dumps(_UA)

_VARS_RE = re.compile(r'var \w+=\d+;var \w+=\d+;var \w+=\d+;var \w+=\d+;')


def _find_node():
    for p in NODE_CANDIDATES:
        if p == 'node':
            return p
        if os.path.exists(p):
            return p
    return None


def _extract_calc_body(script):
    """从脚本里抽出"计算层"代码。

    结构上,检测层与计算层在同一个 async IIFE 内:
        const <state> = await (async function(){
            <反自动化检测, 失败则 return null>
            var a=..;var b=..;var c=..;var d=..;      ← 计算层从这里开始
            function ...(){}
            <运算>
            var <obj>={}; ...; return <obj>;         ← 到这里结束
        })();
    所以只需裁出 (变量声明 .. return <obj>;) 这一段,即可绕过检测层。
    """
    m = _VARS_RE.search(script)
    if not m:
        return None, None
    start = m.start()
    i_obj = script.find('={};', start)
    if i_obj < 0:
        return None, None
    i_ret = script.find('return ', i_obj)
    if i_ret < 0:
        return None, None
    m2 = re.match(r'return (\w+);', script[i_ret:])
    if not m2:
        return None, None
    end = i_ret + m2.end()
    return script[start:end], m2.group(1)


def solve_instrumentation(instr_b64, timeout=25):
    """解压 instrumentation 脚本,在 Node + DOM shim 里跑出 state。

    返回服务端期望的 instr 对象:{i, state:{...}, ts}
    """
    import time as _time

    raw = base64.b64decode(instr_b64)
    script = zlib.decompress(raw, -15).decode('utf-8', 'replace')

    node = _find_node()
    if not node:
        raise RuntimeError('找不到 node.exe,无法执行 instrumentation')

    mn = re.search(r'nonce\s*:\s*"([^"]+)"', script)
    nonce = mn.group(1) if mn else ''

    calc, _obj = _extract_calc_body(script)
    if not calc:
        raise RuntimeError('未能从 instrumentation 中抽取计算层代码')

    # DOM 行为需与浏览器一致:innerText 是文本节点,与 children(元素节点)分离,
    # 这样"先删元素子节点、再读 innerText"才能拿到节点自身的数值。
    prog = (DOM_SHIM + '\n'
            + 'function __calc(){\n' + calc + '\n}\n'
            + 'const st = __calc();\n'
            + 'console.log(JSON.stringify(st));\n')

    fd, path = tempfile.mkstemp(suffix='.js', prefix='cap_instr_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(prog)
        r = subprocess.run([node, path], capture_output=True, text=True,
                           timeout=timeout, encoding='utf-8', errors='replace')
        out = (r.stdout or '').strip()
        if not out:
            raise RuntimeError('instrumentation 执行无输出: %s' % (r.stderr or '')[:300])
        state = json.loads(out.splitlines()[-1])
        if not isinstance(state, dict) or not state:
            raise RuntimeError('instrumentation 产出的 state 为空: %s' % out[:200])
        return {'i': nonce, 'state': state, 'ts': int(_time.time() * 1000)}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 3) 完整流程
# ---------------------------------------------------------------------------

CHALLENGE_HEADERS = {
    'Origin': 'https://linux.sb',
    'Referer': 'https://linux.sb/login',
    'Accept': '*/*',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-site',
}


def get_cap_token(http, endpoint, referer='https://linux.sb/login'):
    """走完 challenge -> PoW -> instrumentation -> redeem,返回 cap_token。

    http 需提供 post_json(url, obj, headers) 风格的能力,这里直接用 urllib 组装。
    """
    import urllib.request

    ep = endpoint if endpoint.endswith('/') else endpoint + '/'
    hdrs = dict(CHALLENGE_HEADERS)
    if referer:
        hdrs['Referer'] = referer

    # --- challenge ---
    code, body, _ = http.post(ep + 'challenge', {}, extra_headers=hdrs)
    if code != 200:
        raise RuntimeError('challenge 失败: HTTP %s %s' % (code, body[:200]))
    resp = json.loads(body)
    ch, token = resp['challenge'], resp['token']

    # --- PoW ---
    sols = solve_challenge(token, ch)

    # --- instrumentation ---
    instr = None
    if resp.get('instrumentation'):
        instr = solve_instrumentation(resp['instrumentation'])

    # --- redeem ---
    payload = {'token': token, 'solutions': sols}
    if instr:
        payload['instr'] = instr
    req = urllib.request.Request(
        ep + 'redeem', data=json.dumps(payload).encode('utf-8'), method='POST',
        headers={
            'User-Agent': _UA,
            'Content-Type': 'application/json',
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Origin': 'https://linux.sb',
            'Referer': referer or 'https://linux.sb/login',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-site',
        })
    import urllib.error
    try:
        r = http.opener.open(req, timeout=45)
        out = r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        raise RuntimeError('redeem 失败: HTTP %s %s'
                           % (e.code, e.read().decode('utf-8', 'replace')[:300]))
    j = json.loads(out)
    if not j.get('success'):
        raise RuntimeError('redeem 被拒: %s' % out[:300])
    return j.get('token')
