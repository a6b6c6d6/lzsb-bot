#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cap 第二层:instrumentation 行为检测(2026-10-26 新版协议)。

服务端下发一段 deflate 压缩的 JS,在沙箱 iframe 里执行,最后 postMessage 回传:

    result = {i: <nonce>, state: <DOM 布局指纹>, p: <浏览器探针>, ts}

服务端同时校验 state 和 p —— **p 缺失直接 403 instr_probe_missing**
(2026-10-26 服务端升级后旧版 {i, state, ts} 格式已被拒)。

脚本分三层:
    a. 反自动化检测 —— Node 栈特征(node:internal/[eval])、
       Object.prototype.toString 哈希、Playwright/Puppeteer 全局变量黑名单、
       WebGL/canvas 原生函数校验……任何一项不过就 return null 或
       postMessage(blocked)。在 Node 里这些必挂,必须剥掉;
    b. DOM 布局指纹(计算层)—— 嵌套 div + innerText 求值,产出 4 个数字。
       用 Node + 极简 DOM shim 真实执行;
    c. 探针层 —— 十余个 c() 包裹的采集函数(ua / oscpu / webdriver /
       productSub / fontWidths / outerWH / screen / plugins …),
       结果汇成 p,随 result 一起回传。

做法:把承载 (a)+(b) 的 async IIFE 替换成只含 (b) 的同步 IIFE,
脚本其余部分(含 (c) 与最终 postMessage)原样执行,配一个
"Firefox on Windows"环境的 shim。

关键细节
--------
- Node 22 的 navigator 是 getter-only 访问器,**直接赋值会被静默忽略**,
  必须用 Object.defineProperty 覆盖(旧版只算 state、不读 UA 所以没暴露)。
- innerText 必须是与 children 分离的纯文本节点:原脚本靠"先删子节点、
  再读 innerText"拿节点自身数值,若 innerText 由 children 递归拼出则全错。
- fontWidths 探针读 getBoundingClientRect().width,shim 按
  (fontFamily + 文本) 做确定性哈希,给出互不相同且量级合理的伪宽度。
- navigator 里**不要**补 WebGLRenderingContext / permissions 之类:
  探针对"取不到的属性"会抛异常并返回 true,与真实浏览器的原生函数一致;
  用自定义 JS 函数去补反而会返回 false,看起来像被篡改。
- screen 取物理像素、outerWH 取 DPI 缩放后的 CSS 像素,与真实浏览器
  在缩放屏上的报告一致;取不到时回退常见笔记本分辨率。
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

# Node 可执行文件探测顺序:config 指定路径 → 常见安装位置 → PATH。
# 全部走通用位置,不依赖任何个人环境;装在非标准位置的用户在 config.json
# 里填 node_path 即可。
NODE_CANDIDATES = [
    '/data/data/com.termux/files/usr/bin/node',   # Termux(Android)
    '/usr/bin/node',
    '/usr/local/bin/node',
    '/opt/homebrew/bin/node',
]


def find_node(extra=None):
    """定位 node 可执行文件;找不到返回 None。"""
    if extra and os.path.isfile(extra):
        return extra
    for p in NODE_CANDIDATES:
        if os.path.isfile(p):
            return p
    # Windows 常见安装位置(PATH 里往往没有 node)
    for env_key, sub in (('ProgramFiles', r'\nodejs\node.exe'),
                         ('LOCALAPPDATA', r'\Programs\nodejs\node.exe'),
                         ('ProgramFiles(x86)', r'\nodejs\node.exe')):
        base = os.environ.get(env_key)
        if base and os.path.isfile(base + sub):
            return base + sub
    return shutil.which('node') or 'node'


_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) rv:142.0 '
       'Gecko/20100101 Firefox/142.0')

# 屏幕尺寸缓存:(screen_w, screen_h, outer_w, outer_h)
_SCREEN_CACHE = None


def _screen_metrics():
    """取本机真实屏幕尺寸,失败给常见笔记本值。

    screen 用物理像素(设了 DPI 感知之后 GetSystemMetrics 拿到的),
    outerWH 用未感知时的 CSS 像素 —— 与真实浏览器在 150% 缩放的
    2560x1600 屏上的报告一致(外层窗口不可能大于屏幕)。
    """
    global _SCREEN_CACHE
    if _SCREEN_CACHE:
        return _SCREEN_CACHE
    try:
        import ctypes
        u = ctypes.windll.user32
        css_w, css_h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        try:  # 本进程之后再做 GetSystemMetrics 拿到的就是物理像素
            u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        phys_w, phys_h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        vals = [int(v) for v in (phys_w, phys_h, css_w, css_h)]
        if all(v > 0 for v in vals):
            _SCREEN_CACHE = tuple(vals)
            return _SCREEN_CACHE
    except Exception:
        pass
    _SCREEN_CACHE = (1920, 1080, 1920, 1080)
    return _SCREEN_CACHE


# 极简浏览器环境:只需支持脚本用到的 appendChild / removeChild / children /
# lastElementChild / innerText / textContent / getBoundingClientRect,
# 外加探针层会读的 navigator / screen / outerWidth / outerHeight。
_DOM_TEMPLATE = """
class El {
  constructor(tag){ this.tagName=tag; this.style={}; this.children=[]; this.parentNode=null; this._text=''; }
  appendChild(c){ c.parentNode=this; this.children.push(c); return c; }
  removeChild(c){ const i=this.children.indexOf(c); if(i>=0) this.children.splice(i,1); c.parentNode=null; return c; }
  get lastElementChild(){ return this.children.length? this.children[this.children.length-1] : null; }
  set innerText(v){ this._text = String(v); }
  get innerText(){ return this._text; }
  getBoundingClientRect(){
    var fam = (this.style && this.style.fontFamily) || '';
    var txt = (this.textContent !== undefined && this.textContent !== '') ? this.textContent : this._text;
    var h = 2166136261;
    var s = fam + '|' + String(txt);
    for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = (h * 16777619) >>> 0; }
    var w = 500 + (h % 600);
    return { width: w + 0.37, height: 90, top: 0, left: 0, right: w, bottom: 90 };
  }
}
let captured = null;
globalThis.window = globalThis;
globalThis.self = globalThis;
globalThis.parent = { postMessage: (m)=>{ captured = m; } };
Object.defineProperty(globalThis, 'navigator', { value: {
  userAgent: __UA__,
  oscpu: 'Windows NT 10.0; Win64; x64',
  webdriver: false,
  productSub: '20030107',
  pdfViewerEnabled: true,
  plugins: { length: 5 }
}, configurable: true, writable: true });
globalThis.screen = { width: __SCREEN_W__, height: __SCREEN_H__, isExtended: false };
globalThis.outerWidth = __OUTER_W__;
globalThis.outerHeight = __OUTER_H__;
globalThis.mozInnerScreenX = 0;
globalThis.document = { createElement: function(t){ return new El(t); }, body: new El('body') };
"""


def _build_dom_shim():
    sw, sh, ow, oh = _screen_metrics()
    return (_DOM_TEMPLATE
            .replace('__UA__', json.dumps(_UA))
            .replace('__SCREEN_W__', str(sw))
            .replace('__SCREEN_H__', str(sh))
            .replace('__OUTER_W__', str(ow))
            .replace('__OUTER_H__', str(oh)))


# Cap 的 instrumentation 会定期更换混淆方式,这里只依赖稳定的结构:
#   1. 计算层以 4 个数字状态变量开始(旧版分号分隔,新版逗号声明);
#   2. 计算层位于外层 async IIFE 内,IIFE 以 `})();` 收尾;
#   3. 探针层与最终 postMessage 在 IIFE 之后,原样保留执行。
_VARS_OLD_RE = re.compile(
    r'(?:var\s+\w+\s*=\s*\d+\s*;\s*){4,}')
_VARS_NEW_RE = re.compile(
    r'var\s+\w+\s*=\s*\d+(?:\s*,\s*\w+\s*=\s*\d+){3,}\s*;')
_IIFE_OPEN_RE = re.compile(r'\(async\s+function\s*\(\)\s*\{')


def _find_calc_start(script):
    """定位计算层(4 个数字状态变量)起点;找不到返回 -1。"""
    matches = list(_VARS_OLD_RE.finditer(script))
    matches.extend(_VARS_NEW_RE.finditer(script))
    if not matches:
        return -1
    return max(matches, key=lambda m: m.start()).start()


def _strip_anti_automation(script):
    """把 `const C=await(async function(){<反自动化检测 + 计算层>})();`
    替换为只含计算层的同步 IIFE,脚本其余部分原样保留。

    返回改造后的完整脚本;结构不认识返回 None(调用方应报错,
    而不是悄悄交回旧格式 —— 服务端已经拒收旧格式了)。
    """
    start = _find_calc_start(script)
    if start < 0:
        return None
    end = script.find('})();', start)
    if end < 0:
        return None
    m = None
    for m in _IIFE_OPEN_RE.finditer(script[:start]):
        pass                      # 取计算层之前最后一个 async IIFE 开头
    if not m:
        return None
    op = m.start()
    pre = script[:op].rstrip()    # 连同前面的 await 一起去掉(可有空白)
    aw = len(pre) - 5 if pre.endswith('await') else op
    calc = script[start:end].rstrip()
    # 注意:原 IIFE 收尾的 `})();` 整段(含分号)被替换,补回的分号不能少 ——
    # 少了它,后面的 `if(!C||...)` 会和 `})()` 挤在同一行,ASI 不生效,直接语法错误。
    return script[:aw] + '(function(){' + calc + '})();\n' + script[end + 5:]


def solve_instrumentation(instr_b64, timeout=25, node_path=None):
    """解压 instrumentation 脚本,在 Node + 浏览器 shim 里跑出完整 result。

    返回服务端期望的 instr 对象:{i, state, p, ts}。
    node_path —— config.json 里的 node 可执行文件路径(PATH 探测不到时用)
    """
    raw = base64.b64decode(instr_b64)
    script = zlib.decompress(raw, -15).decode('utf-8', 'replace')

    node = find_node(node_path)
    if not node:
        raise RuntimeError('找不到 node,无法执行 instrumentation(请安装 nodejs)')

    stripped = _strip_anti_automation(script)
    if stripped is None:
        raise RuntimeError('未能从 instrumentation 中定位计算层'
                           '(服务端可能又更换了混淆方式,需要更新提取规则)')

    prog = ('(async function(){\n'
            + stripped + '\n'
            + 'if (typeof window.onload !== "function") {\n'
            + '  throw new Error("onload not installed");\n'
            + '}\n'
            + 'await window.onload();\n'
            + 'if (!captured || !captured.result) {\n'
            + '  throw new Error("no result posted");\n'
            + '}\n'
            + 'console.log(JSON.stringify(captured.result));\n'
            + '})().catch(function(e){\n'
            + '  console.error("INSTR_ERR", e && e.stack ? e.stack : e);\n'
            + '  process.exit(1);\n'
            + '});\n')

    fd, path = tempfile.mkstemp(suffix='.js', prefix='cap_instr_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(_build_dom_shim() + '\n' + prog)
        r = subprocess.run([node, path], capture_output=True, text=True,
                           timeout=timeout, encoding='utf-8', errors='replace')
        out = (r.stdout or '').strip()
        if not out:
            raise RuntimeError('instrumentation 执行无输出: %s'
                               % (r.stderr or '')[:300])
        result = json.loads(out.splitlines()[-1])
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    i = result.get('i')
    state = result.get('state')
    probes = result.get('p')
    if not isinstance(i, str) or not i:
        raise RuntimeError('instrumentation 未产出 nonce: %s' % out[:200])
    if not isinstance(state, dict) or not state:
        raise RuntimeError('instrumentation 产出的 state 为空: %s' % out[:200])
    if not isinstance(probes, dict) or not probes:
        raise RuntimeError('instrumentation 未产出探针 p'
                           '(服务端 2026-10-26 起强制要求): %s' % out[:200])
    return result
