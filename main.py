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
v0.4.0 新增：/mail search <关键词> [数量]，搜历史邮件（主题 / 发件人 / 正文预览），
            解析、本地过滤、结果排版抽到 search.py（纯标准库、可单测），
            结果行格式和 /mail list 共用 format_rows()，逐字一致。
v0.5.0 新增功能 A：推送时带附件。新配置 notify_attachments（默认 false）——
            false 只在推送文案里追加「附件清单」（文件名 + 可读大小 + 类型），
            true 则在清单之后用 Comp.File(name=..., url=download_url) 把附件
            跟着同一条消息链发出去（组件自己会拿 url 下载，不落临时文件）。
            取下载地址 / 发送失败只记 warning，正文和清单照发，绝不让整轮推送挂掉。
v0.5.0 新增功能 B：每日未读摘要。新配置 digest_enabled（默认 false）、
            digest_time（默认 "09:00"，24 小时制）、digest_sessions（留空回退
            notify_sessions）。插件自带 asyncio 定时（不依赖外部 cron），到点统计
            未读数量 + 最新 3 封的「时间 · 发件人 · 主题」推到目标会话；
            用本地时间判断、同一天只推一次（最后推送日期存插件 KV，重启也不重复），
            和收信轮询共用同一个 Mailer 实例。
注意：`mail` 指令**不声明任何参数**，参数直接从 event.message_str 解析。
AstrBot 的指令参数解析遇到 `*args` 会把 `inspect.Parameter.empty` 当类型调用，
直接抛 `_empty() takes no arguments`（v0.1.0 的坑，v0.1.1 修掉）。
"""

import asyncio
import contextlib
import re
from datetime import datetime

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

try:  # 插件包内导入
    from .attach import (
        MAX_SEND_ATTACHMENTS,
        NOTICE_MAX_CHARS,
        attachment_block,
        clip_text,
        extract_attachments,
    )
    from .mailer import MailError, Mailer
    from .digest import (
        DIGEST_FETCH,
        DIGEST_LATEST,
        fmt_clock,
        format_digest,
        is_due,
        next_check_delay,
        parse_time,
    )
    from .notify import render_notice
    from .search import format_rows, parse_search_args
except ImportError:  # 单独运行 / 被平铺加载时
    from attach import (  # type: ignore[no-redef]
        MAX_SEND_ATTACHMENTS,
        NOTICE_MAX_CHARS,
        attachment_block,
        clip_text,
        extract_attachments,
    )
    from digest import (  # type: ignore[no-redef]
        DIGEST_FETCH,
        DIGEST_LATEST,
        fmt_clock,
        format_digest,
        is_due,
        next_check_delay,
        parse_time,
    )
    from mailer import MailError, Mailer  # type: ignore[no-redef]
    from notify import render_notice  # type: ignore[no-redef]
    from search import format_rows, parse_search_args  # type: ignore[no-redef]

HELP = """AgentMail 邮件工具
· /mail send <收件人> <主题> | <正文>
  例：/mail send 123@qq.com 明天见 | 我去不了，饭钱转你了
· /mail list [数量]   查收件箱
· /mail search <关键词> [数量]  搜历史邮件（主题/发件人/正文，默认 5 条、最多 20）
· /mail read <id>     看某一封
· /mail whoami        看当前邮箱和连通状态
· /mail here          打印当前会话 ID（填「收信推送」用）
· /mail poll          手动跑一次收信轮询"""

CMD_PREFIX = re.compile(r"^[/!！]?\s*(?:mail|邮件)\b\s*", re.IGNORECASE)
SEEN_KEY = "seen_message_ids"
SEEN_LIMIT = 200
# 每日未读摘要「最后一次推送的本地日期」，存插件 KV，进程重启也不会重复推当天
DIGEST_KEY = "digest_last_date"


def _as_bool(value) -> bool:
    """配置里的 bool 判定：兼容 WebUI 存成字符串的 "true"/"false"/"1"/"0"。"""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是", "开")
    return bool(value)


class AgentMailPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.conf = config or {}
        self._mailer = None
        self._poll_task = None
        self._digest_task = None

    # ── 生命周期 ────────────────────────────────────────────
    async def initialize(self):
        """插件加载完成后启动收信轮询 / 每日未读摘要（各自配了才启动）。"""
        await self._start_poll()
        await self._start_digest()

    async def _start_poll(self):
        """启动收信轮询（配了 poll_enabled + notify_sessions 才启动）。"""
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

    async def _start_digest(self):
        """启动每日未读摘要（配了 digest_enabled 才启动）。"""
        if not _as_bool(self.conf.get("digest_enabled")):
            return
        sessions = self._digest_sessions()
        if not sessions:
            logger.warning(
                "[agentmail] 开了每日未读摘要，但 digest_sessions 和 notify_sessions 都是空的，"
                "先跑 /mail here 拿会话 ID"
            )
            return
        hour, minute = self._digest_clock()
        self._digest_task = asyncio.create_task(self._digest_loop())
        logger.info(
            f"[agentmail] 每日未读摘要已启动：每天 {fmt_clock(hour, minute)}（本地时间），"
            f"推送到 {len(sessions)} 个会话"
        )

    async def terminate(self):
        for name, task in (("收信轮询", self._poll_task), ("每日未读摘要", self._digest_task)):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                logger.info(f"[agentmail] {name}已停止")
        self._poll_task = None
        self._digest_task = None

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
        stats = {"new": 0, "pushed": 0, "baseline": 0, "files": 0, "degraded": 0}

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
            text, files = await self._compose_push(m)
            chain = MessageChain([Comp.Plain(text), *files])
            stats["files"] += len(files)
            for s in sessions:
                try:
                    await self.context.send_message(s, chain)
                    stats["pushed"] += 1
                    continue
                except Exception as e:  # noqa: BLE001 单个会话失败不影响其它
                    if not files:  # 纯文字都发不出去，就是会话不可达
                        logger.warning(f"[agentmail] 推送到 {s} 失败：{e}")
                        continue
                    logger.warning(
                        f"[agentmail] 带 {len(files)} 个附件推送到 {s} 失败（{e}），"
                        "降级为纯文字清单重试"
                    )
                try:  # 降级：正文 + 附件清单（清单已经在 text 里）
                    await self.context.send_message(s, MessageChain([Comp.Plain(text)]))
                    stats["pushed"] += 1
                    stats["degraded"] += 1
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[agentmail] 降级后推送到 {s} 仍然失败：{e}")

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
        """推送文案：配了 notify_template 就按模板渲染，否则用内置文案（v0.3.0）。

        v0.5.0：有附件时在末尾追加「附件清单」（文件名 + 可读大小 + 类型），
        没有附件时输出和 v0.2.0/v0.3.0 逐字一致。
        """
        text = render_notice(
            m,
            template=self.conf.get("notify_template") or "",
            preview_len=self.conf.get("notify_preview_len") or 300,
            inbox=self.conf.get("inbox") or "",
            warn=logger.warning,
        )
        block = attachment_block((m or {}).get("attachments"))
        if block:
            text = f"{text}\n{block}"
        return clip_text(text, NOTICE_MAX_CHARS)

    async def _compose_push(self, m: dict) -> tuple[str, list]:
        """（v0.5.0 功能 A）返回 `(推送文案, 要一并发的附件组件列表)`。

        `notify_attachments=false` → 只有文案（里面带附件清单），组件列表为空；
        `notify_attachments=true`  → 再按 `Comp.File(name=..., url=download_url)`
                                     逐个附加，失败只降级成清单 + warning。
        **这个函数永远不抛异常**，正文和清单一定发得出去。
        """
        text = self._compose_notice(m)
        if not _as_bool(self.conf.get("notify_attachments")):
            return text, []
        try:
            files, errors = await self._attachment_components(m)
        except Exception as e:  # noqa: BLE001 兜底：附件这条线绝不能拖垮推送
            logger.warning(f"[agentmail] 处理附件时出错，降级为文字清单：{e}")
            files, errors = [], [f"附件处理异常：{e}"]
        if errors:
            text = f"{text}\n⚠️ 附件没能一起发（正文和清单照发）：{'；'.join(errors[:3])}"
            if len(errors) > 3:
                text = f"{text}…（共 {len(errors)} 个附件失败）"
        return clip_text(text, NOTICE_MAX_CHARS), files

    async def _attachment_components(self, m: dict) -> tuple[list, list[str]]:
        """（v0.5.0 功能 A）把一封邮件的附件变成 `Comp.File` 组件。

        `Comp.File.file` 这个 property 会拿 `url` 自己去下载，所以**不用落临时文件**。
        返回 `(组件列表, 失败说明列表)`；单个附件失败不影响其它附件。
        """
        atts = extract_attachments(m)
        if not atts:
            return [], []
        mid = str((m or {}).get("message_id") or "")
        files: list = []
        errors: list[str] = []
        mailer = self._mailer_or_raise()  # 和轮询共用同一个 Mailer 实例
        for idx, att in enumerate(atts[:MAX_SEND_ATTACHMENTS], 1):
            name = str(att.get("filename") or att.get("name") or f"附件{idx}")
            try:
                aid = str(att.get("attachment_id") or "")
                if not mid or not aid:
                    raise ValueError("缺少 message_id / attachment_id")
                info = await asyncio.to_thread(mailer.attachment_info, mid, aid)
                url = str((info or {}).get("download_url") or "")
                if not url:  # 会过期的临时地址，每次现取，绝不缓存
                    raise ValueError("接口没返回 download_url")
                files.append(Comp.File(name=name, url=url))
            except Exception as e:  # noqa: BLE001 单个附件失败只记 warning
                errors.append(f"{name}：{e}")
                logger.warning(f"[agentmail] 附件「{name}」取下载地址失败：{e}")
        if len(atts) > MAX_SEND_ATTACHMENTS:
            errors.append(f"附件太多，只发前 {MAX_SEND_ATTACHMENTS} 个（共 {len(atts)} 个）")
        return files, errors

    # ── 每日未读摘要（v0.5.0 功能 B） ─────────────────────────
    def _digest_sessions(self) -> list[str]:
        """摘要推到哪：digest_sessions 留空就回退 notify_sessions。"""
        sessions = [str(s) for s in (self.conf.get("digest_sessions") or []) if s]
        if not sessions:
            sessions = [str(s) for s in (self.conf.get("notify_sessions") or []) if s]
        return sessions

    def _digest_clock(self) -> tuple[int, int]:
        """`digest_time` 解析成 (时, 分)；写错了回退 09:00 并记 warning，不抛异常。"""
        return parse_time(self.conf.get("digest_time"), warn=logger.warning)

    async def _digest_loop(self):
        """自带定时：先跑一次（进程中途启动也能补推当天），再睡到下一个到点。"""
        await asyncio.sleep(8)  # 等 AstrBot 平台适配器就绪
        while True:
            try:
                await self._digest_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 摘要出错不能把定时任务搞死
                logger.warning(f"[agentmail] 未读摘要出错：{e}")
            hour, minute = self._digest_clock()
            delay = next_check_delay(datetime.now(), hour, minute)
            logger.info(
                f"[agentmail] 下次未读摘要：{delay / 60:.0f} 分钟后（{fmt_clock(hour, minute)}）"
            )
            await asyncio.sleep(delay)

    async def _digest_once(self) -> dict:
        """到点就统计未读并推一次；同一天只推一次（跨天 / 重启都安全）。"""
        stats = {"unread": 0, "pushed": 0, "skipped": True}
        sessions = self._digest_sessions()
        if not sessions:
            return stats
        hour, minute = self._digest_clock()
        now = datetime.now()  # 本地时间：用 UTC 判断会差 8 小时，跨天容易推两次或整天不推
        last_date = str(await self.get_kv_data(DIGEST_KEY, "") or "")
        if not is_due(now, hour, minute, last_date):
            return stats

        msgs = await asyncio.to_thread(self._mailer_or_raise().list, DIGEST_FETCH)
        unread = [m for m in (msgs or []) if isinstance(m, dict) and self._is_unread(m)]
        text = format_digest(
            len(unread),
            unread,
            inbox=self.conf.get("inbox") or "",
            now=now,
            fetched_cap=DIGEST_FETCH,
        )
        stats["unread"] = len(unread)
        for s in sessions:
            try:
                await self.context.send_message(s, MessageChain([Comp.Plain(text)]))
                stats["pushed"] += 1
            except Exception as e:  # noqa: BLE001 会话不可达只记 warning
                logger.warning(f"[agentmail] 未读摘要推送到 {s} 失败：{e}")
        if stats["pushed"]:
            await self.put_kv_data(DIGEST_KEY, now.date().isoformat())
            stats["skipped"] = False
            logger.info(
                f"[agentmail] 未读摘要已推送：{len(unread)} 封未读，{stats['pushed']} 个会话"
            )
        return stats

    @staticmethod
    def _is_unread(m: dict) -> bool:
        """未读判定：labels 里含 unread、且不是自己发的（sent）。

        没带 labels 的邮件按未读算（AgentMail 新到的就是未读，宁多不漏）。
        """
        labels = (m or {}).get("labels")
        if isinstance(labels, str):
            labels = [labels]
        if not isinstance(labels, (list, tuple)) or not labels:
            return True
        low = [str(x).lower() for x in labels]
        if "sent" in low:
            return False
        return "unread" in low

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
                yield event.plain_result(format_rows(msgs))
            elif action in ("search", "搜", "搜索"):
                keyword, limit = parse_search_args(rest)
                if not keyword:  # 关键词为空 → 只回用法，不去查 API
                    yield event.plain_result(
                        "用法：/mail search <关键词> [数量]\n"
                        "例：/mail search 老登 3\n"
                        "关键词可以是主题、发件人、正文预览里的任意片段（大小写不敏感），"
                        "数量默认 5、最多 20。"
                    )
                    return
                msgs = await asyncio.to_thread(self._mailer_or_raise().search, keyword, limit)
                if not msgs:
                    yield event.plain_result(f"没搜到包含「{keyword}」的邮件")
                    return
                yield event.plain_result(format_rows(msgs))
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
