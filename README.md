# astrbot_plugin_agentmail

给 AstrBot 加一个**真正能用**的邮箱：任意私聊、任意群聊都能发邮件、查收件箱，也注册了 LLM 工具，直接说「给老登发封邮件」就行。

底层是 [AgentMail](https://agentmail.to)（给 AI agent 用的邮件服务），不依赖沙盒、不依赖 bootstrap，装满配好就能用。

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
/mail send <收件人> <主题> | <正文>
/mail list [数量]
/mail search <关键词> [数量]   # 搜历史邮件（主题/发件人/正文），默认 5 条、最多 20
/mail read <message_id>
/mail whoami
/mail here            # 打印当前会话 ID，填收信推送用
/mail poll            # 手动跑一次收信轮询
```

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
再用 `search.py` 里的纯函数做大小写不敏感的 `in` 匹配（主题 / 发件人 / 正文预览 / text），
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

`mailer.py` 里做了双通道：

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
| 群里发指令没反应 | 先看 `allow_all` / `allowed_groups` / 是否管理员；再确认这个会话没禁用 `/mail` 指令 |
| 只看到附件清单、没收到附件文件 | `notify_attachments` 没开；或平台不支持文件组件（日志里有「降级为纯文字清单重试」warning，清单里的信息照样是全的） |
| 到点没收到每日摘要 | 看 `digest_enabled` 是否打开、`digest_sessions`（或 `notify_sessions`）是否填了会话 ID、`digest_time` 格式对不对；日志关键字「每日未读摘要」 |

日志关键字：`[agentmail]`

## 原理备忘

```
/mail send ...  →  Mailer.send()  →  POST /v0/inboxes/{inbox}/messages/send
                                     Authorization: Bearer <api_key>

收信推送 + 附件 → Mailer.list()           →  GET  /v0/inboxes/{inbox}/messages
                → Mailer.attachment_info() →  GET  /v0/inboxes/{inbox}/messages/{id}/attachments/{aid}
                                             →  download_url（带签名、会过期，现取现用）

每日未读摘要    → asyncio 定时（本地时间，同一天只推一次，日期存 KV）
                → Mailer.list(50) → 未读计数 + 最新 3 封 → MessageChain([Plain]) → send_message
```

接口文档：<https://docs.agentmail.to>（OpenAPI：<https://docs.agentmail.to/openapi.json>，注意路径是 `/v0`）

## License

MIT
