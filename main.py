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
v0.4.0 新增：
    - 附件随推送一并送达（附件清单始终显示；notify_attachments 打开后下载真文件一起发）
    - /mail search <关键词> [数量]
    - 每日未读摘要（digest_enabled / digest_time / digest_sessions）
注意：`mail` 指令**不声明任何参数**，参数直接从 event.message_str 解析。
AstrBot 的指令参数解析遇到 `*args` 会把 `inspect.Parameter.empty` 当类型调用，
直接抛 `_empty() takes no arguments`（v0.1.0 的坑，v0.1.1 修掉）。
"""

import asyncio
import contextlib
import re
import shutil
import tempfile
from datetime import datetime

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

try:  # 插件包内导入
    from .attach import attachment_block, download_attachments
    from .digest import format_digest
    from .digest import is_due as digest_is_due
    from .digest import parse_time as parse_digest_time
    from .mailer import MailError, Mailer
    from .notify import render_notice
    from .search import (
        LOCAL_FALLBACK_FETCH,
        USAGE as SEARCH_USAGE,
        format_results as format_search_results,
        local_search,
        parse_args as parse_search_args,
        sort_desc,
    )
except ImportError:  # 单独运行 / 被平铺加载时
    from attach import attachment_block, download_attachments  # type: ignore[no-redef]
    from digest import format_digest  # type: ignore[no-redef]
    from digest import is_due as digest_is_due  # type: ignore[no-redef]
    from digest import parse_time as parse_digest_time  # type: ignore[no-redef]
    from mailer import MailError, Mailer  # type: ignore[no-redef]
    from notify import render_notice  # type: ignore[no-redef]
    from search import (  # type: ignore[no-redef]
        LOCAL_FALLBACK_FETCH,
        USAGE as SEARCH_USAGE,
        format_results as format_search_results,
        local_search,
        parse_args as parse_search_args,
        sort_desc,
    )

HELP = """AgentMail 邮件工具
· /mail send <收件人> <主题> | <正文>
  例：/mail send 123@qq.com 明天见 | 我去不了，饭钱转你了
· /mail list [数量]   查收件箱
· /mail search <关键词> [数量]   搜历史邮件（关键词可带空格，数量 1-100）
· /mail read <id>     看某一封
· /mail whoami        看当前邮箱和连通状态
· /mail here          打印当前会话 ID（填「收信推送」用）
· /mail poll          手动跑一次收信轮询
· /mail digest        立刻推一次未读摘要（不影响每天定时那次）"""

CMD_PREFIX = re.compile(r"^[/!！]?\s*(?:mail|邮件)\b\s*", re.IGNORECASE)
SEEN_KEY = "seen_message_ids"
SEEN_LIMIT = 200
DIGEST_KEY = "digest_last_date"
DIGEST_TICK = 30  # 摘要调度每 30 秒看一眼表（纯本地计算，很便宜）
DIGEST_MAX_FETCH = 50  # 最多拉 50 封未读来统计/挑最新几封


class AgentMailPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.conf = config or {}
        self._mailer = None
        self._poll_task = None
        self._digest_task = None
        self._digest_targets = []

    # ── 生命周期 ────────────────────────────────────────────
    async def initialize(self):
        """插件加载完成后按配置启动后台任务（收信轮询 / 每日摘要，都是配了才启动）。"""
        self._start_polling()
        self._start_digest()

    def _start_polling(self):
        """v0.2.0 的收信轮询启动逻辑（行为保持不变）。"""
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

    def _start_digest(self):
        """v0.4.0 每日未读摘要的调度启动（插件自带定时，不靠外部 cron）。"""
        if not self.conf.get("digest_enabled"):
            return
        targets = self._digest_sessions()
        if not targets:
            logger.warning(
                "[agentmail] 开了每日未读摘要，但 digest_sessions 和 notify_sessions 都是空的，先跑 /mail here 拿会话 ID"
            )
            return
        self._digest_targets = targets
        hour, minute = self._digest_clock()
        self._digest_task = asyncio.create_task(self._digest_loop())
        logger.info(
            f"[agentmail] 每日未读摘要已启动：每天 {hour:02d}:{minute:02d}（本地时间）→ {len(targets)} 个会话"
        )

    async def terminate(self):
        for task, name in ((self._poll_task, "收信轮询"), (self._digest_task, "每日摘要")):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                logger.info(f"[agentmail] {name}已停止")

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
        stats = {"new": 0, "pushed": 0, "baseline": 0, "degraded": 0, "files": 0}

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
            # 附件：清单已经在 text 里了（render_notice 追加的），这里按配置决定要不要发真文件
            saved, errors, tmpdir = await self._prepare_attachments(m)
            degrade_note = ""
            if errors:
                degrade_note = "\n（附件没发出去，只有上面的文字清单：%s）" % "；".join(errors)
            try:
                try:
                    chain = [Comp.Plain(text + degrade_note)]
                    for item in saved:
                        chain.append(self._file_component(item))
                except Exception as e:  # noqa: BLE001 组件不可用（老版本 AstrBot）也要降级
                    logger.warning(f"[agentmail] 文件消息组件不可用，降级纯文字：{e}")
                    chain = [
                        Comp.Plain(text + degrade_note + "\n（附件文件发送不可用，已降级为文字清单）")
                    ]
                    saved = []
                for s in sessions:
                    try:
                        await self.context.send_message(s, MessageChain(list(chain)))
                        stats["pushed"] += 1
                        stats["files"] += len(saved)
                    except Exception as e:  # noqa: BLE001 单个会话失败不影响其它
                        if not saved:
                            logger.warning(f"[agentmail] 推送到 {s} 失败：{e}")
                            continue
                        # 文件消息发不出去（适配器不支持 etc.）→ 降级纯文字，整轮推送不能挂
                        logger.warning(f"[agentmail] 带附件的推送发往 {s} 失败，降级纯文字：{e}")
                        try:
                            await self.context.send_message(
                                s,
                                MessageChain(
                                    [
                                        Comp.Plain(
                                            text
                                            + degrade_note
                                            + "\n（附件文件发送失败，已降级为文字清单）"
                                        )
                                    ]
                                ),
                            )
                            stats["pushed"] += 1
                            stats["degraded"] += 1
                        except Exception as e2:  # noqa: BLE001
                            logger.warning(f"[agentmail] 降级纯文字也失败了 {s}：{e2}")
            finally:
                self._cleanup_dir(tmpdir)

        ids = [str(m.get("message_id")) for m in fresh if m.get("message_id")]
        await self.put_kv_data(SEEN_KEY, (ids + seen)[:SEEN_LIMIT])
        logger.info(f"[agentmail] 推送了 {len(fresh)} 封新邮件")
        return stats

    # ── 附件（v0.4.0）────────────────────────────────────────
    async def _prepare_attachments(self, m: dict):
        """按 `notify_attachments` 配置把附件下到临时目录。

        返回 `(saved, errors, tmpdir)`；`notify_attachments` 关着时直接
        `([], [], None)`，行为与 v0.3.0 完全一致（只多一行附件清单文字）。
        任何异常都在这里吃掉，最多让这一封的附件降级成文字清单。
        """
        atts = m.get("attachments")
        if not self.conf.get("notify_attachments"):
            return [], [], None
        if not isinstance(atts, (list, tuple)) or not atts:
            return [], [], None

        tmpdir = None
        try:
            tmpdir = await asyncio.to_thread(tempfile.mkdtemp, prefix="agentmail-")
            saved, errors = await asyncio.to_thread(
                download_attachments, self._mailer_or_raise(), m, tmpdir
            )
            if errors:
                logger.warning(f"[agentmail] 附件下载部分失败：{'；'.join(errors)}")
            if saved:
                logger.info(f"[agentmail] 附件已下载 {len(saved)} 个到临时目录")
            return saved, errors, tmpdir
        except Exception as e:  # noqa: BLE001 附件永远不能把整轮推送搞挂
            logger.warning(f"[agentmail] 附件准备失败，降级为文字清单：{e}")
            self._cleanup_dir(tmpdir)
            return [], [f"下载失败（{e}）"], None

    @staticmethod
    def _file_component(item: dict):
        """AstrBot 文件消息组件：`astrbot.api.message_components.File(name=..., file=...)`。

        File 的构造签名（astrbot/core/message/components.py）：
            File(name: str, file: str = "", url: str = "")
        """
        file_cls = getattr(Comp, "File", None)
        if file_cls is None:  # 老版本 AstrBot 没有 File 组件
            raise RuntimeError("当前 AstrBot 没有 message_components.File")
        return file_cls(name=item.get("filename") or "attachment", file=item.get("path") or "")

    @staticmethod
    def _cleanup_dir(tmpdir) -> None:
        """临时文件用完就删，别把容器磁盘塞满。"""
        if not tmpdir:
            return
        shutil.rmtree(tmpdir, ignore_errors=True)

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

    # ── 每日未读摘要（v0.4.0）────────────────────────────────
    def _digest_clock(self) -> tuple[int, int]:
        """读 digest_time；写错回退 09:00 + warning（不抛异常）。"""
        return parse_digest_time(self.conf.get("digest_time"), warn=logger.warning)

    def _digest_sessions(self) -> list[str]:
        """digest_sessions 优先，留空则用 notify_sessions。"""
        own = [s for s in (self.conf.get("digest_sessions") or []) if s]
        if own:
            return own
        return [s for s in (self.conf.get("notify_sessions") or []) if s]

    async def _digest_loop(self):
        """每 30 秒看一眼表；到点且今天没推过就推一次。"""
        await asyncio.sleep(10)  # 等平台适配器就绪
        while True:
            try:
                await self._maybe_digest()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 摘要出错不能把任务搞死
                logger.warning(f"[agentmail] 每日摘要出错：{e}")
            await asyncio.sleep(DIGEST_TICK)

    async def _maybe_digest(self, now: datetime | None = None):
        """到点 + 今天还没推过 → 推一次并记下日期；否则什么都不做。"""
        targets = self._digest_targets or self._digest_sessions()
        if not targets:
            return None
        now = now or datetime.now()  # 本地时间：跨天判断必须用本地，别用 UTC
        hour, minute = self._digest_clock()
        last = str(await self.get_kv_data(DIGEST_KEY, "") or "")
        if not digest_is_due(now, hour, minute, last):
            return None
        stats = await self._push_digest(targets, now)
        # 先记日期再返回：进程重启 / 30 秒轮询都不会把同一天的摘要推第二遍
        await self.put_kv_data(DIGEST_KEY, now.date().isoformat())
        return stats

    async def _push_digest(self, targets, now: datetime | None = None) -> dict:
        now = now or datetime.now()
        try:
            unread, latest = await self._collect_unread()
        except Exception as e:  # noqa: BLE001 取数失败只记 warning，不影响定时任务
            logger.warning(f"[agentmail] 每日摘要统计未读失败：{e}")
            return {"pushed": 0, "unread": 0, "error": str(e)}

        text = format_digest(
            unread,
            latest,
            inbox=self.conf.get("inbox") or "",
            now=now,
            fetched_cap=DIGEST_MAX_FETCH,
        )
        pushed = 0
        for s in targets:
            try:
                await self.context.send_message(s, MessageChain([Comp.Plain(text)]))
                pushed += 1
            except Exception as e:  # noqa: BLE001 单个会话失败不影响其它
                logger.warning(f"[agentmail] 摘要推送到 {s} 失败：{e}")
        if pushed:
            logger.info(f"[agentmail] 每日未读摘要已推送：未读 {unread} 封 → {pushed} 个会话")
        else:
            logger.warning("[agentmail] 每日未读摘要一条都没推出去")
        return {"pushed": pushed, "unread": unread}

    async def _collect_unread(self, limit: int = DIGEST_MAX_FETCH):
        """统计未读封数 + 最新 3 封。Mailer 和收信轮询共用同一个实例。"""
        mailer = self._mailer_or_raise()
        msgs = await asyncio.to_thread(mailer.list, limit, ["unread"])
        unread = [m for m in msgs if self._is_unread(m)]
        return len(unread), sort_desc(unread)[:3]

    @staticmethod
    def _is_unread(m: dict) -> bool:
        labels = (m or {}).get("labels") or []
        if isinstance(labels, str):
            labels = [labels]
        return "unread" in [str(x).lower() for x in labels]

    # ── 搜索（v0.4.0）────────────────────────────────────────
    async def _search_mail(self, keyword: str, count: int):
        """走 AgentMail search 接口；接口不可用就降级成「最近 N 封本地过滤」。

        返回 `(messages, note)`，note 是给用户看的降级说明（没降级就是空串）。
        """
        mailer = self._mailer_or_raise()
        try:
            msgs = await asyncio.to_thread(mailer.search, keyword, count)
            return sort_desc(msgs)[:count], ""
        except MailError as e:
            # 接口不支持 / 参数不被接受 / 老部署没有这个路由 → 本地过滤兜底
            logger.warning(f"[agentmail] 搜索接口不可用，降级为本地过滤最近邮件：{e}")
            fetch = max(LOCAL_FALLBACK_FETCH, min(50, count))
            recent = await asyncio.to_thread(mailer.list, fetch)
            hits = local_search(recent, keyword, count)
            return (
                hits,
                f"（搜索接口不可用，已降级为在最近 {len(recent)} 封里本地过滤）",
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
            elif action in ("search", "搜", "搜索", "查找"):
                keyword, count = parse_search_args(rest)
                if not keyword:
                    # 空关键词给用法，不抛异常、也不浪费一次接口请求
                    yield event.plain_result(SEARCH_USAGE)
                    return
                found, note = await self._search_mail(keyword, count)
                text = format_search_results(keyword, found)
                yield event.plain_result(f"{text}\n{note}" if note else text)
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
            elif action in ("digest", "摘要", "未读摘要"):
                targets = self._digest_sessions()
                if not targets:
                    yield event.plain_result(
                        "还没配推送目标：把 digest_sessions 或 notify_sessions 填上（/mail here 拿会话 ID）。"
                    )
                    return
                stats = await self._push_digest(targets)
                if stats.get("error"):
                    yield event.plain_result(f"统计未读出错了：{stats['error']}")
                else:
                    yield event.plain_result(
                        f"未读 {stats['unread']} 封，摘要已推到 {stats['pushed']} 个会话。\n"
                        "（手动触发不写「今天已推过」的标记，每天的定时摘要照旧）"
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
        text = (
            f"主题：{m.get('subject', '(无主题)')}\n"
            f"发件人：{frm}\n收件人：{to or '?'}\n时间：{m.get('timestamp', '?')}\n"
            f"────────────\n{str(body)[:1500]}"
        )
        block = attachment_block(m.get("attachments"))  # v0.4.0：读信也带上附件清单
        return f"{text}\n{block}" if block else text

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
