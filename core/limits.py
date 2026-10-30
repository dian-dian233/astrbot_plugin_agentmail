"""发送限流（v0.6.0）：纯函数，方便单测，不碰数据库也不碰 astrbot。

两道上限（都在 _conf_schema.json 里）：

    send_cooldown     同一用户两次发送之间的最小间隔，单位秒，默认 30；0 = 关掉
    send_daily_limit  同一用户每天最多成功发几封，默认 50；0 = 不限制

关键约定：**只统计「真正发出去」的成功记录**（`ok=1`）。
被冷却/额度挡下来的请求也会记审计（`ok=0`），但不算进冷却和额度，
否则用户越急躁越自锁，一个人能把当天额度「刷」没。

超出上限时要说清「还要等多久 / 还剩几条」，不能只回一句「太频繁」。
"""

from __future__ import annotations

DEFAULT_COOLDOWN = 30
DEFAULT_DAILY_LIMIT = 50

REASON_OK = "ok"
REASON_COOLDOWN = "cooldown"
REASON_DAILY = "daily_limit"

WEBUI = "WebUI → 插件配置"


def clamp_int(value, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    """配置里的数字：容忍 "30" / 30 / True，写错了回默认值，负数夹到 minimum。"""
    if isinstance(value, bool):
        num = int(value)
    elif isinstance(value, str):
        try:
            num = int(float(value.strip()))
        except (TypeError, ValueError):
            return default
    else:
        try:
            num = int(value)
        except (TypeError, ValueError):
            return default
    if num < minimum:
        num = minimum
    if maximum is not None and num > maximum:
        num = maximum
    return num


def read_cooldown(value) -> int:
    """`send_cooldown` → 秒；未配置回默认 30，写错也回 30，负数按 0（关掉）。"""
    return clamp_int(value, DEFAULT_COOLDOWN, 0)


def read_daily_limit(value) -> int:
    """`send_daily_limit` → 条数；未配置回默认 50，写错也回 50，负数按 0（不限）。"""
    return clamp_int(value, DEFAULT_DAILY_LIMIT, 0)


def check_send_limit(
    *,
    now: float,
    last_sent: float | None = None,
    used_today: int = 0,
    cooldown: int = DEFAULT_COOLDOWN,
    daily_limit: int = DEFAULT_DAILY_LIMIT,
) -> dict:
    """判定这次能不能发。`now` / `last_sent` 都是 epoch 秒。

    返回：allowed / reason / cooldown / daily_limit / used / remaining /
          retry_after（还要等几秒，只在 cooldown 时有意义）/ elapsed（距上次成功发送几秒）
    `remaining` 为 -1 表示「没设上限」。
    """
    cooldown = clamp_int(cooldown, DEFAULT_COOLDOWN, 0)
    daily_limit = clamp_int(daily_limit, DEFAULT_DAILY_LIMIT, 0)
    used = max(0, clamp_int(used_today, 0, 0))
    remaining = -1 if daily_limit <= 0 else max(0, daily_limit - used)
    base = {
        "allowed": True,
        "reason": REASON_OK,
        "cooldown": cooldown,
        "daily_limit": daily_limit,
        "used": used,
        "remaining": remaining,
        "retry_after": 0,
        "elapsed": None,
    }

    # 先判每日额度：额度满了就算冷却已经过了也没意义，别让用户傻等那 20 秒
    if daily_limit > 0 and used >= daily_limit:
        return {**base, "allowed": False, "reason": REASON_DAILY}

    if cooldown > 0 and last_sent is not None:
        elapsed = max(0.0, float(now) - float(last_sent))
        if elapsed < cooldown:
            wait = int(round(cooldown - elapsed)) or 1  # 至少报 1 秒，别回「还要等 0 秒」
            return {**base, "allowed": False, "reason": REASON_COOLDOWN, "retry_after": wait,
                    "elapsed": int(elapsed)}

    if last_sent is not None:
        base["elapsed"] = max(0, int(float(now) - float(last_sent)))
    return base


def format_limit_message(decision: dict) -> str:
    """把拦截结果渲染成能直接回给用户的话：原因 + 剩余时间/剩余条数 + 去哪改。"""
    d = decision or {}
    reason = d.get("reason")
    if reason == REASON_DAILY:
        limit = d.get("daily_limit", DEFAULT_DAILY_LIMIT)
        used = d.get("used", limit)
        return (
            f"今天已经发满 {limit} 封了，现在发不了。\n"
            f"原因：send_daily_limit = {limit}（每个用户每天最多成功发 {limit} 封，按发送人统计）。\n"
            f"剩余：0 封（今天已用 {used}/{limit}），额度在明天 0 点（本地时间）重置。\n"
            f"解决：明天再发；或让管理员在 {WEBUI} → send_daily_limit 里调大"
            "（填 0 = 关掉每日上限）。"
        )
    if reason == REASON_COOLDOWN:
        wait = d.get("retry_after", 0)
        cooldown = d.get("cooldown", DEFAULT_COOLDOWN)
        elapsed = d.get("elapsed")
        since = f"上一次是 {elapsed} 秒前发的" if elapsed is not None else "刚发过一封"
        return (
            f"发得太快了，还要等 {wait} 秒。\n"
            f"原因：send_cooldown = {cooldown} 秒（同一个人两次发送之间至少隔 {cooldown} 秒），{since}。\n"
            f"剩余：{wait} 秒后可再发。\n"
            f"解决：等 {wait} 秒再发；或让管理员在 {WEBUI} → send_cooldown 里调小"
            "（填 0 = 关掉冷却）。"
        )
    return ""


def after_success(decision: dict) -> dict:
    """成功发出去一封之后，把额度口径顺延一条（`format_quota_note` 要用发完之后的数）。"""
    d = dict(decision or {})
    used = clamp_int(d.get("used", 0), 0, 0) + 1
    limit = clamp_int(d.get("daily_limit", 0), 0, 0)
    d["used"] = used
    d["remaining"] = -1 if limit <= 0 else max(0, limit - used)
    return d


def format_quota_note(decision: dict) -> str:
    """成功发送后附一句额度提示（没设每日上限 / 额度还多时不啰嗦）。"""
    d = decision or {}
    limit = clamp_int(d.get("daily_limit", 0), 0, 0)
    remaining = d.get("remaining", -1)
    if limit <= 0 or remaining < 0:
        return ""
    used = d.get("used", 0)
    return f"今日额度：剩 {remaining}/{limit} 封（今天已发 {used} 封）"
