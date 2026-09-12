#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cap 第一层:PoW 挑战。

服务端下发 {c, s, d},要求解 c 个子挑战。每个子挑战的 (salt, target)
**不是直接给的**,而是用 FNV-1a 播种 + xorshift32 从一个 token 推导出来的。

算法取自 cap 官方 wasm/src/rust/src/lib.rs 与 widget 源码,已实测复现。

JS 原版:
    let t=2166136261;
    for (const c of s) { t ^= c; t += (t<<1)+(t<<4)+(t<<7)+(t<<8)+(t<<24); }
    // 等价于 t = (t ^ c) * 16777619 (mod 2^32)
"""

import hashlib

M = 0xFFFFFFFF


def fnv1a32(s):
    """FNV-1a 32 位哈希(offset basis 2166136261,乘数 16777619)。"""
    h = 2166136261
    for ch in s:
        h = ((h ^ ord(ch)) * 16777619) & M
    return h


def derive(seed, length):
    """JS 的 d(e,t):FNV-1a 播种 + xorshift32(13,17,5),输出 length 个 hex 字符。"""
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
    """暴力找 nonce,使 sha256(salt + str(nonce)) 以 target 开头。

    注意 nonce 是**十进制字符串**拼接(不是 hex),与 JS 的 `salt + n` 隐式转换一致。
    """
    n = 0
    while n < limit:
        if hashlib.sha256((salt + str(n)).encode()).hexdigest().startswith(target):
            return n
        n += 1
    return None


def solve_challenge(token, ch):
    """解出全部 c 个子挑战,返回 solutions 列表。

    i 从 1 开始 —— 对齐 JS 里 `i++` 先自增再使用的写法。
    """
    sols = []
    for i in range(ch['c']):
        salt = derive('%s%d' % (token, i + 1), ch['s'])
        target = derive('%s%dd' % (token, i + 1), ch['d'])
        n = solve_pow(salt, target)
        if n is None:
            raise RuntimeError('PoW 求解失败(第 %d 个)' % i)
        sols.append(n)
    return sols
