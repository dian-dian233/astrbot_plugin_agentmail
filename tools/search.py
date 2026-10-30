"""历史邮件搜索的纯逻辑（纯标准库，不依赖 astrbot，方便单独单测）。

v0.4.0 新增指令 `/mail search <关键词> [数量]`，这个文件负责三件事：

1. 参数解析：`parse_search_args("老登 3")` → `("老登", 3)`
2. 本地过滤：`filter_messages(msgs, "老登", 5)` —— 服务端搜索端点不可用时的兜底
3. 结果排版：`format_rows(msgs)` —— 和 `/mail list` **完全一致**的行格式

几个刻意的约定：

- 匹配用大小写不敏感的 `in`（子串），**不用正则 `\b` 边界**：中文没有词边界，
  `\b老登\b` 在「老登，你太美」里根本匹配不到；`in` 对中英文都有效。
- 空关键词 → `matches()` 一律 False、`filter_messages()` 返回 `[]`（插件那边会先
  回显用法，压根不会去打 API）。
- 数量非法不抛异常：`normalize_limit()` 把非数字夹回默认 5，把超范围的夹进 [1, 20]。
"""

from __future__ import annotations

DEFAULT_LIMIT = 5
MAX_LIMIT = 20

# 搜索范围：主题 / 发件人 / 正文预览（text 也捎上，服务端有全文但本地兜底时能多命中）
SEARCH_FIELDS = ("subject", "from", "sender", "preview", "text")


def normalize_limit(raw) -> int:
    """把用户给的数量夹成 [1, 20]；非数字（abc / None / 空）一律回默认 5。"""
    if isinstance(raw, bool):  # bool 是 int 子类，别把 True 当 1 用
        return DEFAULT_LIMIT
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, n))


def _is_number_token(token: str) -> bool:
    t = token.strip()
    if not t:
        return False
    if t[0] in "+-":
        t = t[1:]
    return t.isdigit()


def parse_search_args(rest: str) -> tuple[str, int]:
    """拆 `/mail search` 后面的参数：`关键词 [数量]`。

    - 最后一个 token 是数字（含 `-1` / `999`）→ 当数量，其余部分是关键词
    - 否则整串都是关键词（关键词里可以有空格，也可以长得像 `abc`）
    - 空 → `("", 5)`
    """
    text = (rest or "").strip()
    if not text:
        return "", DEFAULT_LIMIT
    parts = text.rsplit(None, 1)
    if len(parts) == 2 and _is_number_token(parts[1]):
        return parts[0].strip(), normalize_limit(parts[1])
    return text, DEFAULT_LIMIT


def matches(message: dict, keyword: str) -> bool:
    """这封邮件是否命中关键词（主题 / 发件人 / 正文预览，大小写不敏感子串）。"""
    kw = (keyword or "").strip().lower()
    if not kw or not isinstance(message, dict):
        return False
    for field in SEARCH_FIELDS:
        value = message.get(field)
        if not value:
            continue
        if isinstance(value, (list, tuple)):
            if any(kw in str(x).lower() for x in value):
                return True
        elif kw in str(value).lower():
            return True
    return False


def filter_messages(messages, keyword: str, limit=DEFAULT_LIMIT) -> list[dict]:
    """本地过滤（服务端搜索不可用时的兜底）：顺序保持原顺序，最多返回 limit 条。"""
    n = normalize_limit(limit)
    if not (keyword or "").strip():
        return []
    out: list[dict] = []
    for m in messages or []:
        if matches(m, keyword):
            out.append(m)
            if len(out) >= n:
                break
    return out


def format_rows(messages) -> str:
    """和 `/mail list` 一模一样的行格式：`时间 · 发件人` / 缩进主题 / 缩进 message_id，
    多条之间空一行。`/mail list` 和 `/mail search` 共用这一个函数，保证逐字一致。"""
    lines = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        frm = m.get("from") or m.get("sender") or "?"
        ts = str(m.get("timestamp", ""))[:19]
        subject = m.get("subject") or "(无主题)"
        lines.append(f"{ts} · {frm}\n  {subject}\n  {m.get('message_id', '')}")
    return "\n\n".join(lines)
