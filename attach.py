"""附件清单的纯逻辑（纯标准库，不依赖 astrbot，方便单独单测）。

v0.5.0 功能 A「附件随推送一并送达」用到的 AgentMail 事实（已核对，不再猜）：

1. message 对象自带 `attachments` 字段，元素含
   `attachment_id` / `filename` / `size` / `content_type`；没有附件时该字段可能
   缺失、是 None、是空 list，甚至有脏数据 —— 这个文件负责安全地跳过。
2. 下载地址：`GET /v0/inboxes/{inbox}/messages/{message_id}/attachments/{attachment_id}`
   返回 `download_url` / `text_url` / `expires_at`。**会过期，不许缓存**。

职责边界：
- `human_size` / `extract_attachments` / `attachment_lines` / `attachment_block` /
  `clip_text`：纯函数，无 IO、无副作用，永远不抛异常。
- 真正把附件发出去由 main.py 用 `Comp.File(name=..., url=download_url)` 干，
  组件自己会拿 url 去下载，这里不需要落临时文件。
"""

from __future__ import annotations

# 一封邮件最多在推送里列几个附件（再多就折叠成一行，别把消息塞爆）
MAX_LIST_ITEMS = 8
# 一封邮件最多尝试发送几个附件文件（防止有人发 50 个附件刷屏）
MAX_SEND_ATTACHMENTS = 5
# 推送文案（正文预览 + 模板 + 附件清单）拼接后的总长度上限
NOTICE_MAX_CHARS = 1500


def human_size(size) -> str:
    """字节数转人类可读：`29B` / `12.3KB` / `3.4MB`。非法值返回 `?`。"""
    try:
        n = float(size)
    except (TypeError, ValueError):
        return "?"
    if n != n or n < 0:  # NaN / 负数
        return "?"
    if n < 1024:
        return f"{int(n)}B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024.0
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
    return f"{n:.1f}GB"


def extract_attachments(message) -> list[dict]:
    """从 message 里安全取出附件数组。

    `attachments` 为空 / 字段缺失 / 不是 list / 元素不是 dict → 一律返回 `[]`，
    绝不抛异常（脏数据不能把整轮推送搞挂）。
    """
    if not isinstance(message, dict):
        return []
    raw = message.get("attachments")
    if not isinstance(raw, (list, tuple)):
        return []
    return [a for a in raw if isinstance(a, dict)]


def _attachment_name(att: dict, index: int) -> str:
    name = att.get("filename") or att.get("name") or ""
    name = str(name).strip()
    return name or f"附件{index}"


def attachment_lines(attachments) -> list[str]:
    """附件清单行：`· report.pdf（12.3KB · application/pdf）`。非法输入返回 `[]`。"""
    items = attachments if isinstance(attachments, (list, tuple)) else []
    items = [a for a in items if isinstance(a, dict)]
    if not items:
        return []
    lines: list[str] = []
    for idx, att in enumerate(items[:MAX_LIST_ITEMS], 1):
        name = _attachment_name(att, idx)
        size = human_size(att.get("size"))
        ctype = str(att.get("content_type") or att.get("mime_type") or "未知类型")
        lines.append(f"· {name}（{size} · {ctype}）")
    if len(items) > MAX_LIST_ITEMS:
        lines.append(f"· …还有 {len(items) - MAX_LIST_ITEMS} 个附件（共 {len(items)} 个）")
    return lines


def attachment_block(attachments) -> str:
    """清单块（没有附件时返回空字符串，调用方据此保持 v0.2.0/v0.3.0 原文案不变）。"""
    items = [a for a in attachments if isinstance(a, dict)] if isinstance(attachments, (list, tuple)) else []
    lines = attachment_lines(items)
    if not lines:
        return ""
    return "\n".join(["────────────", f"📎 附件 {len(items)} 个", *lines])


def clip_text(text: str, limit: int = NOTICE_MAX_CHARS) -> str:
    """总长度兜底：超长截断并补 `…`，防止模板 + 清单拼出一条几万字的推送。"""
    text = str(text or "")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = NOTICE_MAX_CHARS
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "…"
