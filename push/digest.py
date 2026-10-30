"""每日未读摘要的纯逻辑（时间解析 / 到点判断 / 文案排版）。

v0.5.0 功能 B，纯标准库、无 IO，方便单独单测。三个刻意的约定：

1. **到点判断用本地时间**：`datetime.now()` 是本地时区。用 UTC 判断的话，
   东八区会差 8 小时，跨天那一刻容易「一天推两次」或「整天不推」。
2. **同一天只推一次**：`is_due()` 带上「上次推送的本地日期」，日期相同就不推；
   日期存在插件 KV（`digest_last_date`）里，进程重启也记得住。
3. **时间字符串写错不抛异常**：`parse_time()` 回退 `09:00` 并返回 warning 文本，
   由调用方决定怎么记日志（`"25:00"` / `"9点"` / `""` 都走这条路）。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

DEFAULT_DIGEST_TIME = "09:00"
DEFAULT_HOUR = 9
DEFAULT_MINUTE = 0
# 摘要里列几封最新邮件
DIGEST_LATEST = 3
# 拉多少封来统计未读（同时也是「N+ 封」的上限口径）
DIGEST_FETCH = 50

_TIME_RE = re.compile(r"^(\d{1,2})\s*[:：]\s*(\d{1,2})$")


def _emit(warn, message: str) -> None:
    if warn:
        warn(message)
        return
    import logging

    logging.getLogger("astrbot_plugin_agentmail").warning(message)


def parse_time(raw, warn=None) -> tuple[int, int]:
    """把 `"09:00"` 解析成 `(9, 0)`。

    - `None`（压根没配）→ 默认 `(9, 0)`，不吵 warning
    - 写错（`""` / `"9点"` / `"25:00"` / `"9:70"` / `123`）→ 回退 `(9, 0)` + warning，
      **不抛异常**
    """
    if raw is None:
        return DEFAULT_HOUR, DEFAULT_MINUTE
    text = str(raw).strip()
    if not text:
        _emit(warn, f"[agentmail] digest_time 是空的，已回退 {DEFAULT_DIGEST_TIME}")
        return DEFAULT_HOUR, DEFAULT_MINUTE
    m = _TIME_RE.match(text.replace(" ", ""))
    if not m:
        _emit(warn, f"[agentmail] digest_time 格式不对（{text!r}），已回退 {DEFAULT_DIGEST_TIME}；正确写法如 09:00")
        return DEFAULT_HOUR, DEFAULT_MINUTE
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        _emit(
            warn,
            f"[agentmail] digest_time 超出范围（{text!r}），已回退 {DEFAULT_DIGEST_TIME}；小时 0-23、分钟 0-59",
        )
        return DEFAULT_HOUR, DEFAULT_MINUTE
    return hour, minute


def fmt_clock(hour: int, minute: int) -> str:
    """`(9, 0)` → `"09:00"`（日志用）。"""
    return f"{int(hour):02d}:{int(minute):02d}"


def is_due(now: datetime, hour: int, minute: int, last_date: str = "") -> bool:
    """到点了吗，且今天还没推过？

    `last_date` 是上一次推送的本地日期（`"2026-10-27"`），同一天一律 False。
    `now` 已经过了今天的点到时间也算到点（进程中途启动时补推一次，不漏天）。
    """
    if not isinstance(now, datetime):
        return False
    today = now.date().isoformat()
    if str(last_date or "") == today:
        return False
    return (now.hour, now.minute) >= (int(hour), int(minute))


def next_check_delay(now: datetime, hour: int, minute: int) -> float:
    """距下一次「到点」还有多少秒（最少 1 秒），给 asyncio 循环用。"""
    if not isinstance(now, datetime):
        return 60.0
    target = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def _ts_key(message: dict):
    """排序键：能解析成时间的按时间，解析不了的一律排最后。"""
    raw = str((message or {}).get("timestamp") or "")
    try:
        return (1, datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except (ValueError, OSError, OverflowError):
        return (0, 0.0)


def sort_latest(messages, count: int = DIGEST_LATEST) -> list[dict]:
    """取最新 `count` 封（按 timestamp 倒序；时间解析不了的沉底）。"""
    items = [m for m in (messages or []) if isinstance(m, dict)]
    items.sort(key=_ts_key, reverse=True)
    try:
        count = max(0, int(count))
    except (TypeError, ValueError):
        count = DIGEST_LATEST
    return items[:count]


def fmt_ts(timestamp) -> str:
    """`2026-10-27T10:24:08.175Z` → `10-27 10:24`（转成本地时区）。"""
    raw = str(timestamp or "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:16].replace("T", " ") or "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%m-%d %H:%M")


def unread_label(count: int, fetched_cap: int = DIGEST_FETCH) -> str:
    """未读数量文案；拉满了就标 `50+`，别报一个假的精确数字。"""
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0
    if fetched_cap and count >= int(fetched_cap):
        return f"{count}+ 封"
    return f"{count} 封"


def format_digest(
    unread_count: int,
    latest,
    inbox: str = "",
    now: datetime | None = None,
    fetched_cap: int = DIGEST_FETCH,
) -> str:
    """摘要正文：未读数量 + 最新 3 封的「时间 · 发件人 · 主题」。"""
    now = now if isinstance(now, datetime) else datetime.now()
    lines = [f"📬 每日未读摘要 · {now.strftime('%Y-%m-%d')}"]
    if inbox:
        lines.append(f"收件箱：{inbox}")
    if not unread_count:
        lines.append("未读邮件：0 封，邮箱是干净的 ✨")
        return "\n".join(lines)

    lines.append(f"未读邮件：{unread_label(unread_count, fetched_cap)}")
    top = sort_latest(latest, DIGEST_LATEST)
    if top:
        lines.append("────────────")
        lines.append(f"最新 {len(top)} 封：")
        for i, m in enumerate(top, 1):
            frm = m.get("from") or m.get("sender") or "?"
            subject = m.get("subject") or "(无主题)"
            lines.append(f"{i}. {fmt_ts(m.get('timestamp'))} · {frm} · {subject}")
    return "\n".join(lines)
