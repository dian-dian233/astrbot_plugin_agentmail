"""AgentMail 邮件插件：让任何私聊 / 群聊都能发邮件、查收件箱。

指令：
    /mail send <收件人> <主题> | <正文>
    /mail list [数量]
    /mail read <message_id>
    /mail whoami

也注册了 LLM 工具 `agentmail_send`，所以「给老登发封邮件说明天见」这种自然语言也能直接触发。

注意：`mail` 指令**不声明任何参数**，参数直接从 event.message_str 里解析。
AstrBot 的指令参数解析遇到 `*args` 会把 `inspect.Parameter.empty` 当成类型去调用，
直接抛 `_empty() takes no arguments`（v0.1.0 的坑，v0.1.1 修掉）。
"""

import asyncio
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # 插件包内导入
    from .mailer import MailError, Mailer
except ImportError:  # 单独运行 / 被平铺加载时
    from mailer import MailError, Mailer  # type: ignore[no-redef]

HELP = """AgentMail 邮件工具
· /mail send <收件人> <主题> | <正文>
  例：/mail send 123@qq.com 明天见 | 我去不了，饭钱转你了
· /mail list [数量]   查收件箱
· /mail read <id>     看某一封
· /mail whoami        看当前邮箱和连通状态"""

CMD_PREFIX = re.compile(r"^[/!！]?\s*(?:mail|邮件)\b\s*", re.IGNORECASE)


class AgentMailPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.conf = config or {}
        self._mailer = None

    # ── 内部 ────────────────────────────────────────────────
    def _mailer_or_raise(self):
        if self._mailer is None:
            self._mailer = Mailer(
                api_key=self.conf.get("api_key", ""),
                inbox=self.conf.get("inbox", ""),
                proxy=self.conf.get("proxy", ""),
                timeout=int(self.conf.get("timeout", 30) or 30),
            )
        return self._mailer

    def _allowed(self, event) -> bool:
        """白名单：allow_all / 管理员 / 指定用户 / 指定群。"""
        if self.conf.get("allow_all"):
            return True
        try:
            if event.is_admin():
                return True
        except Exception:  # noqa: BLE001 老版本没有这个方法
            pass
        sender = str(event.get_sender_id() or "")
        group = str(event.get_group_id() or "")
        users = [str(x) for x in (self.conf.get("allowed_users") or [])]
        groups = [str(x) for x in (self.conf.get("allowed_groups") or [])]
        return bool((sender and sender in users) or (group and group in groups))

    @staticmethod
    def _args(event) -> str:
        """从消息文本里剥掉指令前缀，拿到后续参数。"""
        raw = (getattr(event, "message_str", "") or "").strip()
        stripped = CMD_PREFIX.sub("", raw, count=1).strip()
        return stripped if stripped != raw.strip() else raw.strip()

    # ── 指令 ────────────────────────────────────────────────
    # 不声明任何参数：AstrBot 的参数解析器会把 *args 的 annotation（inspect empty）
    # 当成类型去调用，导致 `_empty() takes no arguments`。参数自己解析最省事。
    @filter.command("mail", alias={"邮件"})
    async def mail(self, event: AstrMessageEvent):
        """AgentMail 邮件工具：发信 / 查收件箱 / 读邮件。"""
        if not self._allowed(event):
            yield event.plain_result("这个会话没开邮件权限，找管理员把你的 QQ 或群号加进白名单。")
            return

        text = self._args(event)
        if not text:
            yield event.plain_result(HELP)
            return

        action, _, rest = text.partition(" ")
        action = action.strip().lower()
        rest = rest.strip()

        try:
            if action in ("send", "发", "发信", "发送"):
                to, subject, body = self._parse_send(rest)
                res = await asyncio.to_thread(self._mailer_or_raise().send, to, subject, body)
                mid = res.get("message_id", "?")
                yield event.plain_result(f"发出去了：{', '.join(to)}\n主题：{subject}\nid：{mid}")
            elif action in ("list", "收件箱", "inbox"):
                limit = int(rest) if rest.isdigit() else 5
                msgs = await asyncio.to_thread(self._mailer_or_raise().list, limit)
                if not msgs:
                    yield event.plain_result("收件箱是空的。")
                    return
                lines = []
                for m in msgs:
                    frm = m.get("from") or m.get("sender") or "?"
                    ts = str(m.get("timestamp", ""))[:19]
                    lines.append(
                        f"{ts} · {frm}\n  {m.get('subject', '(无主题)')}\n  {m.get('message_id', '')}"
                    )
                yield event.plain_result("\n\n".join(lines))
            elif action in ("read", "读"):
                if not rest:
                    yield event.plain_result("给我个 message_id。")
                    return
                m = await asyncio.to_thread(self._mailer_or_raise().read, rest)
                yield event.plain_result(self._render_mail(m))
            elif action in ("whoami", "状态"):
                info = await asyncio.to_thread(self._mailer_or_raise().whoami)
                yield event.plain_result(
                    f"邮箱：{info.get('email', '?')}\n"
                    f"显示名：{info.get('display_name', '?')}\n"
                    f"状态到期：{info.get('status_until', '?')}\n"
                    f"代理：{self.conf.get('proxy') or '（直连）'}"
                )
            else:
                yield event.plain_result(HELP)
        except MailError as e:
            logger.warning(f"[agentmail] {e}")
            yield event.plain_result(f"邮件接口出错：{e}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[agentmail] 未预期错误: {e}", exc_info=True)
            yield event.plain_result(f"出错了：{e}")

    @staticmethod
    def _parse_send(rest: str):
        """解析 `收件人 主题 | 正文`，也兼容用换行分隔。"""
        body = ""
        if "|" in rest:
            head, body = rest.split("|", 1)
        elif "\n" in rest:
            head, body = rest.split("\n", 1)
        else:
            head = rest
        head = head.strip()
        parts = head.split(None, 1)
        if not parts:
            raise MailError("用法：/mail send 收件人 主题 | 正文")
        to = [x.strip() for x in parts[0].replace("，", ",").split(",") if x.strip()]
        subject = parts[1].strip() if len(parts) > 1 else "(无主题)"
        return to, subject, body.strip()

    @staticmethod
    def _render_mail(m: dict) -> str:
        frm = m.get("from") or "?"
        to = m.get("to")
        if isinstance(to, (list, tuple)):
            to = ", ".join(str(x) for x in to)
        body = m.get("text") or m.get("preview") or m.get("extracted_text") or "(无正文)"
        return (
            f"主题：{m.get('subject', '(无主题)')}\n"
            f"发件人：{frm}\n收件人：{to or '?'}\n时间：{m.get('timestamp', '?')}\n"
            f"────────────\n{str(body)[:1500]}"
        )

    # ── LLM 工具 ────────────────────────────────────────────
    @filter.llm_tool(name="agentmail_send")
    async def agentmail_send(self, event: AstrMessageEvent, to: str, subject: str, body: str):
        """用 AgentMail 给指定邮箱发一封邮件。

        Args:
            to(string): 收件人邮箱地址，多个用英文逗号分隔
            subject(string): 邮件主题
            body(string): 邮件正文，纯文本
        """
        if not self._allowed(event):
            return "这个会话没有邮件权限，发不了。"
        try:
            res = await asyncio.to_thread(self._mailer_or_raise().send, to, subject, body)
            return f"邮件已发出，收件人 {to}，主题「{subject}」，message_id={res.get('message_id', '?')}"
        except MailError as e:
            return f"发送失败：{e}"
