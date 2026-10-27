"""AgentMail 邮件插件：让任何私聊 / 群聊都能发邮件、查收件箱，还能把新邮件推到指定会话。

指令：
    /mail send <收件人> <主题> | <正文>
    /mail list [数量]
    /mail read <message_id>
    /mail whoami
    /mail here            打印当前会话的 unified_msg_origin（填收信推送用）
    /mail poll            手动跑一次收信轮询

v0.2.0 新增：后台收信轮询，把新邮件推到 notify_sessions 里配置的会话/群。
v0.3.0 新增：推送文案可配（notify_template，占位符 {subject} {from} {preview} {time} {inbox}），
            渲染逻辑抽到 notify.py（纯标准库、可单测），模板非法时回退内置文案。
注意：`mail` 指令**不声明任何参数**，参数直接从 event.message_str 解析。
AstrBot 的指令参数解析遇到 `*args` 会把 `inspect.Parameter.empty` 当类型调用，
直接抛 `_empty() takes no arguments`（v0.1.0 的坑，v0.1.1 修掉）。
"""

import asyncio
import contextlib
import re

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

try:  # 插件包内导入
    from .mailer import MailError, Mailer
    from .notify import render_notice
except ImportError:  # 单独运行 / 被平铺加载时
    from mailer import MailError, Mailer  # type: ignore[no-redef]
    from notify import render_notice  # type: ignore[no-redef]

HELP = """AgentMail 邮件工具
· /mail send <收件人> <主题> | <正文>
  例：/mail send 123@qq.com 明天见 | 我去不了，饭钱转你了
· /mail list [数量]   查收件箱
· /mail read <id>     看某一封
· /mail whoami        看当前邮箱和连通状态
· /mail here          打印当前会话 ID（填「收信推送」用）
· /mail poll          手动跑一次收信轮询"""

CMD_PREFIX = re.compile(r"^[/!！]?\s*(?:mail|邮件)\b\s*", re.IGNORECASE)
SEEN_KEY = "seen_message_ids"
SEEN_LIMIT = 200


class AgentMailPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.conf = config or {}
        self._mailer = None
        self._poll_task = None

    # ── 生命周期 ────────────────────────────────────────────
    async def initialize(self):
        """插件加载完成后启动收信轮询（配了才启动）。"""
        if not self.conf.get("poll_enabled"):
            return
        sessions = [s for s in (self.conf.get("notify_sessions") or []) if s]
        if not sessions:
            logger.warning("[agentmail] 开了收信轮询，但 notify_sessions 是空的，先跑 /mail here 拿会话 ID")
            return
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"[agentmail] 收信轮询已启动：间隔 {self._interval()}s，推送到 {len(sessions)} 个会话"
        )

    async def terminate(self):
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._poll_task
            logger.info("[agentmail] 收信轮询已停止")

    # ── 内部 ────────────────────────────────────────────────
    def _interval(self) -> int:
        try:
            return max(60, int(self.conf.get("poll_interval") or 300))
        except (TypeError, ValueError):
            return 300

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

    # ── 收信轮询 ────────────────────────────────────────────
    async def _poll_loop(self):
        await asyncio.sleep(8)  # 等 AstrBot 平台适配器就绪
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 轮询不能因为一次失败就死
                logger.warning(f"[agentmail] 轮询出错：{e}")
            await asyncio.sleep(self._interval())

    async def _poll_once(self) -> dict:
        """拉一次收件箱：首次只记录基线，之后把新邮件推给 notify_sessions。"""
        sessions = [s for s in (self.conf.get("notify_sessions") or []) if s]
        msgs = await asyncio.to_thread(self._mailer_or_raise().list, 10)
        incoming = [m for m in msgs if self._is_incoming(m)]

        seen = await self.get_kv_data(SEEN_KEY, []) or []
        seen = [str(x) for x in seen]
        stats = {"new": 0, "pushed": 0, "baseline": 0}

        if not seen and not self.conf.get("notify_on_first_poll"):
            ids = [str(m.get("message_id")) for m in incoming if m.get("message_id")]
            await self.put_kv_data(SEEN_KEY, (ids + seen)[:SEEN_LIMIT])
            stats["baseline"] = len(ids)
            logger.info(f"[agentmail] 首次轮询，记录 {len(ids)} 封为基线，不推送")
            return stats

        fresh = [m for m in incoming if str(m.get("message_id")) not in seen]
        stats["new"] = len(fresh)
        if not fresh:
            return stats

        for m in fresh:
            text = self._compose_notice(m)
            for s in sessions:
                try:
                    await self.context.send_message(s, MessageChain([Comp.Plain(text)]))
                    stats["pushed"] += 1
                except Exception as e:  # noqa: BLE001 单个会话失败不影响其它
                    logger.warning(f"[agentmail] 推送到 {s} 失败：{e}")

        ids = [str(m.get("message_id")) for m in fresh if m.get("message_id")]
        await self.put_kv_data(SEEN_KEY, (ids + seen)[:SEEN_LIMIT])
        logger.info(f"[agentmail] 推送了 {len(fresh)} 封新邮件")
        return stats

    @staticmethod
    def _is_incoming(m: dict) -> bool:
        """只关心收到的邮件（跳过自己发出去的）。"""
        labels = m.get("labels") or []
        if isinstance(labels, str):
            labels = [labels]
        labels = [str(x).lower() for x in labels]
        if "sent" in labels:
            return False
        return ("received" in labels) or ("unread" in labels) or not labels

    def _compose_notice(self, m: dict) -> str:
        """推送文案：配了 notify_template 就按模板渲染，否则用内置文案（v0.3.0）。"""
        return render_notice(
            m,
            template=self.conf.get("notify_template") or "",
            preview_len=self.conf.get("notify_preview_len") or 300,
            inbox=self.conf.get("inbox") or "",
            warn=logger.warning,
        )

    # ── 指令 ────────────────────────────────────────────────
    # 不声明任何参数：AstrBot 的参数解析器会把 *args 的 annotation（inspect empty）
    # 当成类型去调用，导致 `_empty() takes no arguments`。参数自己解析最省事。
    @filter.command("mail", alias={"邮件"})
    async def mail(self, event: AstrMessageEvent):
        """AgentMail 邮件工具：发信 / 查收件箱 / 读邮件 / 收信推送。"""
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
            elif action in ("here", "会话"):
                umo = getattr(event, "unified_msg_origin", "") or "（拿不到）"
                yield event.plain_result(
                    f"当前会话 ID：\n{umo}\n\n"
                    "把它整行填进插件配置的 notify_sessions，新邮件就会推到这里。"
                )
            elif action in ("poll", "轮询"):
                stats = await self._poll_once()
                if stats["baseline"]:
                    yield event.plain_result(
                        f"首次轮询：已把 {stats['baseline']} 封邮标记为基线，之后只推新到的。"
                    )
                elif stats["new"]:
                    yield event.plain_result(
                        f"发现 {stats['new']} 封新邮件，推送 {stats['pushed']} 次。"
                    )
                else:
                    yield event.plain_result("没有新邮件。")
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
