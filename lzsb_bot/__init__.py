#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
linux.sb(烧饼社区)自动回帖机器人
================================

模块结构
--------
    lzsb_bot/
        __main__.py     命令行入口(python -m lzsb_bot)
        paths.py        运行路径与全局常量
        log.py          日志
        config.py       配置加载与校验
        http.py         带 Cookie/代理/重试的 HTTP 客户端
        store.py        SQLite 去重库(seen / replied / daily / runtime)
        html.py         HTML 解析工具
        site.py         站点交互(登录 / 抓帖 / 发帖)
        llm.py          OpenAI 兼容接口调用(多 provider 自动切换)
        filters.py      回复前的过滤规则
        prompt.py       提示词与用户提示词拼装
        bot.py          主循环编排
        captcha/        Cap 人机验证求解
        search/         联网搜索(smart / multi / bing 三档)

只依赖 Python 标准库;captcha 的 DOM 指纹计算需要系统里有 node。
"""

__version__ = "2.0.0"
__all__ = ["__version__"]
