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
| `notify_attachments` | 推送时是否**把附件文件一起发出去**，默认关。关着也会显示附件清单（名字+大小+类型），只是不发真文件 |

> ⚠️ 建议保持 `allow_all=false`：邮箱是你的，别人能用了就能以你的名义发信。群聊要用就填 `allowed_groups`。

## 指令

```
/mail send <收件人> <主题> | <正文>
/mail list [数量]
/mail read <message_id>
/mail whoami
/mail here            # 打印当前会话 ID，填收信推送用
/mail poll            # 手动跑一次收信轮询
```

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

例子：

```
/mail send 1150880493@qq.com 明天见 | 我这边跑完了，报告在附件里（下略）
/mail list 3
/mail whoami
```

多个收件人用英文逗号：`a@x.com,b@y.com`。

## 附件随推送（v0.4.0）

新邮件带附件时，推送里会多一段清单（`notify_attachments` 关着也照样有）：

```
📬 新邮件
主题：报表来了
发件人：boss@corp.com
时间：2026-10-27T10:00:00
────────────
正文预览前 300 字…
────────────
📎 附件 2 个
· Q3报表.xlsx（48.2 KB · application/vnd.openxmlformats-officedocument.spreadsheetml.sheet）
· 截图.png（1.2 MB · image/png）
```

想让**真文件**也发到会话里，把 `notify_attachments` 打开（默认关，不影响现有用户）：

- 附件会先下载到系统临时目录，再用 AstrBot 的文件消息组件
  `astrbot.api.message_components.File(name=..., file=...)` 随文字一起发出去，发完**立刻删临时目录**
- 单个附件上限 20 MB、每封最多取前 5 个（防止把容器磁盘塞满）
- 下载失败 / 平台适配器不支持文件消息 → **自动降级成文字清单 + 日志 warning**，整轮推送不会因此中断
- 清单永远显示，所以关着 `notify_attachments` 也能知道「这封有附件、叫什么、多大」

用到的 AgentMail 接口（都实测核对过）：

| 用途 | 接口 |
| --- | --- |
| 附件元信息（`attachments` 字段） | 直接来自列表 / 详情里的 message 对象 |
| 取签名下载地址 | `GET /v0/inboxes/{inbox}/messages/{message_id}/attachments/{attachment_id}` |
| 下载二进制 | 直接 GET 上一步返回的 `download_url`（签名 CDN 链接，**不能**再带 Authorization 头） |

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

日志关键字：`[agentmail]`

## 原理备忘

```
/mail send ...  →  Mailer.send()  →  POST /v0/inboxes/{inbox}/messages/send
                                     Authorization: Bearer <api_key>
```

接口文档：<https://docs.agentmail.to>（OpenAPI：<https://docs.agentmail.to/openapi.json>，注意路径是 `/v0`）

## License

MIT
