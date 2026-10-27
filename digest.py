"""每日未读摘要的纯逻辑（时间解析 / 到点判断 / 文案排版）。

三个关键点，都是踩过坑的地方：

1. **到点判断用本地时间**：`datetime.now()` 是本地时区。如果用 UTC 判断，
   东八区会晚 8 小时，跨天那一刻容易出现「一天推两次」或「整天不推」。
2. **同一天只推一次**：`is_due()` 会带上「上次推送的日期」，日期相同就不再推；
   日期存在插件 KV 里，进程重启也记得住。
3. **时间字符串写错不抛异常**：`parse_time()` 回退 `09:00` 并返回 warning 文本，
   由调用方决定怎么记日志。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

try:  # 插件包内导入
    from .search import sort_desc
except ImportError:  # 单独运行 / 被平铺加载时
    from search import sort_desc  # type: ignore[no-redef]

DEFAULT_TIME = "09:00"
DEFAULT_HOUR = 9
DEFAULT_MINUTE = 0
DEFAULT_COUNT_SHOWN = 3
_TIME_RE = re.compile(r"^(\d{1,2})\s*[:：]\s*(\d{1,2})$")


def parse_time(raw, warn=None) -> tuple[int, int]:
    """把 `"09:00"` 解析成 `(9, 0)`。

    - 空值 → 默认 (9, 0)，不吵 warning（视为「没配」）
    - 写错（`"9点"`、`"25:00"`、`"9:70"`、`123`）→ 回退 (9, 0) 并 warn，**不抛异常**
    """
    text = "" if raw is None else str(raw).strip()
    if not text:
        return DEFAULT_HOUR, DEFAULT_MINUTE
    m = _TIME_RE.match(text.replace(" ", ""))
    if not m:
        _emit(warn, f"[agentmail] digest_time 格式不对（{text!r}），已回退 {DEFAULT_TIME}；正确写法如 09:00")
        return DEFAULT_HOUR, DEFAULT_MINUTE
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        _emit(warn, f"[agentmail] digest_time 超出范围（{text!r}），已回退 {DEFAULT_TIME}；小时 0-23、分钟 0-59")
        return DEFAULT_HOUR, DEFAULT_MINUTE
    return hour, minute


def _emit(warn, message: str) -> None:
    if warn:
        warn(message)
    else:
        import logging

        logging.getLogger("astrbot_plugin_agentmail").warning(message)


def is_due(now: datetime, hour: int, minute: int, last_date: str = "") -> bool:
    """到点了吗？且今天还没推过。

    `last_date` 是上一次推送的本地日期（`"2026-10-27"`），同一天一律返回 False。
    """
    if now is None:
        return False
    today = now.date().isoformat()
    if str(last_date or "") == today:
        return False
    return (now.hour, now.minute) >= (hour, minute)


def fmt_ts(timestamp, tz=None) -> str:
    """`2026-10-27T10:24:08.175Z` → `10-27 10:24`（默认转本地时区）。"""
    raw = str(timestamp or "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:16].replace("T", " ") or "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    target = tz or dt.astimezone().tzinfo
    return dt.astimezone(target).strftime("%m-%d %H:%M")


def unread_label(count: int, fetched_cap: int = 50) -> str:
    """未读数量文案；达到拉取上限时标 `50+`，避免报一个假的精确数字。"""
    if count >= fetched_cap:
        return f"{count}+ 封"
    return f"{count} 封"


def format_digest(
    unread_count: int,
    latest,
    inbox: str = "",
    now: datetime | None = None,
    tz=None,
    fetched_cap: int = 50,
) -> str:
    """摘要文案。

    - `unread_count`：未读封数
    - `latest`：最新几封（这里会再按时间倒序取前 3，调用方给乱序也行）
    """
    now = now or datetime.now()
    lines = [f"📬 每日未读摘要 · {now.strftime('%Y-%m-%d')}"]
    if inbox:
        lines.append(f"收件箱：{inbox}")
    if not unread_count:
        lines.append("未读邮件：0 封，邮箱是干净的 ✨")
        return "\n".join(lines)

    lines.append(f"未读邮件：{unread_label(int(unread_count), fetched_cap)}")
    top = sort_desc([m for m in (latest or []) if isinstance(m, dict)])[:DEFAULT_COUNT_SHOWN]
    if top:
        lines.append("────────────")
        lines.append(f"最新 {len(top)} 封：")
        for i, m in enumerate(top, 1):
            frm = m.get("from") or m.get("sender") or "?"
            subject = m.get("subject") or "(无主题)"
            lines.append(f"{i}. {fmt_ts(m.get('timestamp'), tz)} · {frm} · {subject}")
    return "\n".join(lines)


def next_check_delay(now: datetime, hour: int, minute: int) -> float:
    """（给调度循环用）距离下一次「到点」还有多少秒，最少 1 秒。"""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return max(1.0, (target - now).total_seconds())
