"""AgentMail 邮件工具本体（纯标准库，不依赖 astrbot，可以单独跑）。

设计要点：
1. 出网双通道：默认直连；如果拿回来的是网关的 HTML 错误页（403/502 之类），
   自动回退到代理再试一次。这样「被墙的内网」和「放通的远程机」都不用改代码。
2. 只做加法：send / list / read / whoami / 附件（attachment_info + download），够用就好，方便别人读代码。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "https://api.agentmail.to/v0"
DEFAULT_INBOX = "diandian233@agentmail.to"
# 单个附件落盘上限，兜底防止把容器磁盘写满
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


def _q(value) -> str:
    """把 message_id / inbox / 关键词这类片段塞进 URL 前统一转义。

    AgentMail 的 message_id 长这样：`<010001a1...-000000@email.amazonses.com>`，
    里面的 `<` `>` `@` 不转义会直接拼出坏 URL（实测返回空响应）。
    """
    return urllib.parse.quote(str(value or ""), safe="")



class MailError(Exception):
    """邮件接口出错（带 HTTP 状态码时挂在 .status 上）。"""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _as_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [x.strip() for x in value.replace("；", ",").replace(";", ",").split(",") if x.strip()]
    return [str(x).strip() for x in value if str(x).strip()]


class Mailer:
    def __init__(
        self,
        api_key: str,
        inbox: str = DEFAULT_INBOX,
        proxy: str = "",
        base: str = DEFAULT_BASE,
        timeout: int = 30,
    ):
        if not api_key:
            raise MailError("没有配置 AgentMail API Key")
        self.api_key = api_key.strip()
        self.inbox = (inbox or DEFAULT_INBOX).strip()
        self.proxy = (proxy or "").strip()
        self.base = (base or DEFAULT_BASE).rstrip("/")
        self.timeout = int(timeout or 30)

    # ── 底层 ────────────────────────────────────────────────
    def _raw(self, path: str, method: str, body: dict | None, use_proxy: bool):
        url = self.base + path
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.api_key)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "astrbot-plugin-agentmail/0.1")
        handlers = [
            urllib.request.ProxyHandler(
                {"http": self.proxy, "https": self.proxy} if use_proxy else {}
            )
        ]
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, raw, False
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            is_html = raw.lstrip()[:6].lower().startswith(("<!doct", "<html"))
            return e.code, raw, is_html
        except Exception as e:  # noqa: BLE001 网络层任何异常都算一次失败
            return 0, str(e), False

    def request(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        attempts = [(False, "直连")]
        if self.proxy:
            attempts.append((True, "代理"))
        last_msg = "未知错误"
        for use_proxy, label in attempts:
            status, raw, is_html = self._raw(path, method, body, use_proxy)
            if status and 200 <= status < 300:
                if not raw.strip():
                    return {}
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as e:
                    raise MailError(f"{label}返回的不是 JSON：{raw[:200]}") from e
            # 网关 HTML 拦截页 → 换通道重试；明确的 API 错误 → 直接抛
            if is_html:
                last_msg = f"{label}被网关拦截（HTTP {status}，返回 HTML 页面）"
                continue
            detail = raw.strip()[:300]
            raise MailError(f"HTTP {status}：{detail}", status)
        raise MailError(last_msg)

    # ── 业务 ────────────────────────────────────────────────
    def send(self, to, subject: str, text: str = "", html: str = "") -> dict:
        recipients = _as_list(to)
        if not recipients:
            raise MailError("收件人为空")
        if not subject:
            raise MailError("主题为空")
        payload = {"to": recipients, "subject": subject}
        if text:
            payload["text"] = text
        if html:
            payload["html"] = html
        if not text and not html:
            payload["text"] = "（无正文）"
        return self.request(f"/inboxes/{self.inbox}/messages/send", "POST", payload)

    def list(self, limit: int = 5, labels=None) -> list[dict]:
        """列收件箱（最新在前）。`labels` 可传 ["unread"] 只取未读（接口原生支持）。"""
        size = max(1, min(50, int(limit)))
        path = f"/inboxes/{self.inbox}/messages?limit={size}"
        labels = _as_list(labels)
        if labels:
            path += "&labels=" + urllib.parse.quote(",".join(labels), safe="")
        data = self.request(path)
        return data.get("messages") or data.get("data") or []

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """全文搜索（发件人 / 收件人 / 主题子串 + 正文分词）。

        结果按**相关度**返回，要按时间倒序得调用方自己排（见 search.sort_desc）。
        关键词为空直接抛 MailError（接口对空 q 会 400），别浪费一次请求。
        """
        q = (query or "").strip()
        if not q:
            raise MailError("搜索关键词为空")
        size = max(1, min(100, int(limit or 20)))  # 上限 100，超了接口直接 400
        data = self.request(f"/inboxes/{self.inbox}/messages/search?q={_q(q)}&limit={size}")
        return data.get("messages") or data.get("data") or []

    def read(self, message_id: str) -> dict:
        return self.request(f"/inboxes/{self.inbox}/messages/{_q(message_id)}")

    def whoami(self) -> dict:
        return self.request(f"/inboxes/{self.inbox}")

    # ── 附件 ────────────────────────────────────────────────
    def attachment_info(self, message_id: str, attachment_id: str) -> dict:
        """拿单个附件的元信息（含带签名的 download_url）。"""
        if not message_id or not attachment_id:
            raise MailError("附件请求缺少 message_id / attachment_id")
        return self.request(
            f"/inboxes/{self.inbox}/messages/{_q(message_id)}/attachments/{_q(attachment_id)}"
        )

    def download(self, url: str, dest_path: str, limit: int = MAX_DOWNLOAD_BYTES) -> int:
        """把签名 URL 的附件二进制写到 dest_path，返回字节数。

        - 签名 URL 走 CDN，**不能再带 Authorization 头**（带了反而可能 403）。
        - 和 JSON 请求一样支持直连失败回退代理。
        - 超过 limit 直接中断并删掉半截文件，避免把磁盘写满。
        """
        if not url:
            raise MailError("下载地址为空")
        attempts = [(False, "直连")]
        if self.proxy:
            attempts.append((True, "代理"))
        last_msg = "未知错误"
        for use_proxy, label in attempts:
            handlers = [
                urllib.request.ProxyHandler(
                    {"http": self.proxy, "https": self.proxy} if use_proxy else {}
                )
            ]
            opener = urllib.request.build_opener(*handlers)
            req = urllib.request.Request(url, method="GET")
            req.add_header("User-Agent", "astrbot-plugin-agentmail/0.4")
            total = 0
            try:
                with opener.open(req, timeout=self.timeout) as resp:
                    with open(dest_path, "wb") as f:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > limit:
                                raise MailError(f"附件超过下载上限（>{limit} 字节）")
                            f.write(chunk)
                if total <= 0:
                    raise MailError("下载到 0 字节")
                return total
            except MailError:
                self._unlink(dest_path)
                raise
            except urllib.error.HTTPError as e:
                self._unlink(dest_path)
                last_msg = f"{label}下载失败（HTTP {e.code}）"
            except Exception as e:  # noqa: BLE001 网络层任何异常都算一次失败
                self._unlink(dest_path)
                last_msg = f"{label}下载失败（{e}）"
        raise MailError(last_msg)

    @staticmethod
    def _unlink(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

