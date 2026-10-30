"""AgentMail 邮件工具本体（纯标准库，不依赖 astrbot，可以单独跑）。

设计要点：
1. 出网双通道：默认直连；如果拿回来的是网关的 HTML 错误页（403/502 之类），
   自动回退到代理再试一次。这样「被墙的内网」和「放通的远程机」都不用改代码。
2. 只做加法：send / list / read / whoami / search，够用就好，方便别人读代码。
3. v0.4.0 加了 `search()`：优先走 AgentMail 服务端搜索端点
   `GET /v0/inboxes/{inbox}/messages/search?q=<关键词>&limit=<n>`（参数名取自官方
   OpenAPI：只有 `q` / `limit` / `page_token` / `before` / `after`，没有 body）。
   该端点报 400/404/405/422/501（例如套餐或权限不支持）时，自动退化为
   `list(50)` 拉下来用 search.py 里的纯函数本地过滤，并在日志里记一条 warning。
4. v0.5.0 加了 `attachment_info()`：拿附件的 `download_url`（带签名、会过期，
   所以每次发附件都重新问一次，不做任何缓存）。
5. v0.6.0 的 `send()` 支持真 `cc` / `bcc`：AgentMail 的 SendMessageRequest 本身就有这两个
   字段（Addresses = string | list of string），见
   https://docs.agentmail.to/api-reference/inboxes/messages/send 。
   万一某个老部署回 400/422 抱怨这俩字段，自动去掉 cc/bcc 重发一次、
   把 CC/BCC 写进正文开头，并在返回体里标 `cc_fallback=True` 让上层如实说明。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from ..tools.search import (
    DEFAULT_LIMIT,
    filter_messages,
    normalize_limit,
)

logger = logging.getLogger("astrbot_plugin_agentmail")
# 服务端搜索端点"不可用"的状态码：这些情况退化成本地过滤，其它错误（401 等）照常抛
SEARCH_FALLBACK_STATUS = (400, 404, 405, 422, 501)
SEARCH_FALLBACK_SCAN = 50

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


# 服务端「不认识 cc/bcc 字段」的错误长什么样：校验错误里点到 'cc'/'bcc'，
# 或者明说 unknown field / additionalProperties / not permitted。
_CC_FIELD_RE = re.compile(r"['\"]b?cc['\"]", re.IGNORECASE)
_CC_WORD_RE = re.compile(
    r"unknown field|additional propert|unexpected|not permitted|not allowed|unsupported|unknown parameter",
    re.IGNORECASE,
)


def _cc_unsupported(error: "MailError") -> bool:
    """判断这次 400/422 是不是「因为带了 cc/bcc」被拒的（只认字段级抱怨，不误伤别的 400）。"""
    if getattr(error, "status", 0) not in (400, 422):
        return False
    detail = str(error)
    return bool(_CC_WORD_RE.search(detail)) or bool(_CC_FIELD_RE.search(detail))


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
    def send(
        self,
        to,
        subject: str,
        text: str = "",
        html: str = "",
        cc=None,
        bcc=None,
        *,
        cc_fallback: bool = True,
    ) -> dict:
        """发信。

        `cc` / `bcc` 是**真的 CC/BCC**：AgentMail 的 SendMessageRequest 里就有
        `cc` / `bcc` 两个字段（Addresses = string | list of string），
        见 https://docs.agentmail.to/api-reference/inboxes/messages/send
        所以这里直接放进 payload，收件人之间互相看不见 BCC 是邮件协议本身保证的。

        `cc_fallback=True`（默认）时留一条后路：万一对端回 400/422 且明确抱怨
        `cc` / `bcc` 字段（比如老部署还没上线这两个字段），就**去掉 cc/bcc 重发一次**，
        并把 CC/BCC 写进正文开头，返回体里带 `cc_fallback=True` 让上层如实告诉用户
        「API 不支持真 CC，已写在正文」——不许假装支持。
        """
        recipients = _as_list(to)
        if not recipients:
            raise MailError("收件人为空")
        if not subject:
            raise MailError("主题为空")
        cc_list = _as_list(cc)
        bcc_list = _as_list(bcc)
        payload = {"to": recipients, "subject": subject}
        if cc_list:
            payload["cc"] = cc_list
        if bcc_list:
            payload["bcc"] = bcc_list
        if text:
            payload["text"] = text
        if html:
            payload["html"] = html
        if not text and not html:
            payload["text"] = "（无正文）"
        path = f"/inboxes/{self.inbox}/messages/send"
        try:
            return self.request(path, "POST", payload)
        except MailError as e:
            if not (cc_list or bcc_list) or not cc_fallback or not _cc_unsupported(e):
                raise
            logger.warning(f"[agentmail] 服务端不认 cc/bcc 字段（{e}），改为把 CC/BCC 写进正文重发")
            payload.pop("cc", None)
            payload.pop("bcc", None)
            header = []
            if cc_list:
                header.append("CC: " + ", ".join(cc_list))
            if bcc_list:
                header.append("BCC: " + ", ".join(bcc_list))
            header.append("（AgentMail 接口不支持 CC/BCC 字段，改写在正文里）")
            payload["text"] = "\n".join(header + ["", payload.get("text") or "（无正文）"])
            data = self.request(path, "POST", payload)
            return {**data, "cc_fallback": True, "cc": cc_list, "bcc": bcc_list}

    def list(self, limit: int = 5) -> list[dict]:
        data = self.request(f"/inboxes/{self.inbox}/messages?limit={max(1, min(50, int(limit)))}")
        return data.get("messages") or data.get("data") or []

    def search(self, keyword: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """关键词搜历史邮件：先服务端全文搜索，端点不可用时退化为本地过滤。

        - 关键词为空 → 直接 `[]`（不打 API）
        - `limit` 非法/超范围由 search.normalize_limit 夹成 [1, 20]，默认 5
        - 服务端按相关度排序返回；本地兜底则按收件箱原顺序（最新在前）
        """
        kw = (keyword or "").strip()
        n = normalize_limit(limit)
        if not kw:
            return []
        path = f"/inboxes/{self.inbox}/messages/search?q={urllib.parse.quote(kw)}&limit={n}"
        try:
            data = self.request(path)
        except MailError as e:
            if e.status not in SEARCH_FALLBACK_STATUS:
                raise
            logger.warning(
                f"[agentmail] 服务端搜索不可用（HTTP {e.status}），退化为 list({SEARCH_FALLBACK_SCAN}) 本地过滤"
            )
            return self._search_locally(kw, n)
        messages = data.get("messages") or data.get("data") or []
        return [m for m in messages if isinstance(m, dict)][:n]

    def _search_locally(self, keyword: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """兜底：拉最近 50 封，本地做大小写不敏感的子串过滤（主题/发件人/正文预览）。"""
        return filter_messages(self.list(SEARCH_FALLBACK_SCAN), keyword, limit)

    def read(self, message_id: str) -> dict:
        return self.request(f"/inboxes/{self.inbox}/messages/{message_id}")

    def attachment_info(self, message_id: str, attachment_id: str) -> dict:
        """取附件的下载信息（v0.5.0）。

        `GET /v0/inboxes/{inbox}/messages/{message_id}/attachments/{attachment_id}`
        → `{download_url, text_url?, expires_at, ...}`。

        ⚠️ `download_url` 是带签名的临时地址，**会过期、不许缓存**，
        每次要发附件都重新问一次接口。
        """
        mid = str(message_id or "").strip()
        aid = str(attachment_id or "").strip()
        if not mid or not aid:
            raise MailError("附件下载信息缺少 message_id 或 attachment_id")
        return self.request(
            f"/inboxes/{self.inbox}/messages/{urllib.parse.quote(mid, safe='')}"
            f"/attachments/{urllib.parse.quote(aid, safe='')}"
        )

    def whoami(self) -> dict:
        return self.request(f"/inboxes/{self.inbox}")
