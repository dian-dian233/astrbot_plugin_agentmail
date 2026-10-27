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

> ⚠️ 建议保持 `allow_all=false`：邮箱是你的，别人能用了就能以你的名义发信。群聊要用就填 `allowed_groups`。

## 指令

```
/mail send <收件人> <主题> | <正文>
/mail list [数量]
/mail read <message_id>
/mail whoami
```

例子：

```
/mail send 1150880493@qq.com 明天见 | 我这边跑完了，报告在附件里（下略）
/mail list 3
/mail whoami
```

多个收件人用英文逗号：`a@x.com,b@y.com`。

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
