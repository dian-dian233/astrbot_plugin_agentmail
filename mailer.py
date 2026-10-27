"""AgentMail 邮件工具本体（纯标准库，不依赖 astrbot，可以单独跑）。

设计要点：
1. 出网双通道：默认直连；如果拿回来的是网关的 HTML 错误页（403/502 之类），
   自动回退到代理再试一次。这样「被墙的内网」和「放通的远程机」都不用改代码。
2. 只做加法：send / list / read / whoami，够用就好，方便别人读代码。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_BASE = "https://api.agentmail.to/v0"
DEFAULT_INBOX = "diandian233@agentmail.to"


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

    def list(self, limit: int = 5) -> list[dict]:
        data = self.request(f"/inboxes/{self.inbox}/messages?limit={max(1, min(50, int(limit)))}")
        return data.get("messages") or data.get("data") or []

    def read(self, message_id: str) -> dict:
        return self.request(f"/inboxes/{self.inbox}/messages/{message_id}")

    def whoami(self) -> dict:
        return self.request(f"/inboxes/{self.inbox}")
