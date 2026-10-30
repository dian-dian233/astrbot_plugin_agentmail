"""权限判定（v0.6.0）：把「能不能发」从 bool 变成「带原因 + 怎么修」的结构。

v0.5.x 的 `_allowed(event)` 只返回 bool，拒绝时固定一句
「这个会话没开邮件权限，找管理员把你的 QQ 或群号加进白名单。」——
用户被哪条规则挡住、该找谁加什么，一概不知道。v0.6.0 把判定抽成纯函数
（不依赖 astrbot，可以单测），拒绝时说清「原因 + 解决」：

    evaluate(conf, sender=..., group=..., umo=..., is_admin=...) -> Decision

Decision 字段：
    allowed  能不能用
    rule     放行时命中的规则（allow_all / admin / allowed_users / allowed_groups）
    reason   拒绝时的原因码，四种：
               not_whitelisted_dm     私聊：allow_all 关，且用户不在 allowed_users
               not_whitelisted_group  群聊：allow_all 关，用户不在 allowed_users，群也不在 allowed_groups
               not_whitelisted_no_id  拿不到用户 ID / 群号，没法跟白名单比
               allow_all_invalid      allow_all 的值不是合法布尔（如 "off"），按关闭处理
    message  直接能贴给用户的整段话（「原因：…」+「解决：…」）
    detail   一行给日志用的定位信息（谁 / 哪个会话 / 被哪条规则挡）
"""

from __future__ import annotations

from collections import namedtuple

# WebUI 里改配置的路径，拒绝文案里直接写出来，用户能自己修
WEBUI = "WebUI → 插件配置"

RULE_ALLOW_ALL = "allow_all"
RULE_ADMIN = "admin"
RULE_USERS = "allowed_users"
RULE_GROUPS = "allowed_groups"

REASON_OK = "ok"
REASON_DM = "not_whitelisted_dm"
REASON_GROUP = "not_whitelisted_group"
REASON_NO_ID = "not_whitelisted_no_id"
REASON_BAD_BOOL = "allow_all_invalid"

DENY_HEAD = "这个会话没有邮件权限，发不了。"

_BOOL_YES = {"1", "true", "yes", "y", "on", "是", "开", "启用", "打开"}
_BOOL_NO = {"0", "false", "no", "n", "off", "否", "关", "关闭", "禁用", ""}

Decision = namedtuple("Decision", "allowed rule reason message detail")


def _ids(value) -> list[str]:
    """配置里的 list / 逗号串 / 单值，统一成去空的字符串列表。"""
    if not value:
        return []
    if isinstance(value, str):
        parts = value.replace("，", ",").replace("；", ",").replace(";", ",").split(",")
        return [p.strip() for p in parts if p.strip()]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()]


def parse_bool(value) -> tuple[bool, bool]:
    """宽松布尔解析，返回 `(值, 类型是否规范)`。

    `类型是否规范` 为 True 只有两种情况：真的是 Python `bool`（WebUI 正常写入的形态），
    或者没配（None＝按关闭）。

    这里刻意把「配置里是字符串 / 数字」当成类型不规范：
    WebUI 的 bool 配置项存的是真正的 true/false，手改 config json 才会出现
    `"false"` / `"off"` / `0` 这类写法，而 v0.5.x 的 `if self.conf.get("allow_all"):`
    会把非空字符串一律当「开」—— 明明是关的配置，结果所有人都有权限（或者反过来让人
    完全查不出被什么挡住）。所以解释上按「宽松值」走，但会在拒绝文案里点明类型不对。
    """
    if isinstance(value, bool):
        return value, True
    if value is None:  # 没填 = 关闭，不算写错
        return False, True
    if isinstance(value, (int, float)):
        return bool(value), False
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _BOOL_YES:
            return True, False
        if low in _BOOL_NO:
            return False, False
        return False, False
    return False, False


def _allow(rule: str, detail: str) -> Decision:
    return Decision(True, rule, REASON_OK, "", detail)


def evaluate(conf, *, sender: str = "", group: str = "", umo: str = "", is_admin: bool = False) -> Decision:
    """判定这个会话能不能用邮件，放行给规则名，拒绝给原因 + 解决办法。"""
    conf = conf or {}
    allow_all, bool_ok = parse_bool(conf.get("allow_all"))
    users = _ids(conf.get("allowed_users"))
    groups = _ids(conf.get("allowed_groups"))
    sender = str(sender or "").strip()
    group = str(group or "").strip()
    where = f"user={sender or '?'} session={umo or '?'} group={group or '-'}"

    if allow_all:
        return _allow(RULE_ALLOW_ALL, f"{where} rule=allow_all reason={REASON_OK}")
    if is_admin:
        return _allow(RULE_ADMIN, f"{where} rule=admin reason={REASON_OK}")
    if sender and sender in users:
        return _allow(RULE_USERS, f"{where} rule=allowed_users reason={REASON_OK}")
    if group and group in groups:
        return _allow(RULE_GROUPS, f"{where} rule=allowed_groups reason={REASON_OK}")

    # ── 下面是拒绝：按「哪条规则挡的」分原因 ─────────────────
    if not bool_ok:
        reason = REASON_BAD_BOOL
        raw = conf.get("allow_all")
        lines = [
            DENY_HEAD,
            f"原因：allow_all 的配置值是 {raw!r}，不是真正的布尔值"
            "（WebUI 里正常存的是 true / false），已按「关闭」处理。",
        ]
        fix = [f"让管理员在 {WEBUI} → allow_all 里选 true 或 false（别手写文字/数字）"]
        if sender:
            fix.append(f"或者把 {sender} 加进 allowed_users")
        if group:
            fix.append(f"或把群号 {group} 填进 allowed_groups")
        lines.append("解决：" + "；".join(fix) + "。")
        return Decision(False, "", reason, "\n".join(lines), f"{where} reason={reason} allow_all={raw!r}")

    if not sender and not group:
        reason = REASON_NO_ID
        lines = [
            DENY_HEAD,
            "原因：allow_all 未开启，而这个会话既拿不到用户 ID、也拿不到群号"
            f"（会话标识：{umo or '空'}），没法跟白名单比对。",
            f"解决：让管理员把当前群号填进 {WEBUI} → allowed_groups，"
            "或把你的 QQ 号填进 allowed_users。",
        ]
        return Decision(False, "", reason, "\n".join(lines), f"{where} reason={reason}")

    if group:
        reason = REASON_GROUP
        lines = [
            DENY_HEAD,
            f"原因：allow_all 未开启，且你的 QQ 号 {sender or '（拿不到）'} 不在 allowed_users 里，"
            f"当前群 {group} 也不在 allowed_groups 里。",
            f"解决：让管理员在 {WEBUI} → allowed_users 里加上 {sender or '你的号'}"
            f"（只放开你），或把群号 {group} 填进 allowed_groups（整群可用）。",
        ]
        return Decision(False, "", reason, "\n".join(lines), f"{where} reason={reason}")

    reason = REASON_DM
    lines = [
        DENY_HEAD,
        f"原因：allow_all 未开启，且你的 QQ 号 {sender} 不在 allowed_users 里"
        "（当前是私聊，allowed_groups 只对群聊生效，帮不上忙）。",
        f"解决：让管理员在 {WEBUI} → allowed_users 里加上 {sender}（一行一个）。",
    ]
    return Decision(False, "", reason, "\n".join(lines), f"{where} reason={reason}")
