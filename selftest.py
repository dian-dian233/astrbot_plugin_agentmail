"""不装 AstrBot 也能测：用假的 astrbot.api 模块桩，把插件真正跑一遍（v0.4.0）。

覆盖：帮助 / 缺参 / 白名单拦截 / 真发信 / 查收件箱 / 会话ID / 收信轮询（基线+推送）
      + v0.3.0 推送模板：默认模板与 v0.2.0 逐字一致 / 自定义模板渲染 / 非法模板回退不抛异常
      + v0.4.0 附件随推送：清单格式 / 有附件发真文件 / 无附件行为不变 /
        下载失败降级 / 适配器不支持文件降级 / 临时目录清理
      + v0.4.0 /mail search：参数解析 / 时间倒序 / 空关键词用法 / 无结果文案 /
        接口不支持降级本地过滤
      + v0.4.0 每日未读摘要：时间解析回退 / 到点推一次 / 同一天不重复 /
        重启不重复 / 跨天再推 / 会话回落 / Mailer 复用

用法：
    PYTHONDONTWRITEBYTECODE=1 python3 selftest.py            # 含真实 API 抽查（没网/没 key 会自动跳过）
    PYTHONDONTWRITEBYTECODE=1 python3 selftest.py --offline  # 只跑本地断言
"""
import asyncio
import glob
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OFFLINE = "--offline" in sys.argv
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # 兼容放在 plugins/ 下、以包形式导入

WARNINGS = []


def _deco(*a, **k):
    def wrap(f):
        return f
    return wrap


def _warn(*a, **k):
    msg = " ".join(str(x) for x in a)
    WARNINGS.append(msg)
    print("[warn]", msg)


_log = types.SimpleNamespace(
    info=lambda *a, **k: None,
    warning=_warn,
    error=lambda *a, **k: print("[err]", *a),
)


class _Star:
    """模拟 Star：带上 KV 存储。"""

    def __init__(self, context=None, config=None):
        self.context = context
        self.logger = _log
        self._kv = {}

    async def get_kv_data(self, key, default=None):
        return self._kv.get(key, default)

    async def put_kv_data(self, key, value):
        self._kv[key] = value


class _Plain:
    def __init__(self, text=""):
        self.text = text


class _MessageChain:
    def __init__(self, components=None):
        self.components = components or []


astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = _log
class _File:
    """对应 astrbot.api.message_components.File(name, file, url)。"""

    def __init__(self, name="", file="", url=""):
        self.name = name
        self.file_ = file
        self.url = url

    def __repr__(self):
        return f"File(name={self.name!r}, file={self.file_!r})"


comp_mod = types.ModuleType("astrbot.api.message_components")
comp_mod.Plain = _Plain
comp_mod.File = _File
event_mod = types.ModuleType("astrbot.api.event")
event_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})
event_mod.MessageChain = _MessageChain
event_mod.filter = types.SimpleNamespace(command=_deco, llm_tool=_deco)
star_mod = types.ModuleType("astrbot.api.star")
star_mod.Star = _Star
star_mod.Context = object
sys.modules.update({
    "astrbot": astrbot,
    "astrbot.api": api,
    "astrbot.api.message_components": comp_mod,
    "astrbot.api.event": event_mod,
    "astrbot.api.star": star_mod,
})

try:
    from astrbot_plugin_agentmail.attach import (  # noqa: E402
        attachment_block,
        attachment_lines,
        download_attachments,
        human_size,
        safe_filename,
    )
    from astrbot_plugin_agentmail.digest import (  # noqa: E402
        format_digest,
        is_due as digest_is_due,
        parse_time as parse_digest_time,
    )
    from astrbot_plugin_agentmail.mailer import MailError  # noqa: E402
    from astrbot_plugin_agentmail.main import AgentMailPlugin  # noqa: E402
    from astrbot_plugin_agentmail.notify import render_notice  # noqa: E402
    from astrbot_plugin_agentmail.search import (  # noqa: E402
        USAGE as SEARCH_USAGE,
        format_results as format_search_results,
        local_search,
        parse_args as parse_search_args,
        sort_desc,
    )
except ImportError:  # 仓库根目录直接跑
    from attach import (  # noqa: E402
        attachment_block,
        attachment_lines,
        download_attachments,
        human_size,
        safe_filename,
    )
    from digest import (  # noqa: E402
        format_digest,
        is_due as digest_is_due,
        parse_time as parse_digest_time,
    )
    from mailer import MailError  # noqa: E402
    from main import AgentMailPlugin  # noqa: E402
    from notify import render_notice  # noqa: E402
    from search import (  # noqa: E402
        USAGE as SEARCH_USAGE,
        format_results as format_search_results,
        local_search,
        parse_args as parse_search_args,
        sort_desc,
    )


class FakeEvent:
    def __init__(self, text, admin=True, sender="1150880493", group="", umo="aiocqhttp:FriendMessage:1150880493"):
        self.message_str = text
        self._admin, self._sender, self._group = admin, sender, group
        self.unified_msg_origin = umo

    def plain_result(self, text):
        return text

    def is_admin(self):
        return self._admin

    def get_sender_id(self):
        return self._sender

    def get_group_id(self):
        return self._group


class FakeContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, session, chain):
        self.sent.append((session, chain))
        return True


class FakeMailer:
    """假的邮件客户端：messages 可随时替换。"""

    def __init__(self, messages):
        self.messages = messages

    def list(self, limit=5, labels=None):
        return self.messages[:limit]


class FakeAttachMailer(FakeMailer):
    """带附件接口的假 mailer：可模拟下载成功 / 失败。"""

    BLOB = b"hello attachment"

    def __init__(self, messages, fail_download=False, missing_url=False):
        super().__init__(messages)
        self.fail_download = fail_download
        self.missing_url = missing_url
        self.calls = []

    def attachment_info(self, message_id, attachment_id):
        self.calls.append(("info", message_id, attachment_id))
        info = {
            "attachment_id": attachment_id,
            "filename": "report.txt",
            "size": len(self.BLOB),
            "content_type": "text/plain",
            "content_disposition": "attachment",
        }
        if not self.missing_url:
            info["download_url"] = "https://cdn.agentmail.to/attachments/x?Signature=abc"
        return info

    def download(self, url, dest_path, limit=None):
        self.calls.append(("download", url))
        if self.fail_download:
            raise MailError("直连下载失败（HTTP 403）")
        with open(dest_path, "wb") as f:
            f.write(self.BLOB)
        return len(self.BLOB)


class FakeSearchMailer(FakeMailer):
    """假搜索：故意按“相关度”乱序返回，时间倒序得靠插件自己排。"""

    def __init__(self, messages, unsupported=False, hits=None):
        super().__init__(messages)
        self.unsupported = unsupported
        self.hits = hits
        self.searched = []

    def search(self, query, limit=20):
        if self.unsupported:
            raise MailError("HTTP 400：不支持 q 参数", 400)
        self.searched.append((query, limit))
        data = self.messages if self.hits is None else self.hits
        order = [2, 0, 1] if len(data) >= 3 else list(range(len(data)))
        return [data[i] for i in order][:limit]


class FakeUnreadMailer(FakeMailer):
    """假 mailer：支持按 labels 过滤（模拟接口的 labels=unread），并记录调用。"""

    def __init__(self, messages):
        super().__init__(messages)
        self.list_calls = []

    def list(self, limit=5, labels=None):
        self.list_calls.append((limit, tuple(labels or ())))
        msgs = list(self.messages)
        if labels:
            want = {str(x).lower() for x in labels}
            msgs = [
                m
                for m in msgs
                if want <= {str(x).lower() for x in (m.get("labels") or [])}
            ]
        return msgs[:limit]


class RecordingContext(FakeContext):
    """记录推送，并在发送那一刻探一下附件文件到底存不存在。"""

    def __init__(self):
        super().__init__()
        self.exists_at_send = []

    async def send_message(self, session, chain):
        for c in getattr(chain, "components", []):
            if isinstance(c, _File):
                self.exists_at_send.append(os.path.exists(c.file_))
        return await super().send_message(session, chain)


class PickyContext(RecordingContext):
    """模拟适配器不支持文件消息：带 File 的链会抛异常。"""

    async def send_message(self, session, chain):
        if any(isinstance(c, _File) for c in getattr(chain, "components", [])):
            raise RuntimeError("当前平台适配器不支持文件消息")
        return await super().send_message(session, chain)


def read_key():
    try:
        return open(os.path.expanduser("~/.secrets/agentmail.key")).read().strip()
    except OSError:
        return ""


BASE_CONF = {
    "inbox": "diandian233@agentmail.to",
    "proxy": "http://192.168.2.15:7890",
    "timeout": 30,
    "allow_all": False,
    "allowed_users": ["1150880493"],
    "allowed_groups": [],
    "poll_enabled": True,
    "poll_interval": 300,
    "notify_sessions": ["aiocqhttp:FriendMessage:1150880493"],
    "notify_preview_len": 300,
    "notify_on_first_poll": False,
}


def conf(**over):
    c = dict(BASE_CONF)
    c.update(over)
    return c


async def run(handler, evt, label):
    print(f"\n── {label} ──")
    print(">", evt.message_str)
    async for r in handler(evt):
        print("←", r)


def mail(
    mid,
    subject,
    labels=("received", "unread"),
    frm="someone@qq.com",
    preview=None,
    attachments=None,
):
    m = {
        "message_id": mid,
        "subject": subject,
        "from": frm,
        "timestamp": "2026-10-27T10:00:00.000Z",
        "labels": list(labels),
        "preview": "这是一封测试邮件的正文预览，用来验证推送文案。" if preview is None else preview,
    }
    if attachments is not None:
        m["attachments"] = attachments
    return m


def att(filename="report.txt", size=29, content_type="text/plain", attachment_id="att-1"):
    return {
        "attachment_id": attachment_id,
        "filename": filename,
        "size": size,
        "content_type": content_type,
        "content_disposition": "attachment",
    }


def legacy_v020_notice(m, preview_len=300):
    """v0.2.0 的 _compose_notice 原文（逐字复制），用来当基线对照。"""
    try:
        limit = int(preview_len or 300)
    except (TypeError, ValueError):
        limit = 300
    frm = m.get("from") or "?"
    body = m.get("preview") or m.get("text") or "(正文见邮件)"
    body = str(body).replace("\n\n", "\n").strip()
    if len(body) > limit:
        body = body[:limit] + "…"
    return (
        "📬 新邮件\n"
        f"主题：{m.get('subject', '(无主题)')}\n"
        f"发件人：{frm}\n"
        f"时间：{str(m.get('timestamp', ''))[:19]}\n"
        "────────────\n"
        f"{body}"
    )


def test_notify_template():
    print("\n── 推送模板（v0.3.0 纯函数）──")
    m = mail("m9", "新来的")

    # ① 默认模板（空配置）必须和 v0.2.0 逐字一致
    cases = [
        mail("a", "新来的"),
        mail("b", None),  # 主题缺失
        {"message_id": "c", "text": "只有 text 没有 preview", "timestamp": "2026-10-27T10:00:00.000Z"},
        {"message_id": "d", "subject": "缺字段", "from": None, "preview": "长" * 40, "timestamp": ""},
        {"message_id": "e", "subject": "双空行", "from": "x@y.com", "preview": "第一段\n\n第二段", "timestamp": 1700000000},
    ]
    for case in cases:
        for limit in (300, 10, 0, "abc", None):
            got = render_notice(case, template="", preview_len=limit)
            exp = legacy_v020_notice(case, limit)
            assert got == exp, f"默认文案和 v0.2.0 不一致\n got={got!r}\n exp={exp!r}"
    print("① 默认模板（空 / 纯空白 / 未配置）与 v0.2.0 逐字一致 ✅（5 封邮件 × 5 组 preview_len）")
    print("   样例：", repr(render_notice(m, template="")))

    # ② 自定义模板 {subject}|{from}|{preview}
    got = render_notice(m, template="{subject}|{from}|{preview}", preview_len=300, inbox=BASE_CONF["inbox"])
    exp = "新来的|someone@qq.com|这是一封测试邮件的正文预览，用来验证推送文案。"
    assert got == exp, f"自定义模板渲染错\ngot={got!r}\nexp={exp!r}"
    print("② 自定义模板 {subject}|{from}|{preview} →", repr(got), "✅")

    # ②′ 五个占位符都要能用，{preview} 受 notify_preview_len 截断
    got = render_notice(
        m,
        template="{subject} / {from} / {time} / {inbox} / {preview}",
        preview_len=6,
        inbox=BASE_CONF["inbox"],
    )
    assert got == "新来的 / someone@qq.com / 2026-10-27T10:00:00 / diandian233@agentmail.to / 这是一封测试…", got
    print("   五个占位符 + preview_len=6 截断 →", repr(got), "✅")

    # ③ 非法模板：回退内置文案、不抛异常、记 warning
    bad = ["{foo}", "{subject", "{subject}|{", "{subject!r}", "{:>3}", "{}", "{subject[0]}", "{Subject}"]
    baseline = legacy_v020_notice(m)
    for tpl in bad:
        WARNINGS.clear()
        got = render_notice(m, template=tpl, preview_len=300, warn=_warn)  # 不能抛
        assert got == baseline, f"非法模板 {tpl!r} 应回退内置文案，实际 {got!r}"
        assert WARNINGS and "推送模板不可用" in WARNINGS[-1], f"非法模板 {tpl!r} 没记 warning"
        print(f"   非法模板 {tpl!r} → 回退内置文案 + warning ✅")

    # ③″ 不给 warn 回调时，走标准库 logging 也要留痕（插件里传的是 astrbot logger）
    import logging

    recs = []

    class _Cap(logging.Handler):
        def emit(self, record):
            recs.append(record.getMessage())

    lg = logging.getLogger("astrbot_plugin_agentmail")
    h = _Cap()
    lg.addHandler(h)
    try:
        assert render_notice(m, template="{foo}") == baseline
    finally:
        lg.removeHandler(h)
    assert recs and "推送模板不可用" in recs[-1], recs
    print("   默认 warn 通道（标准库 logging）也留了 warning ✅")

    # ③′ 非字符串配置也不炸
    for weird in (None, 123, ["{subject}"]):
        assert render_notice(m, template=weird, warn=_warn) == baseline, weird
    print("③ 非法模板（8 种）与非字符串配置（3 种）全部回退内置文案、零异常 ✅")

    # ④ 集成：main.py 的 _compose_notice 真的读了配置
    p = AgentMailPlugin(FakeContext(), conf(notify_template="{subject}|{from}|{preview}"))
    assert p._compose_notice(m) == exp, p._compose_notice(m)
    p2 = AgentMailPlugin(FakeContext(), conf())
    assert p2._compose_notice(m) == baseline
    WARNINGS.clear()
    p3 = AgentMailPlugin(FakeContext(), conf(notify_template="{oops}"))
    assert p3._compose_notice(m) == baseline, "非法模板在 _compose_notice 里没回退"
    assert WARNINGS and "推送模板不可用" in WARNINGS[-1], "插件日志里没 warning"
    print("④ _compose_notice 走配置：自定义模板 / 空配置 / 非法模板（回退+warning）三种都对 ✅")


def test_digest_helpers():
    """v0.4.0 功能③：时间解析 / 到点判断 / 摘要文案（纯函数）。"""
    print("\n── 每日未读摘要（v0.4.0 纯函数）──")

    # 时间解析
    assert parse_digest_time("09:00") == (9, 0)
    assert parse_digest_time("9:5") == (9, 5)
    assert parse_digest_time(" 21：30 ") == (21, 30), "全角冒号也要认"
    assert parse_digest_time("") == (9, 0) and parse_digest_time(None) == (9, 0)
    WARNINGS.clear()
    for bad in ("9点", "25:00", "9:70", "abc", "09:00:00", 123, "-1:00"):
        assert parse_digest_time(bad, warn=_warn) == (9, 0), f"{bad!r} 应该回退 09:00"
    assert len(WARNINGS) == 7 and all("digest_time" in w for w in WARNINGS), WARNINGS
    print("   ① 时间解析：09:00 / 9:5 / 全角 / 空 → 正常；7 种写错 → 回退 09:00 + warning，零异常 ✅")

    # 到点判断（含跨天 / 同一天不重复 / 本地时区语义）
    now = datetime(2026, 10, 27, 9, 0, 30)
    assert digest_is_due(now, 9, 0, "") is True
    assert digest_is_due(now, 9, 0, "2026-10-27") is False, "同一天不能推第二次"
    assert digest_is_due(datetime(2026, 10, 27, 8, 59), 9, 0, "") is False, "没到点不能推"
    assert digest_is_due(datetime(2026, 10, 28, 9, 0), 9, 0, "2026-10-27") is True, "跨天要能推"
    assert digest_is_due(datetime(2026, 10, 28, 0, 5), 9, 0, "2026-10-27") is False, "第二天凌晨没到点"
    # 本地时区语义：东八区 09:05 就是「到点」，不能因为 UTC 还在 01:05 就判成没到点
    cst = timezone(timedelta(hours=8))
    assert digest_is_due(datetime(2026, 10, 27, 9, 5, tzinfo=cst), 9, 0, "") is True
    print("   ② 到点判断：到点推 / 同一天不重复 / 跨天才再推 / 凌晨不误推 / 东八区 09:05 算到点 ✅")

    # 文案
    msgs = [
        s_mail("d-1", "最早的", "2026-10-01T00:00:00.000Z"),
        s_mail("d-2", "最新的", "2026-10-27T02:24:08.000Z"),
        s_mail("d-3", "中间的", "2026-10-15T01:00:00.000Z"),
        s_mail("d-4", "更早的", "2026-09-30T00:00:00.000Z"),
    ]
    text = format_digest(4, msgs, inbox="me@agentmail.to", now=datetime(2026, 10, 27, 9, 0), tz=timezone.utc)
    assert "📬 每日未读摘要 · 2026-10-27" in text, text
    assert "未读邮件：4 封" in text and "收件箱：me@agentmail.to" in text, text
    assert "最新 3 封" in text, "最多只列 3 封"
    assert "1. 10-27 02:24 · boss@corp.com · 最新的" in text, text
    tail = text.split("最新 3 封")[1]
    assert "中间的" in tail and "最早的" in tail and "更早的" not in tail, "只列最新 3 封，第 4 封不该出现"
    assert "0 封" in format_digest(0, [], now=datetime(2026, 10, 27, 9, 0)), "0 未读要有单独文案"
    assert "50+ 封" in format_digest(50, msgs, now=datetime(2026, 10, 27, 9, 0)), "到上限标 50+"
    print("   ③ 文案：日期 / 未读封数 / 最新 3 封（时间 · 发件人 · 主题）/ 0 封单独文案 ✅")


async def test_digest_schedule():
    """v0.4.0 功能③：调度行为（到点推一次 / 同一天不重复 / 重启不重复 / 格式错回退）。"""
    print("\n── 每日未读摘要调度（v0.4.0 集成）──")
    inbox_msgs = [
        s_mail("u-1", "未读甲", "2026-10-27T02:00:00.000Z"),
        s_mail("u-2", "未读乙", "2026-10-27T01:00:00.000Z"),
        s_mail("r-1", "已读的", "2026-10-27T00:30:00.000Z", labels=("received",)),
    ]

    # ④ 默认关闭：不会起调度
    p_off = AgentMailPlugin(FakeContext(), conf())
    p_off._start_digest()
    assert p_off._digest_task is None, "digest_enabled 默认关，不该起后台任务"

    # ⑤ 到点推一次
    ctx = RecordingContext()
    p = AgentMailPlugin(ctx, conf(digest_enabled=True, digest_time="09:00", digest_sessions=["sess-a"]))
    p._mailer = FakeUnreadMailer(inbox_msgs)
    st = await p._maybe_digest(datetime(2026, 10, 27, 9, 0, 5))
    assert st and st["pushed"] == 1 and st["unread"] == 2, st
    assert len(ctx.sent) == 1 and ctx.sent[0][0] == "sess-a", ctx.sent
    body = ctx.sent[0][1].components[0].text
    assert "每日未读摘要" in body and "未读邮件：2 封" in body and "未读甲" in body, body
    assert "已读的" not in body, "已读邮件不该进摘要"
    assert p._mailer.list_calls[-1] == (50, ("unread",)), p._mailer.list_calls
    assert await p.get_kv_data("digest_last_date", "") == "2026-10-27"
    print("   ④ 到点推一次：未读 2 封 + 最新 2 封进文案，只请求 labels=unread，日期已记 KV ✅")

    # ⑥ 同一天不重复
    assert await p._maybe_digest(datetime(2026, 10, 27, 9, 30)) is None
    assert await p._maybe_digest(datetime(2026, 10, 27, 18, 0)) is None
    assert len(ctx.sent) == 1, "同一天只能推一次"
    print("   ⑤ 同一天（9:30 / 18:00）再检查 → 一条都不多推 ✅")

    # ⑦ 进程重启后不重复推当天（KV 是持久化的，这里用同一份 KV 模拟重启）
    ctx_r = RecordingContext()
    p_restart = AgentMailPlugin(ctx_r, conf(digest_enabled=True, digest_time="09:00", digest_sessions=["sess-a"]))
    p_restart._mailer = FakeUnreadMailer(inbox_msgs)
    p_restart._kv = dict(p._kv)  # 继承「今天已经推过」的 KV
    assert await p_restart._maybe_digest(datetime(2026, 10, 27, 10, 0)) is None
    assert ctx_r.sent == [], "重启后不能重复推当天的摘要"
    print("   ⑥ 模拟进程重启（KV 继承）→ 当天不再重复推 ✅")

    # ⑧ 跨天到点再推
    assert await p._maybe_digest(datetime(2026, 10, 28, 9, 1)) is not None
    assert len(ctx.sent) == 2, "第二天到点要再推一次"
    assert await p.get_kv_data("digest_last_date", "") == "2026-10-28"
    print("   ⑦ 跨天（次日 09:01）→ 再推一次，日期改成 10-28 ✅")

    # ⑨ 时间格式写错 → 回退 09:00 + warning，照样能推，不抛异常
    WARNINGS.clear()
    ctx_bad = RecordingContext()
    p_bad = AgentMailPlugin(ctx_bad, conf(digest_enabled=True, digest_time="9点上班", digest_sessions=["sess-b"]))
    p_bad._mailer = FakeUnreadMailer(inbox_msgs)
    assert p_bad._digest_clock() == (9, 0)
    assert any("digest_time" in w for w in WARNINGS), WARNINGS
    assert await p_bad._maybe_digest(datetime(2026, 10, 27, 8, 59)) is None, "回退成 09:00，8:59 不该推"
    assert (await p_bad._maybe_digest(datetime(2026, 10, 27, 9, 0)))["pushed"] == 1
    print("   ⑧ digest_time 写错 → 回退 09:00 + warning，8:59 不推、9:00 推，零异常 ✅")

    # ⑩ digest_sessions 留空 → 用 notify_sessions；两者都空 → 不起任务、只 warning
    p_fb = AgentMailPlugin(FakeContext(), conf(digest_enabled=True, digest_sessions=[]))
    assert p_fb._digest_sessions() == ["aiocqhttp:FriendMessage:1150880493"], "留空要回落到 notify_sessions"
    WARNINGS.clear()
    p_none = AgentMailPlugin(FakeContext(), conf(digest_enabled=True, digest_sessions=[], notify_sessions=[]))
    p_none._start_digest()
    assert p_none._digest_task is None and any("digest_sessions" in w for w in WARNINGS), WARNINGS
    # 共用同一个 Mailer 实例（别开两条连接）
    p_shared = AgentMailPlugin(FakeContext(), conf(digest_enabled=True))
    fake = FakeUnreadMailer(inbox_msgs)
    p_shared._mailer = fake
    assert p_shared._mailer_or_raise() is fake
    await p_shared._collect_unread()
    assert p_shared._mailer is fake and fake.list_calls, "摘要必须复用收信轮询的 Mailer 实例"
    print("   ⑨ digest_sessions 留空 → 回落 notify_sessions；都空 → 只 warning 不起任务；Mailer 实例复用 ✅")

    # ⑪ 手动 /mail digest：能立刻推，但不写「今天已推过」的标记
    p_manual = AgentMailPlugin(FakeContext(), conf(digest_enabled=True, digest_sessions=["sess-m"]))
    p_manual._mailer = FakeUnreadMailer(inbox_msgs)
    out = await collect(p_manual.mail, FakeEvent("/mail digest"))
    assert any("摘要已推到 1 个会话" in r for r in out), out
    assert await p_manual.get_kv_data("digest_last_date", "") == "", "手动触发不写日期标记"
    print("   ⑩ /mail digest 手动触发：立刻推一次，且不占用当天的定时名额 ✅")


def s_mail(mid, subject, ts, frm="boss@corp.com", preview="正文预览", labels=("received", "unread")):
    m = mail(mid, subject, labels=labels, frm=frm, preview=preview)
    m["timestamp"] = ts
    return m


def test_search_helpers():
    """v0.4.0 功能②：搜索参数解析 / 排序 / 本地降级过滤（纯函数）。"""
    print("\n── 搜索（v0.4.0 纯函数）──")

    # 关键词 + 数量
    assert parse_search_args("发票 5") == ("发票", 5)
    assert parse_search_args("  年度 总结 3 ") == ("年度 总结", 3), parse_search_args("  年度 总结 3 ")
    assert parse_search_args("发票") == ("发票", 10)
    assert parse_search_args("") == ("", 10)
    assert parse_search_args("2026") == ("2026", 10), "单个纯数字要当关键词，不能瞎猜成数量"
    assert parse_search_args("发票 999") == ("发票", 100), "数量要夹到 1-100"
    assert parse_search_args("发票 0") == ("发票", 1)
    assert "用法" in SEARCH_USAGE
    print("   ① 参数解析：正常 / 带空格关键词 / 缺数量 / 空串 / 纯数字 / 越界夹取 ✅")

    # 时间倒序
    msgs = [
        s_mail("id-old", "早的", "2026-10-01T09:00:00.000Z"),
        s_mail("id-new", "最新的", "2026-10-27T10:00:00.000Z"),
        s_mail("id-mid", "中间的", "2026-10-15T12:00:00.000Z"),
        {"message_id": "id-broken", "subject": "没时间字段"},
    ]
    assert [m["message_id"] for m in sort_desc(msgs)] == ["id-new", "id-mid", "id-old", "id-broken"]
    # 本地降级过滤：主题 / 发件人 / 正文都能命中，大小写不敏感，且结果仍按时间倒序
    inv = [
        s_mail("a", "季度发票", "2026-10-02T00:00:00.000Z"),
        s_mail("b", "无关邮件", "2026-10-03T00:00:00.000Z"),
        s_mail("c", "转发：发票", "2026-10-04T00:00:00.000Z"),
        s_mail("d", "别人发的", "2026-10-05T00:00:00.000Z", frm="INVOICE@Corp.com", preview="啥也没有"),
    ]
    hits = local_search(inv, "发票", 10)
    assert [m["message_id"] for m in hits] == ["c", "a"], hits
    assert [m["message_id"] for m in local_search(inv, "invoice", 10)] == ["d"]
    assert len(local_search(inv, "发票", 1)) == 1, "数量要生效"
    assert local_search(inv, "", 10) == [] and local_search(inv, "根本没有", 10) == []
    print("   ② 时间倒序 + 本地子串过滤（主题/发件人/正文，大小写不敏感）✅")

    # 空结果文案
    empty = format_search_results("发票", [])
    assert "没搜到包含「发票」的邮件" in empty, empty
    one = format_search_results("发票", [s_mail("id-1", "季度发票", "2026-10-02T03:04:05.000Z")])
    assert "找到 1 封" in one and "2026-10-02 03:04:05" in one and "boss@corp.com" in one and "id-1" in one, one
    print("   ③ 输出格式：时间 · 发件人 · 主题 + message_id；空结果有明确文案 ✅")


async def collect(handler, evt):
    out = []
    async for r in handler(evt):
        out.append(r)
    return out


async def test_search_command():
    """v0.4.0 功能②：/mail search 指令（离线假 mailer）。"""
    print("\n── /mail search（v0.4.0 集成）──")
    msgs = [
        s_mail("id-1", "季度发票", "2026-10-02T10:00:00.000Z"),
        s_mail("id-2", "无关邮件", "2026-10-03T10:00:00.000Z"),
        s_mail("id-3", "转发：年度发票", "2026-10-27T10:00:00.000Z"),
    ]

    # ④ 正常搜索：时间倒序 + 数量生效 + id 打出来
    p = AgentMailPlugin(FakeContext(), conf())
    p._mailer = FakeSearchMailer(msgs)
    out = await collect(p.mail, FakeEvent("/mail search 发票 2"))
    text = "\n".join(out)
    assert p._mailer.searched == [("发票", 2)], p._mailer.searched
    assert "找到 2 封" in text, text
    assert text.index("id-3") < text.index("id-1"), f"必须先新后旧：{text}"
    assert "id-2" not in text, "无关邮件不该出现"
    assert "→ notice" not in text and "2026-10-27 10:00:00 · boss@corp.com · 转发：年度发票" in text, text
    print("   ④ 正常：搜到 2 封，按时间倒序（id-3 在 id-1 前），无关邮件被排除 ✅")

    # ⑤ 空关键词：给用法提示，不抛异常，也不打接口
    p2 = AgentMailPlugin(FakeContext(), conf())
    p2._mailer = FakeSearchMailer(msgs)
    out2 = await collect(p2.mail, FakeEvent("/mail search"))
    assert any("用法：/mail search" in r for r in out2), out2
    assert p2._mailer.searched == [], "空关键词不该请求接口"
    out2b = await collect(p2.mail, FakeEvent("/mail search    "))  # 只有空格
    assert any("用法" in r for r in out2b), out2b
    # 纯数字关键词不瞎猜成数量：`/mail search 2026` 是搜「2026」
    out2c = await collect(p2.mail, FakeEvent("/mail search 2026"))
    assert p2._mailer.searched[-1] == ("2026", 10), p2._mailer.searched
    print("   ⑤ 空关键词 → 用法提示（零异常、零请求）✅")

    # ⑥ 无结果：明确文案
    p3 = AgentMailPlugin(FakeContext(), conf())
    p3._mailer = FakeSearchMailer(msgs, hits=[])
    out3 = await collect(p3.mail, FakeEvent("/mail search 发票"))
    assert any("没搜到包含「发票」的邮件" in r for r in out3), out3
    print("   ⑥ 无结果 → 「没搜到包含「发票」的邮件」✅")

    # ⑦ 接口不支持（HTTP 400）→ 降级为本地过滤最近 N 封
    p4 = AgentMailPlugin(FakeContext(), conf())
    p4._mailer = FakeSearchMailer(msgs, unsupported=True)
    WARNINGS.clear()
    out4 = await collect(p4.mail, FakeEvent("/mail search 发票 5"))
    text4 = "\n".join(out4)
    assert "已降级为在最近 3 封里本地过滤" in text4, text4
    assert "id-3" in text4 and "id-1" in text4 and "id-2" not in text4, text4
    assert text4.index("id-3") < text4.index("id-1"), text4
    assert any("搜索接口不可用" in w for w in WARNINGS), WARNINGS
    print("   ⑦ 接口不支持 → 降级本地过滤（仍时间倒序）+ warning + 明确提示 ✅")


def test_attachment_helpers():
    """v0.4.0 功能①：附件纯函数（不碰网络）。"""
    print("\n── 附件（v0.4.0 纯函数）──")

    # 大小格式化
    assert human_size(29) == "29 B"
    assert human_size(1536) == "1.5 KB"
    assert human_size(3 * 1024 * 1024) == "3.0 MB"
    assert human_size(None) == "?" and human_size("abc") == "?"

    # 文件名清洗（防目录穿越 / 特殊字符）
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename('a:b*c?"d.txt') == "a_b_c__d.txt"
    assert safe_filename("") == "attachment.bin"

    # 清单：附件名 + 大小 + 类型
    lines = attachment_lines([att()])
    assert lines == ["· report.txt（29 B · text/plain）"], lines
    assert attachment_lines([]) == [] and attachment_lines(None) == [] and attachment_lines("x") == []
    block = attachment_block([att(), att("图.png", 2048, "image/png", "att-2")])
    assert "📎 附件 2 个" in block, block
    assert "· 图.png（2.0 KB · image/png）" in block, block

    # 无附件：文案必须与 v0.2.0 逐字一致（一个字符都不能多）
    plain = mail("x", "无附件")
    assert render_notice(plain) == legacy_v020_notice(plain)
    assert render_notice(plain, template="{subject}|{from}") == "无附件|someone@qq.com"
    print("   ① 清单格式 / 大小 / 文件名清洗 ✅；无附件时文案与 v0.3.0 逐字一致 ✅")

    # 有附件：正文照旧 + 末尾追加清单（模板模式也一样）
    with_att = mail("y", "有附件", attachments=[att()])
    got = render_notice(with_att)
    assert got.startswith(legacy_v020_notice(with_att)), got
    assert "📎 附件 1 个" in got and "report.txt（29 B · text/plain）" in got, got
    got_tpl = render_notice(with_att, template="{subject}")
    assert got_tpl.startswith("有附件") and "📎 附件 1 个" in got_tpl, got_tpl
    print("   ② 有附件时正文不变、末尾追加「附件名 + 大小 + 类型」清单 ✅（模板模式同样追加）")

    # download_attachments：成功 / 缺 download_url / 下载失败 三种都不抛
    tmp = tempfile.mkdtemp(prefix="agentmail-test-")
    try:
        ok_mailer = FakeAttachMailer([])
        saved, errors = download_attachments(ok_mailer, with_att, tmp)
        assert len(saved) == 1 and not errors, (saved, errors)
        assert saved[0]["filename"] == "report.txt" and saved[0]["size"] == len(FakeAttachMailer.BLOB)
        assert os.path.exists(saved[0]["path"]), "附件应该真的落盘"
        assert ok_mailer.calls[0][0] == "info" and ok_mailer.calls[1][0] == "download"

        saved2, errors2 = download_attachments(FakeAttachMailer([], fail_download=True), with_att, tmp)
        assert saved2 == [] and errors2 and "report.txt" in errors2[0], (saved2, errors2)

        saved3, errors3 = download_attachments(FakeAttachMailer([], missing_url=True), with_att, tmp)
        assert saved3 == [] and any("download_url" in e for e in errors3), errors3

        assert download_attachments(ok_mailer, plain, tmp) == ([], []), "没有附件时不该动网络"
    finally:
        import shutil as _sh

        _sh.rmtree(tmp, ignore_errors=True)
    print("   ③ 下载成功落盘 / 缺 download_url / 下载失败 → 都只报 errors 不抛异常 ✅")


async def test_attachment_push():
    """v0.4.0 功能①：附件随推送（走 _poll_once，离线假 mailer）。"""
    print("\n── 附件随推送（v0.4.0 集成）──")
    base_dirs = set(glob.glob(os.path.join(tempfile.gettempdir(), "agentmail-*")))

    # ④ 有附件 + notify_attachments=true：清单 + 真文件一起发
    ctx = RecordingContext()
    p = AgentMailPlugin(ctx, conf(notify_on_first_poll=True, notify_attachments=True))
    p._mailer = FakeAttachMailer([mail("m1", "报表来了", attachments=[att()])])
    st = await p._poll_once()
    assert st["new"] == 1 and st["pushed"] == 1 and st["files"] == 1, st
    comps = ctx.sent[-1][1].components
    assert len(comps) == 2, comps
    assert isinstance(comps[0], _Plain) and isinstance(comps[1], _File), comps
    assert comps[1].name == "report.txt", comps[1]
    assert ctx.exists_at_send == [True], "发消息那一刻附件文件必须真的在临时目录里"
    assert "📎 附件 1 个" in comps[0].text and "report.txt（29 B · text/plain）" in comps[0].text
    # 临时目录用完即删
    assert not os.path.exists(os.path.dirname(comps[1].file_)), "临时附件目录没有清理"
    print("   ④ 有附件 + notify_attachments=true → Plain + File(report.txt) 一起发出，"
          "清单在正文里，临时目录已清理 ✅")

    # ⑤ 无附件：行为与 v0.3.0 完全一致
    ctx2 = RecordingContext()
    p2 = AgentMailPlugin(ctx2, conf(notify_on_first_poll=True, notify_attachments=True))
    p2._mailer = FakeAttachMailer([mail("m2", "没附件")])
    st2 = await p2._poll_once()
    assert st2["pushed"] == 1 and st2["files"] == 0, st2
    assert len(ctx2.sent[-1][1].components) == 1, ctx2.sent[-1][1].components
    assert ctx2.sent[-1][1].components[0].text == legacy_v020_notice(mail("m2", "没附件"))
    assert p2._mailer.calls == [], "没附件不该去请求附件接口"
    print("   ⑤ 无附件 → 单条 Plain 推送，文案与 v0.3.0 逐字一致，不请求附件接口 ✅")

    # ⑥ notify_attachments 默认关闭：清单照显示，但不下载真文件
    ctx3 = RecordingContext()
    p3 = AgentMailPlugin(ctx3, conf(notify_on_first_poll=True))  # 不传 notify_attachments
    assert p3.conf.get("notify_attachments") in (None, False)
    p3._mailer = FakeAttachMailer([mail("m3", "有附件但没开", attachments=[att()])])
    st3 = await p3._poll_once()
    assert st3["files"] == 0 and st3["pushed"] == 1, st3
    comps3 = ctx3.sent[-1][1].components
    assert len(comps3) == 1 and "📎 附件 1 个" in comps3[0].text, comps3
    assert p3._mailer.calls == [], "关闭 notify_attachments 时不该下载附件"
    print("   ⑥ notify_attachments 默认关闭 → 只发文字清单、零下载（省流量、行为不变）✅")

    # ⑦ 下载失败 → 降级文字清单 + warning，整轮推送不挂
    WARNINGS.clear()
    ctx4 = RecordingContext()
    p4 = AgentMailPlugin(ctx4, conf(notify_on_first_poll=True, notify_attachments=True))
    p4._mailer = FakeAttachMailer([mail("m4", "下载会失败", attachments=[att()])], fail_download=True)
    st4 = await p4._poll_once()
    assert st4["pushed"] == 1 and st4["files"] == 0, st4
    text4 = ctx4.sent[-1][1].components[0].text
    assert "📎 附件 1 个" in text4 and "附件没发出去" in text4, text4
    assert len(ctx4.sent[-1][1].components) == 1, ctx4.sent[-1][1].components
    assert any("附件下载" in w for w in WARNINGS), WARNINGS
    print("   ⑦ 下载失败 → 降级成文字清单 + warning，推送照常完成 ✅")

    # ⑧ 文件消息本身发不出去 → 再降级成纯文字，不重试轰炸
    ctx5 = PickyContext()
    p5 = AgentMailPlugin(ctx5, conf(notify_on_first_poll=True, notify_attachments=True))
    p5._mailer = FakeAttachMailer([mail("m5", "平台不支持文件", attachments=[att()])])
    st5 = await p5._poll_once()
    assert st5["pushed"] == 1 and st5["degraded"] == 1 and st5["files"] == 0, st5
    assert "已降级为文字清单" in ctx5.sent[-1][1].components[0].text
    print("   ⑧ 适配器不支持文件消息 → 自动降级纯文字（degraded=1），不抛不重试 ✅")

    # ⑧b 老版本 AstrBot 没有 File 组件 → 也降级纯文字，不抛
    ctx6 = RecordingContext()
    p6 = AgentMailPlugin(ctx6, conf(notify_on_first_poll=True, notify_attachments=True))
    p6._mailer = FakeAttachMailer([mail("m6", "没有 File 组件", attachments=[att()])])
    saved_file = comp_mod.File
    comp_mod.File = None
    try:
        st6 = await p6._poll_once()
    finally:
        comp_mod.File = saved_file
    assert st6["pushed"] == 1 and st6["files"] == 0, st6
    assert "已降级为文字清单" in ctx6.sent[-1][1].components[0].text
    print("   ⑧b 老版本 AstrBot 没 File 组件 → 同样降级纯文字，不抛异常 ✅")

    # ⑨ 兜底：整轮没有残留临时目录
    leftovers = set(glob.glob(os.path.join(tempfile.gettempdir(), "agentmail-*"))) - base_dirs
    assert not leftovers, f"临时目录没清干净：{leftovers}"
    print("   ⑨ 四轮推送过后 /tmp 无残留 agentmail-* 临时目录 ✅")


async def main():
    key = read_key()
    ctx = FakeContext()
    p = AgentMailPlugin(ctx, conf(**{"api_key": key}))

    await run(p.mail, FakeEvent("/mail"), "无参数 → 帮助")
    await run(p.mail, FakeEvent("/mail send"), "缺参数 → 友好报错")
    await run(p.mail, FakeEvent("/mail send 1150880493@qq.com 拦截测试 | 不该发出去",
                                admin=False, sender="999"), "非白名单 → 拦截")
    await run(p.mail, FakeEvent("/mail here"), "拿当前会话 ID")
    if key and not OFFLINE:
        try:
            await run(p.mail, FakeEvent("/mail list 1"), "查收件箱（真连 API）")
        except Exception as e:  # noqa: BLE001 网络抽查失败不算单测失败
            print("[跳过] 真实 API 抽查失败：", e)
    else:
        print("\n[跳过] 没 key 或 --offline，不连真实 API")

    # ── 收信轮询：用假 mailer，不打网络 ──
    print("\n── 收信轮询 ──")
    p._mailer = FakeMailer([mail("m1", "第一封"), mail("m2", "自己发出的不该推", labels=("sent",))])
    s1 = await p._poll_once()
    print("首次轮询 →", s1, "（应为 baseline=1，把已有一封记基线，sent 的跳过）")
    assert s1["baseline"] == 1 and s1["pushed"] == 0, s1

    s2 = await p._poll_once()
    print("没有新邮件 →", s2, "（应为 new=0）")
    assert s2["new"] == 0, s2

    p._mailer = FakeMailer([mail("m3", "新来的"), mail("m1", "第一封"), mail("m2", "sent", labels=("sent",))])
    s3 = await p._poll_once()
    print("来了一封新的 →", s3, "（应为 new=1, pushed=1）")
    assert s3["new"] == 1 and s3["pushed"] == 1, s3
    print("推给会话：", [s for s, _ in ctx.sent])
    print("推送内容：\n" + ctx.sent[-1][1].components[0].text)

    s4 = await p._poll_once()
    print("再跑一次不重复推 →", s4, "（应为 new=0）")
    assert s4["new"] == 0, s4

    await run(p.mail, FakeEvent("/mail poll"), "手动 /mail poll")

    # ── 单元检查 ──
    print("\n── 单元检查 ──")
    print("_parse_send:", AgentMailPlugin._parse_send("a@x.com,b@y.com 你好 | 正文第一行\n第二行"))
    print("_is_incoming(received) =", AgentMailPlugin._is_incoming({"labels": ["received"]}))
    print("_is_incoming(sent)     =", AgentMailPlugin._is_incoming({"labels": ["sent"]}))
    print("_is_incoming(no label) =", AgentMailPlugin._is_incoming({}))

    # ── v0.3.0 推送模板 ──
    test_notify_template()

    # ── v0.4.0 附件随推送 ──
    test_attachment_helpers()
    await test_attachment_push()

    # ── v0.4.0 /mail search ──
    test_search_helpers()
    await test_search_command()

    # ── v0.4.0 每日未读摘要 ──
    test_digest_helpers()
    await test_digest_schedule()

    print("\n全部断言通过 ✅")


asyncio.run(main())
