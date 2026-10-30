"""`/mail send` 参数解析（v0.6.0）：支持 `--cc` / `--bcc`。

语法（收件人仍在最前面，主题在标志之后、`|` 之前）：

    /mail send <收件人> [--cc a@b,c@d] [--bcc e@f] <主题> | <正文>

例子：

    /mail send 1150880493@qq.com --cc boss@corp.com 明天见 | 我去不了，饭钱转你了
    /mail send a@x.com,b@y.com --cc c@x.com d@x.com --bcc e@x.com 周报 | 正文…

解析规则：
  · `--cc` / `--bcc`（也认 `-cc` / `-bcc` / `--抄送` / `--密送`，大小写不敏感）后面跟**看起来像邮箱**的
    token（含 `@`）才算地址，支持逗号分隔一次写多个，也支持连着写多个 token；
    一旦某个 token 不像邮箱（比如主题里的中文），立刻停手，把它留给主题 —— 这样
    `--cc a@b.com 明天见` 里的「明天见」不会被当成抄送地址。
  · 正文分隔符沿用 v0.1.0 的约定：第一个 `|`，没有 `|` 就用第一个换行，都没有则正文为空。

返回 `SendArgs(to, cc, bcc, subject, body)`，三个地址字段都是 list[str]。
"""

from __future__ import annotations

from collections import namedtuple

SendArgs = namedtuple("SendArgs", "to cc bcc subject body")

CC_FLAGS = {"--cc", "-cc", "--抄送"}
BCC_FLAGS = {"--bcc", "-bcc", "--密送"}

USAGE = (
    "用法：/mail send 收件人 [--cc 抄送] [--bcc 密送] 主题 | 正文\n"
    "例：/mail send a@x.com --cc b@x.com 明天见 | 我去不了"
)


class SendArgsError(ValueError):
    """参数写错了（缺收件人 / `--cc` 后面没跟邮箱）。文案直接回给用户。"""


def _addr_list(value) -> list[str]:
    """把 `a@x,b@y` / `['a@x']` / `a@x；b@y` 拆成地址列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        parts = [str(v) for v in value]
    else:
        parts = str(value).replace("，", ",").replace("；", ",").replace(";", ",").split(",")
    out = []
    for part in parts:
        addr = str(part).strip().strip("<>[]").strip()
        if addr:
            out.append(addr)
    return out


def _looks_like_addr(token: str) -> bool:
    """只有「含 @ 且不含空白」的 token 才会被当成地址，避免把主题词吃进 CC。"""
    return "@" in token and not any(ch.isspace() for ch in token)


def split_body(rest: str) -> tuple[str, str]:
    """`头部 | 正文` → 拆开；没有 `|` 用第一个换行；都没有则正文为空。"""
    text = rest or ""
    if "|" in text:
        head, body = text.split("|", 1)
    elif "\n" in text:
        head, body = text.split("\n", 1)
    else:
        head, body = text, ""
    return head.strip(), body.strip()


def parse_send_args(rest: str) -> SendArgs:
    """解析 `/mail send` 后面的整串参数（指令前缀已剥掉）。"""
    head, body = split_body(rest)
    tokens = head.split()
    if not tokens:
        raise SendArgsError(USAGE)
    if tokens[0].lower() in CC_FLAGS or tokens[0].lower() in BCC_FLAGS:
        raise SendArgsError("收件人写在最前面。\n" + USAGE)

    to = _addr_list(tokens[0])
    cc: list[str] = []
    bcc: list[str] = []
    subject_parts: list[str] = []
    i = 1
    while i < len(tokens):
        low = tokens[i].lower()
        if low in CC_FLAGS or low in BCC_FLAGS:
            flag = tokens[i]
            bucket = cc if low in CC_FLAGS else bcc
            i += 1
            got = 0
            while i < len(tokens) and _looks_like_addr(tokens[i]):
                addrs = _addr_list(tokens[i])
                bucket.extend(addrs)
                got += len(addrs)
                i += 1
            if not got:
                raise SendArgsError(f"{flag} 后面要跟邮箱地址，例如 {flag} someone@qq.com。\n" + USAGE)
            continue
        subject_parts.append(tokens[i])
        i += 1

    subject = " ".join(subject_parts).strip() or "(无主题)"
    return SendArgs(to=to, cc=cc, bcc=bcc, subject=subject, body=body)
