"""推送文案渲染（纯标准库，不依赖 astrbot，方便单独单测）。

v0.3.0 新增配置 `notify_template`：

    {subject}  邮件主题
    {from}     发件人
    {preview}  正文预览（仍受 notify_preview_len 截断，超长补 "…"）
    {time}     时间（前 19 位，和内置文案一致）
    {inbox}    插件配置里的收件箱地址

约定：
1. 模板为空（或全是空白）→ 用 v0.2.0 的内置文案，行为完全不变。
2. 模板非法（未知占位符 `{foo}`、花括号没闭合 `{subject`、格式控制等）
   → **不抛异常**，回退内置文案，并通过 warn 回调（默认标准库 logging）
   记一条 warning。
3. v0.4.0 新增附件清单：邮件有附件时，在文案末尾追加「📎 附件 N 个 + 清单」；
   没有附件时输出与 v0.2.0/v0.3.0 逐字一致。
"""

from __future__ import annotations

import logging
import string

try:  # 插件包内导入
    from .attach import attachment_block
except ImportError:  # 单独运行 / 被平铺加载时
    from attach import attachment_block  # type: ignore[no-redef]

DEFAULT_PREVIEW_LEN = 300
PLACEHOLDERS = ("subject", "from", "preview", "time", "inbox")
_ALLOWED = frozenset(PLACEHOLDERS)

logger = logging.getLogger("astrbot_plugin_agentmail")


class TemplateError(ValueError):
    """模板不可用（未知占位符 / 花括号没闭合 / 渲染异常）。"""


def _limit(preview_len) -> int:
    """和 v0.2.0 一致：`int(x or 300)`，非数字或 <=0 都退回 300。"""
    try:
        limit = int(preview_len)
    except (TypeError, ValueError):
        return DEFAULT_PREVIEW_LEN
    return limit if limit > 0 else DEFAULT_PREVIEW_LEN


def preview_text(message: dict, preview_len=DEFAULT_PREVIEW_LEN) -> str:
    """正文预览：和 v0.2.0 的截断规则逐字一致。"""
    message = message or {}
    body = message.get("preview") or message.get("text") or "(正文见邮件)"
    body = str(body).replace("\n\n", "\n").strip()
    limit = _limit(preview_len)
    if len(body) > limit:
        body = body[:limit] + "…"
    return body


def template_values(message: dict, preview_len=DEFAULT_PREVIEW_LEN, inbox: str = "") -> dict:
    """把一封邮件摊平成模板可用的字段。"""
    message = message or {}
    return {
        "subject": str(message.get("subject", "(无主题)")),
        "from": str(message.get("from") or "?"),
        "preview": preview_text(message, preview_len),
        "time": str(message.get("timestamp", ""))[:19],
        "inbox": str(inbox or ""),
    }


def builtin_notice(message: dict, preview_len=DEFAULT_PREVIEW_LEN) -> str:
    """v0.2.0 的内置文案（一行都不能变）。"""
    v = template_values(message, preview_len)
    return (
        "📬 新邮件\n"
        f"主题：{v['subject']}\n"
        f"发件人：{v['from']}\n"
        f"时间：{v['time']}\n"
        "────────────\n"
        f"{v['preview']}"
    )


def render_template(template: str, values: dict) -> str:
    """严格校验后再渲染；任何问题都抛 TemplateError，不抛别的异常。"""
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as e:  # 花括号没闭合之类
        raise TemplateError(f"花括号不合法（{e}）") from e

    unknown = []
    for _, field_name, format_spec, conversion in parsed:
        if field_name is None:  # 纯文本片段
            continue
        if not field_name:
            raise TemplateError("出现了空占位符 {}")
        if field_name not in _ALLOWED:
            unknown.append(field_name)
            continue
        if format_spec or conversion:
            raise TemplateError(f"占位符 {{{field_name}}} 不支持格式控制（如 !r、:>10）")
    if unknown:
        shown = "、".join("{%s}" % x for x in unknown)
        allow = " ".join("{%s}" % p for p in PLACEHOLDERS)
        raise TemplateError(f"未知占位符 {shown}，只能使用 {allow}")

    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError, AttributeError) as e:
        raise TemplateError(f"渲染失败（{e}）") from e


def render_notice(
    message: dict,
    template: str = "",
    preview_len=DEFAULT_PREVIEW_LEN,
    inbox: str = "",
    warn=None,
    attachments=None,
) -> str:
    """渲染推送文案。模板空/非法都回退内置文案，且永远不抛异常。

    v0.4.0：`attachments` 传 None 时自动读 `message["attachments"]`；
    有附件就在文案末尾追加清单，没有附件则输出和 v0.2.0/v0.3.0 逐字一致。
    """
    fallback = builtin_notice(message, preview_len)
    emit = warn or logger.warning
    atts = message.get("attachments") if isinstance(message, dict) else None
    if attachments is not None:
        atts = attachments

    if template is None:
        return _with_attachments(fallback, atts)
    if not isinstance(template, str):
        emit(f"[agentmail] notify_template 类型不对（{type(template).__name__}），已回退内置文案")
        return _with_attachments(fallback, atts)
    if not template.strip():
        return _with_attachments(fallback, atts)

    try:
        text = render_template(template, template_values(message, preview_len, inbox))
    except TemplateError as e:
        emit(f"[agentmail] 推送模板不可用，已回退内置文案：{e}")
        text = fallback
    return _with_attachments(text, atts)


def _with_attachments(text: str, attachments) -> str:
    block = attachment_block(attachments)
    if not block:
        return text
    return f"{text}\n{block}"

