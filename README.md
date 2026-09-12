# lzsb-bot —— linux.sb 自动回帖机器人

在烧饼社区(linux.sb)上,每 60 秒检测一次新帖,自动生成并发布一条中文回帖。

- 零第三方依赖,只用 Python 标准库(Cap 验证码的 DOM 指纹计算需要 Node)
- 自动登录、自动续期,不用手动维护 Cookie
- 默认 **只生成不提交**(dry-run),确认质量后再开启真实发布
- 联网搜索三档可切(`search_mode: 1/2/3`),AI 接口多路备份、自动降级

---

## 一、快速开始

1. 双击 `启动机器人.bat`(首次会自动复制出 `config.json` 和 `llm.json`)
2. 用记事本填两个文件:
   - `config.json` —— `username` / `password`(bot 专用账号,别用主账号)
   - `llm.json` —— AI 接口列表,每个都必须显式填 `base_url` / `api_key` / `model`
3. 再次双击 `启动机器人.bat`
4. 观察日志里生成的回复,满意后把 `config.json` 的 `dry_run` 改成 `false`
5. 按 `Ctrl+C` 停止

命令行方式:

```
python -m lzsb_bot              正常循环运行
python -m lzsb_bot --once       只跑一轮就退出(调试用)
python -m lzsb_bot --dry        强制只读,不提交
python -m lzsb_bot --login-only 只验证登录能不能成功
python -m lzsb_bot --search 关键词 [--mode 1|2|3]   只测联网搜索
```

(旧的 `python bot.py` 仍然可用,它只是个兼容入口)

## 二、运行前必须确认

- **代理必须开着**。linux.sb 直连超时,`config.json` 的 `proxy` 要指向你 Clash 的
  HTTP 端口(默认 `http://127.0.0.1:7897`)。
- **用 bot 专用账号**。不要复用主账号,主账号的登录凭证泄露风险太高。
- **AI 接口必须填 model**。程序不会自己去猜或列模型列表,`model` 留空的接口会被
  直接忽略并打警告。

## 三、两个配置文件

### config.json(站点与搜索)

| 字段 | 说明 |
|---|---|
| `proxy` | Clash HTTP 代理地址,**必填** |
| `username` / `password` | bot 账号 |
| `bot_username` | bot 的显示名,用于跨端去重(手机/电脑同时跑不会重复回帖)。留空取 `username` |
| `poll_interval` | 轮询间隔,秒,默认 60 |
| `reply_delay_min/max` | 检测到新帖后随机等待的秒数,降低机器味 |
| `daily_limit` | 每日最多回复条数,默认 20 |
| `deny_forums` | 不回复的板块,默认 `[2, 8]`(福利放送、我要推广) |
| `deny_keywords` | 标题命中即跳过,滤掉发卡/赠送/兑换类帖 |
| `skip_gacha` | 站点给发卡贴打了自带标记,`true` = 一律不回(推荐) |
| `context_scope` | `op` = 用楼主在本帖的全部发言做上下文(默认);`first` = 只用首楼 |
| `donate_enabled` | 回帖成功后是否自动给楼主打赏(同时点赞),默认开 |
| `donate_amount` | 每次打赏积分数,站方限 1~99,默认 1。账号注册需满 3 天才能打赏 |
| `sync_replies_pages` | 启动时从个人主页「回复」标签页同步几页站点已有回复到本地(0=关闭,默认 2)。跨端防重复的第一道保险 |
| `notify_reply_enabled` | 有人回复/提及 bot 时自动去接话(类似"水他"),默认开 |
| `notify_max_per_tick` | 每轮最多接话几条,默认 2;计入每日上限 |
| `bootstrap_threshold` | 防挖坟安全阀:单轮未见过帖超过该数 → 判定首次运行/长期停摆,只标记不回复 |
| `max_per_tick` | 防爆量安全阀:单轮最多处理 N 个新帖 |
| `dry_run` | `true` = 只生成不提交 |

### 联网搜索

| 字段 | 说明 |
|---|---|
| `enable_search` | 是否联网,默认开 |
| `search_mode` | **填 1 / 2 / 3**:见下表 |
| `search_topk` | 每次搜索取几条,默认 5 |
| `search_timeout` | 单源超时,秒 |
| `search_proxy` | 留空 = 用 `proxy` |

| 模式 | 名称 | 来源与逻辑 |
|---|---|---|
| `1`(默认) | 智能路由 | 按查询语言分流:中文 → GoogleNews+Bing 并行、HN 垫底;英文 → HN+StackOverflow+GitHub 技术源优先。搜索前先用 AI 从帖子里提炼专有名词(产品名/版本号),拒绝泛词 |
| `2` | 多源聚合 | 只打官方 JSON/RSS 接口(GoogleNews + StackOverflow + GitHub + HN):无改版、无反爬、无广告,缺点是中文生活类话题覆盖弱 |
| `3` | Bing 单源 | Bing 直连,通用兜底,结果偏杂、SEO 广告多 |

高级:`search_sources` 可显式指定来源列表覆盖模式选择,可选
`google_news` / `bing` / `hn` / `stackoverflow` / `github`。

### llm.json(AI 接口,独立文件)

```json
{
  "providers": [
    { "name": "主接口",  "base_url": "https://中转站A/v1", "api_key": "sk-xxx", "model": "deepseek-v4-flash" },
    { "name": "备用接口", "base_url": "https://中转站B/v1", "api_key": "sk-yyy", "model": "ministral-3" }
  ],
  "max_tokens": 2400
}
```

- **按数组顺序依次使用**:第 1 个限速/5xx/返回空内容时自动切第 2 个,以此类推
- `model` **必填且显式写出**,程序不猜、不列模型列表;填错的接口直接被忽略
- 中文回帖 1 字 ≈ 1~2 token,`max_tokens` 建议 2400 起,否则句子会被截断在半句
- 该文件含 API Key,已在 `.gitignore` 中,不会进仓库
- 没有这个文件时回退用 `config.json` 里的 `llm` 段(兼容旧配置)

## 四、模块结构

```
lzsb_bot/
├── __main__.py        命令行入口(--once/--dry/--login-only/--search)
├── bot.py             主循环编排:检测→过滤→搜索→生成→提交
├── config.py          配置加载(config.json + llm.json)、搜索模式归一化
├── paths.py           路径与常量
├── log.py             日志(打屏 + 文件)
├── http.py            HttpClient:强制代理 + Cookie + 网络层自动重试
├── site.py            站点交互:登录/发现新帖/抓帖子/发帖(实时解析表单)
├── html.py            纯正则 HTML 解析(楼层/表单/验证码/算术题)
├── store.py           SQLite 状态库:去重/回复记录/每日计数/单实例心跳
├── filters.py         回复前过滤规则(板块黑名单/发卡贴/关键词/跨端去重)
├── llm.py             OpenAI 兼容调用,多 provider 顺序降级
├── prompt.py          提示词:系统提示词/关键词提炼/上下文拼装
├── captcha/           Cap 人机验证三层破解
│   ├── pow.py             PoW:FNV-1a 播种 + xorshift32 + SHA-256 暴力
│   ├── instrumentation.py 行为检测层:剥反自动化、Node 执行 DOM 指纹
│   └── solver.py          challenge → PoW → instrumentation → redeem 全流程
└── search/            联网搜索
    ├── base.py            对外入口 search(query, mode=1|2|3)
    ├── engine.py          智能路由/多源聚合/Bing 单源三种引擎
    ├── sources.py         五个来源实现(GoogleNews/Bing/HN/SO/GitHub)
    └── _http.py           底层 GET、URL 解壳、垃圾结果过滤
```

## 五、工作原理

1. **登录** —— 账号密码 + Cap 人机验证(见 `lzsb_bot/captcha/`,三层:PoW、
   instrumentation 行为检测、redeem)。登录被「邮箱验证」拦截时,在浏览器手动
   登录一次完成账号级验证即可,之后程序化登录畅通。
2. **跨端对账** —— 启动时从个人主页「回复」标签页(公开页面)拉取 bot 在站点上
   已有的回复,标记进本地库。手机/电脑两端各有一份 state.db 也能对齐。
3. **发现新帖** —— 扫各板块列表页(`<ul class="post-list">`,实时),不用
   sitemap(实测滞后 6~10 帖,靠它会漏帖)。与本地 `state.db` 比对找出新帖。
4. **过滤** —— 本地已回过 → 站点侧跨端去重(帖子里已出现 bot 的发言就跳过,
   与启动对账互为双保险)→ 已锁帖 → 发卡贴 → 板块黑名单
   → 标题关键词。
5. **抓内容** —— 解析标题、板块、楼主与 **楼主在本帖的全部发言**。
6. **联网搜索** —— AI 提炼专有名词关键词 → 按所选模式搜索 → 垃圾结果过滤
   → 作为背景资料拼进提示词。
7. **生成回复** —— 调 OpenAI 兼容接口,providers 顺序降级;输出被截断会告警。
8. **打赏** —— 回帖成功后自动给楼主打赏(点赞 + 积分,`donate_amount`);
   失败只告警不重试(防双倍),不影响回帖结果。
9. **通知接话** —— 每轮检查「我的通知」(`?tab=notifications`),有人回复/提及
   bot 就以 @对方 的方式针对其发言自动接话;按对方回复 id 去重,系统类通知
   (投币/红包)跳过;计入每日上限。
10. **提交** —— 每次发帖前实时从帖子页解析表单 action、`_csrf`、验证码挑战与
   PoW 参数(不硬编码端点),`POST /reply_edit`;网络类失败自动重试并撤销
   「已见」标记,业务类拒绝不重试。
11. **防挖坟双保险** —— 存量帖 bootstrap 批量事务(秒级);单轮新帖数超阈值只
   标记不回复;单轮限量;随机延迟;每日上限。

## 六、注意事项

- 首次运行会把**所有存量帖子**标记为已见,不会去回复历史帖。
- 单实例锁:同一台机器双开会被拦截(心跳 2 分钟过期,异常退出后稍等即可重启);
  手机 + 电脑两端同时跑是允许的,靠站点侧跨端去重防重复。
- 部分回复会显示「等待楼主确认是否为有价值讨论」,这是站点侧的审核机制,不是失败。
- 若某帖发不出去,日志会记录服务端原始提示,据此排查。

## 七、在手机上运行(Android + Termux)

iOS 基本不可行(不允许后台常驻、装不了 Node),Android 可以。

1. 从 **F-Droid** 装 Termux(别用 Play 商店版),然后:

   ```
   pkg update && pkg install python nodejs tmux
   ```

   `nodejs` **必需** —— Cap 验证的 DOM 指纹计算要靠 Node。

2. 把整个 `lzsb-bot` 目录放进手机存储(压缩包见下),Termux 里:

   ```
   termux-setup-storage
   cp -r /sdcard/lzsb-bot ~/
   cd ~/lzsb-bot
   sed -i 's/\r$//' start.sh bot.py $(find lzsb_bot -name '*.py')
   ```

   最后那行 `sed` **必须执行**:Windows 的 CRLF 换行符会让 bash 报错。

3. 编辑 `config.json`,把 `proxy` 改成手机 Clash 的 HTTP 端口(通常
   `http://127.0.0.1:7890`,以你的客户端为准);`llm.json` 不用改。

4. 运行与挂后台:

   ```
   bash start.sh          # 自动申请 termux-wake-lock,防熄屏被杀
   tmux new -s bot        # 建议先进 tmux,Ctrl+B 再按 D 挂后台
   tmux attach -t bot     # 需要时连回来
   ```

| 手机端的坑 | 说明 |
|---|---|
| 被杀后台 | 国产 ROM 省电策略会清 Termux → 关电池优化、允许后台活动 |
| 代理端口 | 手机 Clash 的 HTTP 端口要与 `config.json` 一致 |
| 换行符 | 从 Windows 拷来必须执行上面的 `sed` |
| 耗电 | 持续运行会比平时费电 |

## 八、隐私与反特征化

本仓库**没有任何遥测/回传**，不向任何第三方发送数据；站点侧能观察到的只有 HTTP 行为本身。想降低多个安装实例被互相识别的概率：

1. **改写 `system_prompt`** —— 最大的行为特征。所有用默认提示词的实例，回复风格会高度雷同；装好后花两分钟换成自己的话术。
2. **调整节奏** —— `poll_interval` / `reply_delay_min/max` 自定义；轮询间隔自带 ±15% 抖动，不是死板的固定周期。
3. **换 UA** —— `user_agent` 字段随意改。
4. **TLS 指纹的固有限制** —— 纯标准库实现意味着 TLS 握手特征是 Python 的，与伪装的 Firefox UA 并不一致；严格的 Cloudflare 策略可据此拦截。本站目前不校验 JA3，若将来被拦需换 `curl_cffi` 方案（会引入第三方依赖）。
5. Cap 求解在服务端只表现为一次合法验证结果，与浏览器完成验证无异。
6. `node_path` —— node 装在非标准位置时填完整路径；留空则自动探测 PATH 与常见安装位置。
