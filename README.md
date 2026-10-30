# astrbot_plugin_agentmail

[![CI](https://github.com/dian-dian233/astrbot_plugin_agentmail/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/dian-dian233/astrbot_plugin_agentmail/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/dian-dian233/astrbot_plugin_agentmail?display_name=tag&sort=semver)](https://github.com/dian-dian233/astrbot_plugin_agentmail/releases/latest)

给 AstrBot 加一个**真正能用**的邮箱：任意私聊、任意群聊都能发邮件、查收件箱，也注册了 LLM 工具，直接说「给老登发封邮件」就行。

底层是 [AgentMail](https://agentmail.to)（给 AI agent 用的邮件服务），不依赖沙盒、不依赖 bootstrap，装满配好就能用。

## 目录结构

根目录只放「框架要认的东西 + 入口 + 项目文件」，剩下按职责分三个子包（v0.6.x 重构，
纯搬家、行为一行没改）：

```text
astrbot_plugin_agentmail/
├── main.py            # 插件入口：只留 Star 子类、指令/LLM 工具注册与调度
├── __init__.py        # 空文件，AstrBot 以「包」的方式加载插件目录时需要它
├── metadata.yaml      # 插件元数据（框架约定，必须在根目录）
├── _conf_schema.json  # WebUI 配置 schema（框架约定，必须在根目录）
├── requirements.txt   # 空（纯标准库实现）
├── core/              # 收发与策略
│   ├── mailer.py      #   AgentMail HTTP 客户端（直连 + 代理双通道、搜索降级）
│   ├── perm.py        #   权限判定（放行/拒绝 + 原因码 + 解决建议）
│   ├── limits.py      #   发送限流（冷却 / 每日额度）
│   ├── audit.py       #   发件审计（sqlite3 落库、`/mail log` 取数）
│   └── sendargs.py    #   `/mail send` 参数解析（--cc / --bcc）
├── push/              # 收信推送
│   ├── notify.py      #   推送文案模板渲染
│   ├── digest.py      #   每日未读摘要（定时、去重、文案）
│   └── attach.py      #   附件清单 / 文件组件
├── tools/             # 检索
│   └── search.py      #   历史邮件搜索（服务端 q/limit + 本地兜底 + 结果排版）
├── tests/
│   └── selftest.py    # 离线自测（桩掉 astrbot.api，360 条断言）
└── docs/screenshots/  # 截图（占位）
```

跨文件 import 一律用**相对导入**（`from .core.mailer import Mailer`）：
AstrBot 的 `star_manager` 是按包加载的——`path = "data.plugins.<插件目录名>.main"` 然后
`__import__(path, fromlist=["main"])`（v4.9.0 / v4.20.0 / v4.29.3 都是这一行），
所以插件目录就是个真包，`main.py` 里的 `__package__` 是 `data.plugins.astrbot_plugin_agentmail`，
子包 `core/` `push/` `tools/`（各自带 `__init__.py`）能正常被相对导入。
`main.py` / `metadata.yaml` / `_conf_schema.json` 必须留在根目录，这是框架约定。

## 安装

**方式一：WebUI 装（推荐）**

AstrBot WebUI → 插件 → 安装/添加 → 填仓库地址：

```
https://github.com/dian-dian233/astrbot_plugin_agentmail
```

**方式二：手动**

```bash
cd /opt/dockerApp/AstrBot/data/plugins   # 换成你的路径
git clone https://github.com/dian-dian233/astrbot_plugin_agentmail.git
# 然后 WebUI 里重载插件 / 重启 astrbot 容器
```

**方式三：zip 压缩包（GitHub Release 下载）**

去 [Releases](https://github.com/dian-dian233/astrbot_plugin_agentmail/releases/latest) 下 `astrbot_plugin_agentmail-<version>.zip`，**zip 顶层就是 `astrbot_plugin_agentmail/` 文件夹**：

```bash
cd /path/to/AstrBot/data/plugins
unzip ~/Downloads/astrbot_plugin_agentmail-0.5.1.zip   # 直接解出 astrbot_plugin_agentmail/ 文件夹
# 然后 WebUI 里重载插件 / 重启 astrbot 容器
```

无第三方依赖，`requirements.txt` 是空的，不用 pip 装东西。

## 配置

WebUI → 插件 → AgentMail 邮件 → 配置：

| 配置项 | 说明 |
| --- | --- |
| `api_key` | AgentMail API Key（`am_us_...`），必填 |
| `inbox` | 发信用的 inbox，如 `diandian233@agentmail.to` |
| `proxy` | HTTP 代理，留空=直连。日志里出现「被网关拦截（返回 HTML 页面）」时才需要填 |
| `timeout` | 请求超时秒数，默认 30 |
| `allow_all` | 是否允许所有会话使用。**默认关**，只放管理员和白名单 |
| `allowed_users` | 允许使用的用户 ID（QQ 号等），一行一个 |
| `allowed_groups` | 允许整群使用的群号，一行一个 |
| `send_cooldown` | **同一用户两次发信的最小间隔（秒）**，默认 30；填 0 = 关掉冷却（v0.6.0） |
| `send_daily_limit` | **每个用户每天最多发几封**，默认 50，按发送人统计、本地时间 0 点重置；填 0 = 不限制（v0.6.0） |
| `audit_data_dir` | 发件审计库所在目录，**留空＝自动用 AstrBot 的 data 目录**；审计库不写插件仓库目录（v0.6.0） |
| `poll_enabled` | 是否开启收信轮询（新邮件推到会话），默认关 |
| `poll_interval` | 轮询间隔秒数，最小 60，默认 300 |
| `notify_sessions` | 新邮件推送到哪些会话，用 `/mail here` 拿到的 ID，一行一个 |
| `notify_preview_len` | 推送里的正文预览字数，默认 300 |
| `notify_template` | 推送文案模板，**留空=内置文案**。可用占位符 `{subject}` `{from}` `{preview}` `{time}` `{inbox}`；写错（未知占位符、花括号没闭合）会自动回退内置文案并记 warning，不会报错 |
| `notify_on_first_poll` | 首次轮询是否推送历史邮件，建议保持关（首次只记基线，不刷屏） |
| `notify_attachments` | 新邮件带附件时是否把**附件文件一起发出去**，默认关。关＝只在推送里追加附件清单（文件名 + 大小 + 类型）；开＝清单之后再用文件组件逐个发文件，失败自动降级成清单 |
| `digest_enabled` | 是否开启**每日未读摘要**，默认关 |
| `digest_time` | 每日摘要推送时间，24 小时制，默认 `09:00`。写错（`25:00`/`9点`/空）自动回退 `09:00` 并记 warning |
| `digest_sessions` | 每日摘要推送到哪些会话，**留空则回退 `notify_sessions`** |

> ⚠️ 建议保持 `allow_all=false`：邮箱是你的，别人能用了就能以你的名义发信。群聊要用就填 `allowed_groups`。

## 指令

```
/mail send <收件人> [--cc 抄送] [--bcc 密送] <主题> | <正文>
/mail list [数量]
/mail search <关键词> [数量]   # 搜历史邮件（主题/发件人/正文），默认 5 条、最多 20
/mail read <message_id>
/mail log [数量]      # 发件记录（成功/失败都记），默认 10 条、最多 50（v0.6.0）
/mail whoami
/mail here            # 打印当前会话 ID，填收信推送用
/mail poll            # 手动跑一次收信轮询
```

## 没权限时，会说清是什么挡的（v0.6.0）

v0.5.x 被拦只有一句「这个会话没开邮件权限」，完全不知道自己撞了哪条规则、该找谁加什么。
v0.6.0 起拒绝会直接给「原因 + 解决」，并且**日志里也记一条**（谁、哪个会话、被哪条规则挡）：

```
这个会话没有邮件权限，发不了。
原因：allow_all 未开启，且你的 QQ 号 1150880493 不在 allowed_users 里，当前群 680787260 也不在 allowed_groups 里。
解决：让管理员在 WebUI → 插件配置 → allowed_users 里加上 1150880493（只放开你），或把群号 680787260 填进 allowed_groups（整群可用）。
```

能区分的原因有四类：

| 原因码 | 什么情况 | 文案里会告诉你 |
| --- | --- | --- |
| `not_whitelisted_dm` | 私聊：`allow_all` 关，你的号不在 `allowed_users` | 你的号该加进 `allowed_users`（并说明群白名单对私聊无效） |
| `not_whitelisted_group` | 群聊：`allow_all` 关，你的号不在 `allowed_users`、群也不在 `allowed_groups` | 两条路都给：加你的号，或加群号 |
| `not_whitelisted_no_id` | 拿不到用户 ID / 群号，没法比白名单 | 改用群号填 `allowed_groups` |
| `allow_all_invalid` | `allow_all` 的值不是真布尔（手写成 `"off"`、`5`、`[]` 之类） | 指出类型不对、已按关闭处理、去 WebUI 选 true/false |

> ℹ️ 顺带修掉一个坑：v0.5.x 的 `if self.conf.get("allow_all"):` 会把 `"false"` / `"off"` 这种
> **非空字符串一律当「开」**，配置明明是关的却对所有人放开。v0.6.0 按真布尔判定，
> 并把「类型不对」当成一条明确的拒绝原因报出来。

管理员 / 白名单用户 / `allow_all` 打开，这四条放行路径都没变。指令和 LLM 工具
（`agentmail_send`）用的是同一段文案，模型可以直接转述给用户。

## 发件记录：`/mail log`（v0.6.0）

每次发送都落一条审计，**成功和失败都记**（被限流拦下的也记，`结果=失败`，附原因）：

```
/mail log        # 最近 10 条，按时间倒序
/mail log 3      # 最近 3 条（最多 50）
```

输出长这样：

```
📨 发件记录（最近 2 条，按时间倒序）
2026-10-30 15:04:11 · ✅ 成功 · 私聊 1150880493 · 触发者 1150880493
  发给：a@x.com（CC b@x.com）
  主题：明天见 · 正文 12 字 · id：<010001...@email.amazonses.com>
────────────
2026-10-30 15:03:02 · ❌ 失败 · 群 680787260 · 触发者 1150880493
  发给：c@d.com
  主题：周报 · 正文 3 字
  失败：HTTP 401：…
```

落库字段：时间、触发者、会话类型 / 会话 ID、UMO、收件人、CC、BCC、主题、正文长度、
`message_id`、结果（成功/失败）、失败原因、耗时。

存储实现：

- **纯标准库 `sqlite3`**，`requirements.txt` 那条「纯标准库实现」没破
- 文件在 **AstrBot 的 data 目录**：`<data>/plugin_data/astrbot_plugin_agentmail/mail_audit.sqlite3`
  （找目录的顺序：`audit_data_dir` 配置 → `AGENTMAIL_DATA_DIR` / `ASTRBOT_DATA_DIR` 环境变量 →
  `astrbot_path.get_astrbot_data_path()` → `<root>/data` → `./data` → `~/AstrBot/data` / `~/.astrbot/data` → 临时目录）
- **绝不写插件仓库目录**：命中仓库路径的候选会被直接跳过（跑单测时 `./data` 就在仓库里，会被跳掉）
- 审计库初始化 / 写入失败只记 warning，**发信不受影响**
- `/mail log` 和其它指令一样走同一套权限闸门，没权限的人看不到

## 发送限流（v0.6.0）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `send_cooldown` | `30` | 同一用户两次发送的最小间隔（秒）。填 `0` = 关掉 |
| `send_daily_limit` | `50` | 每个用户每天最多成功发几封（按发送人统计，本地时间 0 点重置）。填 `0` = 不限制 |

超限不会只回一句「太频繁」，而是给剩余时间 / 剩余条数：

```
发得太快了，还要等 18 秒。
原因：send_cooldown = 30 秒（同一个人两次发送之间至少隔 30 秒），上一次是 12 秒前发的。
剩余：18 秒后可再发。
解决：等 18 秒再发；或让管理员在 WebUI → 插件配置 → send_cooldown 里调小（填 0 = 关掉冷却）。
```

```
今天已经发满 50 封了，现在发不了。
原因：send_daily_limit = 50（每个用户每天最多成功发 50 封，按发送人统计）。
剩余：0 封（今天已用 50/50），额度在明天 0 点（本地时间）重置。
解决：明天再发；或让管理员在 WebUI → 插件配置 → send_daily_limit 里调大（填 0 = 关掉每日上限）。
```

几个约定：

- **只统计成功发出去的那几封**：失败的那次不占冷却、不吃额度；被拦的那次也不会延长冷却（不然越急越自锁）
- 两个都超时**优先报每日额度**（不然让人白等 20 秒冷却再被拒一次）
- 限流按人算，一个人被拦不影响别人
- 每天成功发一封后回执里会带一句「今日额度：剩 49/50 封」

## CC / BCC（v0.6.0）

**AgentMail 的发送接口原生支持 `cc` / `bcc`**，所以这里是**真 CC/BCC**，不是把地址拼进正文：

```
/mail send a@x.com --cc boss@x.com --bcc log@x.com 周报 | 正文…
/mail send a@x.com,b@y.com --cc c@x.com,d@x.com 明天见 | 正文…
```

- 依据：`POST /v0/inboxes/{inbox_id}/messages/send` 的 `SendMessageRequest` 里就有
  `cc`（Addresses，optional）和 `bcc`（Addresses，optional），Addresses = `string | list of string`，
  见 <https://docs.agentmail.to/api-reference/inboxes/messages/send>
- `--cc` / `--bcc` 后面跟着**像邮箱的 token**（含 `@`）才算地址，可以逗号分隔写多个，也可以连着写多个；
  一旦不像邮箱就停手 —— 所以 `--cc a@b.com 明天见` 里的「明天见」不会被吃成抄送地址
- 回执会明确写出 `CC：…（真 CC）` / `BCC：…（真 BCC，其他收件人看不到）`
- **保底分支（不假装支持）**：如果某个老部署回 400/422 且明确抱怨 `cc`/`bcc` 字段，
  插件会去掉这两个字段重发一次、把 CC/BCC 写进正文开头，并在回执里明说
  「⚠️ 服务端不支持真 CC/BCC，已把 CC/BCC 写进正文开头。」；与 cc 无关的 400 照常抛错，不吞

## 搜索历史邮件（v0.4.0）

收件箱翻不过来，直接搜关键词：

```
/mail search 老登          # 搜「老登」，默认回 5 条
/mail search 报告 3        # 搜「报告」，最多 3 条
```

搜的范围：**主题 / 发件人 / 正文预览**，大小写不敏感（`Welcome` 能搜到 `welcome`），
中文直接搜（不走词边界，`老登` 能命中「老登，你太美」）。结果用**和 `/mail list`
完全一样的行格式**（`时间 · 发件人` / 主题 / message_id，多条之间空一行）：

```
/mail search 太美
2026-10-27T09:15:21 · 点点 <diandian233@agentmail.to>
  老登，你太美
  <010001a17ca48c6e-...@email.amazonses.com>
```

没搜到会明确回一句 `没搜到包含「xxx」的邮件`；关键词为空只回用法，不会去打 API。
数量省略默认 5，范围 1~20（`abc` 之类非数字回默认 5，`-1` 夹成 1，`999` 夹成 20）。

### 搜的是服务端还是本地？

**默认走 AgentMail 的服务端搜索端点**（不是本地扫全量）：

```
GET /v0/inboxes/{inbox}/messages/search?q=<关键词>&limit=<数量>
```

参数名照官方 OpenAPI（`docs.agentmail.to/openapi.json`）抄的：该 path 只有
`q`（必填）、`limit`、`page_token`、`before`、`after` 五个 **query 参数，没有 requestBody**；
服务端对主题/收发件人做子串匹配、对正文做全文索引，结果按相关度排序（最多 100 条）。

只有当这个端点回 **400 / 404 / 405 / 422 / 501**（比如套餐或权限不支持）时，
才自动退化为 **本地过滤**：`GET /v0/inboxes/{inbox}/messages?limit=50` 拉最近 50 封，
再用 `tools/search.py` 里的纯函数做大小写不敏感的 `in` 匹配（主题 / 发件人 / 正文预览 / text），
日志里会留一条 `服务端搜索不可用（HTTP xxx），退化为 list(50) 本地过滤` 的 warning。
401 之类真错误照常报出来，不吞。

## 收信推送（v0.2.0）

有人往 `diandian233@agentmail.to` 发信，插件可以主动推到你的私聊或群里。

开法三步：

1. 在**目标会话**里发 `/mail here`，拿到类似 `aiocqhttp:FriendMessage:1150880493` 的 ID
2. 插件配置：`poll_enabled` 打开，`notify_sessions` 填上这个 ID（多个一行一个）
3. 重载插件，日志出现 `收信轮询已启动` 就成

细节：

- **首次只记基线**：第一次轮询会把现有邮件标记为已读基线，不会把历史邮件全推一遍（想推就把 `notify_on_first_poll` 打开）
- **去重**：已推送过的 `message_id` 存在插件 KV 里（最近 200 条），重启不会重复推
- **只推收到的**：自己发出去的（`labels` 含 `sent`）会被跳过
- **失败不致死**：某个会话推失败只记 warning，不影响其它会话和下一轮
- 想立刻验证效果，不用等 5 分钟，发 `/mail poll` 手动跑一次

推送文案长这样：

```
📬 新邮件
主题：新来的
发件人：someone@qq.com
时间：2026-10-27T10:00:00
────────────
正文预览前 300 字…
```

### 自定义推送文案（v0.3.0）

不想看内置格式，就把配置项 `notify_template` 填上自己的模板（留空＝内置文案，行为不变）：

```text
📮 新邮件｜{subject}
来自 {from} · {time}
收件箱 {inbox}
────────────
{preview}
```

换行直接在配置框里回车（多行模板没问题）。占位符只有这五个：

| 占位符 | 内容 |
| --- | --- |
| `{subject}` | 邮件主题，空则 `(无主题)` |
| `{from}` | 发件人，空则 `?` |
| `{preview}` | 正文预览，仍受 `notify_preview_len` 截断（超长补 `…`） |
| `{time}` | 时间，取前 19 位，如 `2026-10-27T10:00:00` |
| `{inbox}` | 插件配置里的 `inbox` |

模板写坏了（比如 `{foo}`、`{subject` 没闭合、`{subject!r}`）**不会报错**：自动回退内置文案，日志里留一条 warning，`[agentmail] 推送模板不可用，已回退内置文案：…`。

### 附件随推送一并送达（v0.5.0）

新邮件带附件时，不再只有一句「正文预览」，而是把附件也一起交代清楚。开关是
`notify_attachments`（默认 **关**）：

| 值 | 行为 |
| --- | --- |
| `false`（默认） | 推送文案末尾追加**附件清单**：文件名 + 可读大小 + 类型，例如 `· report.pdf（12.3KB · application/pdf）`。不下载、不发文件，只让你知道有附件 |
| `true` | 清单照旧，然后**在同一个消息链里**把附件逐个发出去 |

推送长这样（`notify_attachments=false`，有 2 个附件时）：

```
📬 新邮件
主题：带附件的邮件
发件人：someone@qq.com
时间：2026-10-27T10:00:00
────────────
正文预览…
────────────
📎 附件 2 个
· report.pdf（12.3KB · application/pdf）
· 数据.csv（3.1MB · text/csv）
```

实现上的几个要点：

- 发文件用的是 **`Comp.File(name=<文件名>, url=<download_url>)`**：`File.file` 这个
  property 会自己拿 `url` 去下载，**不用落临时文件**；整条消息链是「一段文字 + N 个文件组件」
- `download_url` 来自 `GET /v0/inboxes/{inbox}/messages/{message_id}/attachments/{attachment_id}`，
  是**带签名、会过期**的临时地址，每次发送前现取，不做任何缓存
- **失败只降级、不炸**：取不到下载地址 → 该附件留在文字清单里 + 一条 warning，正文照发；
  平台不支持文件组件（发送抛异常）→ 自动降级成纯文字清单重试一次 + 一条 warning
- 一封邮件最多发 5 个附件、清单最多列 8 条（再多折叠成「…还有 N 个」），
  整条推送长度上限 1500 字，不会把消息塞爆
- `attachments` 字段缺失 / 是 `None` / 是字符串 / 是空 list / 元素不是对象 → 安全跳过，
  文案和没有附件时逐字一致

例子：

```
/mail send 1150880493@qq.com 明天见 | 我这边跑完了，报告在附件里（下略）
/mail list 3
/mail search 报告 3
/mail whoami
```

多个收件人用英文逗号：`a@x.com,b@y.com`。

## 每日未读摘要（v0.5.0）

不想被一封封推送打断，也可以只要**每天一条**的汇总：`digest_enabled` 打开，
`digest_time` 填时间（24 小时制，默认 `09:00`），`digest_sessions` 填推送目标
（**留空就用 `notify_sessions`**，一般不用填）。

到点推送长这样：

```
📬 每日未读摘要 · 2026-10-27
收件箱：diandian233@agentmail.to
未读邮件：3 封
────────────
最新 3 封：
1. 10-27 12:00 · boss@corp.com · 最新的一封
2. 10-27 11:00 · someone@qq.com · 第二封
3. 10-27 10:00 · other@x.com · 第三封
```

几个实现约定（都是踩过的坑）：

- **插件自带定时**：`asyncio` 循环 + 下次触发时间计算，加载时在 `initialize()` 里起、
  `terminate()` 里取消，**不依赖外部 cron**
- **用本地时间**判断到点，不用 UTC（东八区用 UTC 会差 8 小时，跨天时容易「一天推两次」或「整天不推」）
- **同一天只推一次**：最后一次推送的本地日期存在插件 KV（`digest_last_date`），
  **进程重启也不会重复推当天**；跨天（日期变了）照常推
- 进程中途启动、且当天已经过了点到时间 → 补推一次，不漏天
- `digest_time` 写错（`25:00`、`9点`、空、`9:70`、`abc`）→ 回退 `09:00` + 一条 warning，**不抛异常**
- **和收信轮询共用同一个 Mailer 实例**，不会开两条连接猛刷 API
- 摘要推送失败（会话不可达）只记 warning，不标记当天已推，下次还能补上
- 未读口径：`labels` 含 `unread`、且不含 `sent`；没带 `labels` 的邮件按未读算（宁多不漏）；
  拉到 50 封上限时文案显示 `50+ 封`，不报假精确数字

## 自然语言

插件注册了 LLM 工具 `agentmail_send`，所以这些说法都能直接触发（前提是那个会话开了工具/函数调用）：

- 「给 1150880493@qq.com 发封邮件，主题写站点上线，正文说明天给你截图」
- 「帮我发邮件告诉他我晚点到」

## 出网说明

`core/mailer.py` 里做了双通道：

1. 先**直连** `api.agentmail.to`
2. 如果回来的是**网关的 HTML 错误页**（比如内网 FortiGate 返回 403 HTML），自动**回退代理**重试
3. 明确的 API 错误（401/404 等 JSON）直接抛错并原样显示

所以同一份代码在「被墙的内网」和「放通的远程机」上都能跑，换机器不用改配置。

## 排错

| 现象 | 原因 / 处理 |
| --- | --- |
| `_empty() takes no arguments` | **v0.1.0 的 bug，v0.1.1 已修**：指令 handler 里用了 `*args`，AstrBot 的参数解析器会把 `inspect.Parameter.empty` 当类型去调用。升级到 0.1.1 即可（handler 不再声明参数，自己从 `event.message_str` 解析） |
| `401` | key 错了或者被 rotate 了，回面板重新生成 |
| `404 Route not found` | base 路径不对；本项目用的是 `/v0`，不是 `/v1` |
| `被网关拦截（返回 HTML 页面）` | 没连上 AgentMail，是本地网关吐的页面 → 配置里填 `proxy` |
| `没有配置 AgentMail API Key` | 配置项 `api_key` 是空的，填完记得保存并重载插件 |
| 群里发指令没反应 | 先看 `allow_all` / `allowed_groups` / 是否管理员；再确认这个会话没禁用 `/mail` 指令。**v0.6.0 起拒绝会直接告诉你被哪条规则挡、该加什么**，日志关键字 `权限拒绝` |
| `还要等 N 秒` / `今天已经发满 N 封了` | 撞了 v0.6.0 的发送限流：`send_cooldown` / `send_daily_limit`。等一会儿或让管理员调大（填 0 = 关掉） |
| `/mail log` 说查不到记录 | 审计库没开起来，看插件日志里的 warning（一般是 data 目录不可写）；可以手动指定 `audit_data_dir` |
| 只看到附件清单、没收到附件文件 | `notify_attachments` 没开；或平台不支持文件组件（日志里有「降级为纯文字清单重试」warning，清单里的信息照样是全的） |
| 到点没收到每日摘要 | 看 `digest_enabled` 是否打开、`digest_sessions`（或 `notify_sessions`）是否填了会话 ID、`digest_time` 格式对不对；日志关键字「每日未读摘要」 |

日志关键字：`[agentmail]`

## CI/CD 与发版

`.github/workflows/` 里两个工作流，跑在 GitHub Actions 上。**不用任何 secrets**，只吃默认的 `GITHUB_TOKEN`，权限在 yml 里显式声明为最小（CI 只 `contents: read`，发版只有建 Release 的那个 job 拿 `contents: write`）。

### CI：`.github/workflows/ci.yml`

push 到任意分支、以及所有 PR 都会跑：

| job | 干什么 |
| --- | --- |
| `lint + selftest (py3.12 / py3.13)` | `python -m compileall -q .` → `ruff check .` → `PYTHONDONTWRITEBYTECODE=1 python3 tests/selftest.py --offline` |
| `manifest 体检` | `metadata.yaml` 能被 YAML 解析、必填字段（name/display_name/desc/version/author/repo/astrbot_version）齐全、`repo` 等于当前仓库地址、`name` 等于仓库名；`_conf_schema.json` 是合法 JSON 且每项都有 `description`/`type`/`default` |

Python 矩阵只放 `3.12` / `3.13`：AstrBot 自己的约束是 `requires-python = ">=3.12"`（见 `astrbot_src/pyproject.toml`），3.10 / 3.11 上的兼容性没有意义。

本地复现（和 CI 完全一致）：

```bash
pip install ruff
ruff check .                                     # 规则见 ruff.toml：行宽 120、target py312、E/W/F/I/B/C4
PYTHONDONTWRITEBYTECODE=1 python3 tests/selftest.py --offline
```

`tests/selftest.py --offline` 不需要真 key，用假的 `am_test_dummy` 跑（断言全是桩、不打网络）；不带 `--offline` 又读不到 `~/.secrets/agentmail.key` 时，会打一行提示然后自动降级成离线模式，不会抛 traceback。

### 发版：`.github/workflows/release.yml`

打 tag 就发版。唯一硬要求：**tag 必须和 `metadata.yaml` 里的 `version` 一致**，不一致直接红并告诉你该怎么做（这是最容易翻的车）。

```bash
# 1. 先把 metadata.yaml 里的 version 改成 0.5.1，提交、推到 main
# 2. 再打 tag（v 前缀 + 同一个版本号）
git tag v0.5.1
git push origin v0.5.1
```

然后工作流会：校验 tag ↔ version → 打包 `astrbot_plugin_agentmail-<version>.zip`（zip **顶层就是 `astrbot_plugin_agentmail/` 文件夹**，里面的 `metadata.yaml` / `main.py` / `_conf_schema.json` 一应俱全，运行期代码所在的 `core/` `push/` `tools/` 三个子包整目录打进去；已排除 `__pycache__` / `.git` / `tests/` / `docs/` / CI 自身）→ 用 `gh release create` 建 Release、挂上 zip，release notes 自动取 `git log`（上一个 tag 到这次的提交）。

不想为了验证就污染仓库？用 **workflow_dispatch**：Actions → Release → Run workflow，只做「校验 tag ↔ version + 打包 + 上传 artifact」，默认**不**建 Release（想真发才勾 `publish`）。也可以故意填错 tag（如 `v9.9.9`）看它红。

## 原理备忘

```
/mail send ...  →  权限（core/perm.py：放行/拒绝 + 原因）→ 限流（core/limits.py：冷却/每日额度）
                →  Mailer.send()（core/mailer.py）→  POST /v0/inboxes/{inbox}/messages/send
                                    （cc / bcc 是接口原生字段）
                →  审计（core/audit.py：sqlite3 落一条）→  回执
```

```markdown
收信推送 + 附件 → Mailer.list()（push/attach.py 拼清单/文件组件）→  GET  /v0/inboxes/{inbox}/messages
                → Mailer.attachment_info() →  GET  /v0/inboxes/{inbox}/messages/{id}/attachments/{aid}
                                             →  download_url（带签名、会过期，现取现用）

每日未读摘要    → asyncio 定时（push/digest.py：本地时间，同一天只推一次，日期存 KV）
                → Mailer.list(50) → 未读计数 + 最新 3 封 → MessageChain([Plain]) → send_message

/mail log       → core/audit.AuditLog.recent(n) → format_log_rows() → 纯文本回执
```

接口文档：<https://docs.agentmail.to>（OpenAPI：<https://docs.agentmail.to/openapi.json>，注意路径是 `/v0`）

## 署名约定

本仓库的产出由多个 agent 协作完成，参与方写在提交信息的 trailer 里：

```
Co-Authored-By: 兔兔小弟 <tutu@wcnmb.si>
Co-Authored-By: 点点 <dian-dian233@users.noreply.github.com>
```

用法（`-m` 的第二段放 trailer，和正文之间空一行）：

```bash
git commit -m "docs: 补充某某说明" \
  -m "Co-Authored-By: 兔兔小弟 <tutu@wcnmb.si>
Co-Authored-By: 点点 <dian-dian233@users.noreply.github.com>"
```

三档区别，一句话讲清：

- 提交的 `author` / `committer` 是 git 元数据，跟 GitHub 账号没有强制绑定
- email 绑定了 GitHub 账号 → 网页上显示头像并可点击；没绑定 → 只显示为文字
- 所以「谁写的」靠 trailer 留痕，不靠另开账号

**不改历史**：已经推上去的提交不补写 trailer，这项约定只对之后的提交生效。

## License

MIT
