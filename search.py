"""`/mail search` 的纯逻辑（参数解析 / 排序 / 本地降级过滤 / 输出排版）。

为什么单独一个文件：这些函数不碰网络、不依赖 astrbot，selftest 里能直接断言。

AgentMail 的搜索接口（已用真实 API 核对）：
    GET /v0/inboxes/{inbox}/messages/search?q=<关键词>&limit=<1-100>
- `q` 必填，空串直接 400（所以关键词为空要在本地就拦住）
- `limit` 上限 100，超了 400
- 结果**按相关度排序，不是按时间**（所以插件自己要重排成时间倒序）
- 参数名是 `q`：传 `query=` 会 400；把 `q` 拼到 `/messages` 上会被忽略（等于没筛）
"""

from __future__ import annotations

from datetime import datetime, timezone

DEFAULT_COUNT = 10
MAX_COUNT = 100
LOCAL_FALLBACK_FETCH = 20  # 降级时先拉最近 N 封做本地过滤

USAGE = (
    "用法：/mail search <关键词> [数量]\n"
    "例：/mail search 发票 5\n"
    "数量 1-100，默认 10；结果按时间倒序，复制 id 接着 /mail read <id> 看正文。"
)


def parse_args(rest: str) -> tuple[str, int]:
    """解析 `关键词 [数量]`。

    - 关键词允许带空格（`/mail search 年度 总结 3` → 关键词「年度 总结」，数量 3）
    - 末尾是纯数字才当数量；只有一个 token 且是数字时仍当关键词（不瞎猜）
    """
    text = (rest or "").strip()
    if not text:
        return "", DEFAULT_COUNT
    parts = text.split()
    count = DEFAULT_COUNT
    if len(parts) > 1 and parts[-1].isdigit():
        count = int(parts[-1])
        parts = parts[:-1]
    count = max(1, min(MAX_COUNT, count))
    return " ".join(parts).strip(), count


def ts_key(message: dict):
    """时间排序键：统一成 aware datetime，脏数据排最后。"""
    raw = str((message or {}).get("timestamp") or "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def sort_desc(messages) -> list[dict]:
    """按时间倒序（接口给的是相关度排序，这里要自己排）。"""
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    return sorted(msgs, key=ts_key, reverse=True)


def _haystack(message: dict) -> str:
    m = message or {}
    to = m.get("to")
    if isinstance(to, (list, tuple)):
        to = " ".join(str(x) for x in to)
    parts = [
        m.get("subject"),
        m.get("from"),
        m.get("sender"),
        m.get("preview"),
        m.get("text"),
        to,
    ]
    return " ".join(str(p) for p in parts if p)


def matches(message: dict, keyword: str) -> bool:
    """本地子串匹配（大小写不敏感）：主题 / 发件人 / 正文预览 / 收件人。"""
    kw = (keyword or "").strip().casefold()
    if not kw:
        return False
    return kw in _haystack(message).casefold()


def local_search(messages, keyword: str, count: int = DEFAULT_COUNT) -> list[dict]:
    """接口不可用时的降级方案：在最近 N 封里本地过滤，仍然按时间倒序。"""
    hits = [m for m in (messages or []) if isinstance(m, dict) and matches(m, keyword)]
    return sort_desc(hits)[: max(1, int(count or DEFAULT_COUNT))]


def format_item(message: dict) -> str:
    """一条结果：时间 · 发件人 · 主题，下一行是 message_id（方便接着 /mail read）。"""
    ts = str(message.get("timestamp") or "")[:19].replace("T", " ")
    frm = message.get("from") or message.get("sender") or "?"
    subject = message.get("subject") or "(无主题)"
    return f"{ts} · {frm} · {subject}\n  {message.get('message_id', '')}"


def format_results(keyword: str, messages) -> str:
    """整段输出。空结果要有明确文案，不能只返回一个空字符串。"""
    msgs = list(messages or [])
    if not msgs:
        return f"没搜到包含「{keyword}」的邮件。换个关键词试试？"
    lines = [f"🔍 「{keyword}」找到 {len(msgs)} 封（按时间倒序）："]
    lines.extend(format_item(m) for m in msgs)
    lines.append("接着看某封：/mail read <上面的 id>")
    return "\n".join(lines)
