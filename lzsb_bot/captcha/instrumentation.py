#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cap 第二层:instrumentation 行为检测。

服务端下发一段 deflate 压缩的 HTML+JS,在 sandbox iframe 里执行。它做两件事:

    a. 反自动化检测 —— 查 node:internal 调用栈、Playwright 泄漏变量哈希等,
       不通过则 return null;
    b. DOM 布局指纹  —— 往 DOM 插嵌套 div,靠 innerText 遍历求和,产出 4 个数字。

服务端只校验 (b) 的结果,所以这里**剥掉 (a)**、用 Node + 极简 DOM shim 真实执行 (b)。

关键细节
--------
检测层与计算层在**同一个 async IIFE** 内。早期想用 `{}` 整体替换整个 IIFE,
结果把 state 变量也清空了 → `failed_challenge`。正确做法是**裁剪**出计算层:

    const <state> = await (async function(){
        <反自动化检测, 失败则 return null>
        var a=..;var b=..;var c=..;var d=..;   ← 计算层从这里开始
        function ...(){}
        <运算>
        var <obj>={}; ...; return <obj>;      ← 到这里结束
    })();

DOM shim 的关键:`innerText` 必须是**与 children 分离的纯文本节点**。
原脚本靠"先删元素子节点、再读 innerText"拿节点自身的数值 ——
如果 innerText 是从 children 递归拼出来的,结果就全错了。
"""

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import zlib

from .pow import M  # noqa: F401  (保持包内导入一致性)

# Node 可执行文件候选:Windows / Termux(Android) / 通用 Linux 都覆盖。
# 顺序上先找确定存在的绝对路径,最后回退到 PATH 里的 node。
NODE_CANDIDATES = [
    r'C:\Users\user\.mytools\binaries\node\versions\22.22.2-3\node.exe',
    r'C:\Users\user\.mytools\binaries\node\versions\24.19.0\node.exe',
    r'D:\mytools\NodeTools\node.exe',
    '/data/data/com.termux/files/usr/bin/node',   # Termux(Android)
    '/usr/bin/node',
    '/usr/local/bin/node',
    '/opt/homebrew/bin/node',
    'node',
]

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:142.0) '
       'Gecko/20100101 Firefox/142.0')

# 极简 DOM:只需支持脚本用到的 appendChild / removeChild / children /
# lastElementChild / innerText / parentNode。
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


def find_node():
    """定位 node 可执行文件;找不到返回 None。"""
    for p in NODE_CANDIDATES:
        if p == 'node':
            return shutil.which('node') or 'node'
        if os.path.exists(p):
            return p
    return None


def _extract_calc_body(script):
    """从脚本里抽出「计算层」代码,绕过检测层。返回 (代码, state 变量名)。"""
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

    node = find_node()
    if not node:
        raise RuntimeError('找不到 node,无法执行 instrumentation(请安装 nodejs)')

    mn = re.search(r'nonce\s*:\s*"([^"]+)"', script)
    nonce = mn.group(1) if mn else ''

    calc, _obj = _extract_calc_body(script)
    if not calc:
        raise RuntimeError('未能从 instrumentation 中抽取计算层代码')

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
