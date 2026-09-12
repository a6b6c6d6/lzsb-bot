#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cap 第三层 + 完整流程:challenge → PoW → instrumentation → redeem。

注意:redeem 请求**必须带浏览器特征头**(Origin/Referer/Sec-Fetch-*),
否则会被 Cloudflare 以 error 1010 拦下。
"""

import json
import urllib.error
import urllib.request

from .instrumentation import _UA
from .pow import solve_challenge
from .instrumentation import solve_instrumentation


def challenge_headers(referer='https://linux.sb/login'):
    return {
        'Origin': 'https://linux.sb',
        'Referer': referer or 'https://linux.sb/login',
        'Accept': '*/*',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-site',
    }


def get_cap_token(http, endpoint, referer='https://linux.sb/login', node_path=None):
    """走完 challenge -> PoW -> instrumentation -> redeem,返回 cap_token。

    http 需提供 `post(url, data, extra_headers=...)` 与 `.opener`。
    node_path —— config 里的 node 可执行文件路径(可选)。
    """
    ep = endpoint if endpoint.endswith('/') else endpoint + '/'
    hdrs = challenge_headers(referer)

    # --- 1. challenge ---
    code, body, _ = http.post(ep + 'challenge', {}, extra_headers=hdrs)
    if code != 200:
        raise RuntimeError('challenge 失败: HTTP %s %s' % (code, body[:200]))
    resp = json.loads(body)
    ch, token = resp['challenge'], resp['token']

    # --- 2. PoW ---
    sols = solve_challenge(token, ch)

    # --- 3. instrumentation ---
    instr = None
    if resp.get('instrumentation'):
        instr = solve_instrumentation(resp['instrumentation'], node_path=node_path)

    # --- 4. redeem ---
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
            **challenge_headers(referer),
        })
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
