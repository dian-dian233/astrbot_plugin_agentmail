"""附件清单格式化 + 下载编排（纯标准库，可单独单测）。

AgentMail 侧接口（已用真实 API 核对，见 README「附件接口」一节）：

1. 列表 / 单封详情里的 `attachments` 字段（没有附件时该字段直接不出现）：
   `[{"attachment_id": "...", "filename": "a.txt", "size": 29,
      "content_type": "text/plain", "content_disposition": "attachment"}]`
2. 拿下载地址：`GET /v0/inboxes/{inbox}/messages/{message_id}/attachments/{attachment_id}`
   → `{"attachment_id", "filename", "size", "content_type",
        "download_url", "text_url"? , "expires_at"}`
   `download_url` 是**带签名的 CDN 链接**，下载时不能再带 Authorization 头。
3. 下载：直接 GET 上面的 `download_url`。

职责边界：
- `attachment_lines` / `attachment_block` / `human_size` / `safe_filename`：纯函数，无 IO。
- `download_attachments`：把一封邮件的附件拉到一个临时目录，**单个失败不影响其它**，
  失败只返回 errors（永不抛异常），交给调用方降级成文字清单。
"""

from __future__ import annotations

import os
import re

# 单个附件下载上限（防止有人发个 2G 的附件把容器磁盘塞满）
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
# 一封邮件最多尝试下载几个附件
MAX_ATTACHMENTS_PER_MESSAGE = 5

_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def human_size(size) -> str:
    """字节数转人类可读（29 B / 1.2 KB / 3.4 MB）。非法值返回 "?"。"""
    try:
        n = float(size)
    except (TypeError, ValueError):
        return "?"
    if n < 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def safe_filename(name, fallback: str = "attachment.bin") -> str:
    """把远端文件名洗成能落盘的安全名字（去目录、去特殊字符）。"""
    base = os.path.basename(str(name or "").replace("\\", "/"))
    base = _BAD_NAME_CHARS.sub("_", base).strip().strip(".")
    return base or fallback


def attachment_lines(attachments) -> list[str]:
    """附件清单：每条一行「附件名（大小 · 类型）」。非法输入返回空列表。"""
    if not isinstance(attachments, (list, tuple)):
        return []
    lines = []
    for a in attachments:
        if not isinstance(a, dict):
            continue
        name = a.get("filename") or a.get("name") or "(未命名附件)"
        size = human_size(a.get("size"))
        ctype = a.get("content_type") or a.get("mime_type") or "未知类型"
        lines.append(f"· {name}（{size} · {ctype}）")
    return lines


def attachment_block(attachments) -> str:
    """清单块（没有附件返回空字符串，调用方据此保持 v0.2.0/v0.3.0 原文案不变）。"""
    lines = attachment_lines(attachments)
    if not lines:
        return ""
    return "\n".join(["────────────", f"📎 附件 {len(lines)} 个", *lines])


def _attachment_list(message) -> list[dict]:
    """从 message 里取附件数组（容忍各种脏数据）。"""
    if not isinstance(message, dict):
        return []
    raw = message.get("attachments")
    if not isinstance(raw, (list, tuple)):
        return []
    return [a for a in raw if isinstance(a, dict)]


def download_attachments(mailer, message, tmpdir) -> tuple[list[dict], list[str]]:
    """把一封邮件的附件下载到 tmpdir。

    返回 `(saved, errors)`：
      saved  = [{"filename", "path", "size", "content_type"}]
      errors = ["report.txt：HTTP 404", ...]

    **永不抛异常**：任何问题都进 errors，调用方降级成文字清单即可。
    """
    saved: list[dict] = []
    errors: list[str] = []
    attachments = _attachment_list(message)
    if not attachments:
        return saved, []
    message_id = message.get("message_id") or ""

    for idx, att in enumerate(attachments[:MAX_ATTACHMENTS_PER_MESSAGE], 1):
        name = att.get("filename") or f"attachment-{idx}"
        attachment_id = att.get("attachment_id") or ""
        try:
            if not attachment_id:
                raise ValueError("缺少 attachment_id")
            if isinstance(att.get("size"), (int, float)) and att["size"] > MAX_ATTACHMENT_BYTES:
                raise ValueError(f"附件 {human_size(att['size'])} 超过下载上限 {human_size(MAX_ATTACHMENT_BYTES)}")

            # ① 拿签名下载地址
            info = mailer.attachment_info(message_id, attachment_id) or {}
            url = info.get("download_url") or ""
            if not url:
                raise ValueError("接口没返回 download_url")

            # ② 下载到临时目录（文件名加序号前缀，避免同名互相覆盖）
            filename = safe_filename(info.get("filename") or name, f"attachment-{idx}.bin")
            path = os.path.join(tmpdir, f"{idx}-{filename}")
            size = mailer.download(url, path)
            saved.append(
                {
                    "filename": info.get("filename") or name,
                    "path": path,
                    "size": size,
                    "content_type": info.get("content_type") or att.get("content_type") or "",
                }
            )
        except Exception as e:  # noqa: BLE001 单个附件失败不影响其它附件
            errors.append(f"{name}：{e}")

    if len(attachments) > MAX_ATTACHMENTS_PER_MESSAGE:
        errors.append(
            f"附件太多，只尝试下载前 {MAX_ATTACHMENTS_PER_MESSAGE} 个（共 {len(attachments)} 个）"
        )
    return saved, errors
