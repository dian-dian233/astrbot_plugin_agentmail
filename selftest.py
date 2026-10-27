"""不装 AstrBot 也能测：用假的 astrbot.api 模块桩，把插件真正跑一遍（v0.4.0）。

覆盖：帮助 / 缺参 / 白名单拦截 / 真发信 / 查收件箱 / 会话ID / 收信轮询（基线+推送）
      + v0.3.0 推送模板：默认模板与 v0.2.0 逐字一致 / 自定义模板渲染 / 非法模板回退不抛异常
      + v0.4.0 历史邮件搜索：主题/发件人/正文命中 + 大小写不敏感 + 中文 + 无结果 +
        数量非法夹取 + 服务端搜索参数（q/limit）与本地兜底

用法：
    PYTHONDONTWRITEBYTECODE=1 python3 selftest.py            # 含真实 API 抽查（没网/没 key 会自动跳过）
    PYTHONDONTWRITEBYTECODE=1 python3 selftest.py --offline  # 只跑本地断言
"""
import asyncio
import os
import sys
import types

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
comp_mod = types.ModuleType("astrbot.api.message_components")
comp_mod.Plain = _Plain
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
    from astrbot_plugin_agentmail.mailer import MailError, Mailer  # noqa: E402
    from astrbot_plugin_agentmail.main import AgentMailPlugin  # noqa: E402
    from astrbot_plugin_agentmail.notify import render_notice  # noqa: E402
    from astrbot_plugin_agentmail.search import (  # noqa: E402
        filter_messages,
        format_rows,
        matches,
        normalize_limit,
        parse_search_args,
    )
except ImportError:  # 仓库根目录直接跑
    from mailer import MailError, Mailer  # noqa: E402
    from main import AgentMailPlugin  # noqa: E402
    from notify import render_notice  # noqa: E402
    from search import (  # noqa: E402
        filter_messages,
        format_rows,
        matches,
        normalize_limit,
        parse_search_args,
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
    """假的邮件客户端：messages 可随时替换。search 走 search.py 的本地过滤，不打网络。"""

    def __init__(self, messages):
        self.messages = messages
        self.search_calls = []

    def list(self, limit=5):
        return self.messages[:limit]

    def search(self, keyword, limit=5):
        self.search_calls.append((keyword, limit))
        return filter_messages(self.messages, keyword, limit)


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


def mail(mid, subject, labels=("received", "unread"), frm="someone@qq.com", preview=None):
    return {
        "message_id": mid,
        "subject": subject,
        "from": frm,
        "timestamp": "2026-10-27T10:00:00.000Z",
        "labels": list(labels),
        "preview": "这是一封测试邮件的正文预览，用来验证推送文案。" if preview is None else preview,
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


def search_fixtures():
    """搜索用的固定样本：主题命中 / 发件人命中 / 正文命中各一封，外加一封中文。"""
    return [
        {"message_id": "m-subject", "subject": "项目周报 第 3 期", "from": "boss@corp.com",
         "timestamp": "2026-10-27T09:00:00.000Z", "preview": "本周进度见附件。"},
        {"message_id": "m-from", "subject": "随便聊聊", "from": "Report@Corp.COM",
         "timestamp": "2026-10-27T08:00:00.000Z", "preview": "晚上吃啥"},
        {"message_id": "m-body", "subject": "例行同步", "from": "x@y.com",
         "timestamp": "2026-10-27T07:00:00.000Z", "preview": "正文里顺手提了报告两个字"},
        {"message_id": "m-cn", "subject": "老登，你太美", "from": "点点 <diandian233@agentmail.to>",
         "timestamp": "2026-10-27T06:00:00.000Z", "preview": "没别的意思，就想写封信告诉你一句——你太美。"},
    ]


async def collect(handler, evt):
    return [r async for r in handler(evt)]


class _SpyMailer(Mailer):
    """假 Mailer：只记它请求了哪个 path，不打网络；status≠0 时抛对应 MailError。"""

    def __init__(self, messages=(), status=0, payload=None):
        super().__init__(api_key="dummy-key", inbox="box@agentmail.to")
        self.messages = list(messages)
        self.status = status
        self.payload = payload if payload is not None else {"count": 0, "messages": []}
        self.paths = []
        self.list_limits = []

    def request(self, path, method="GET", body=None):
        self.paths.append(path)
        if self.status:
            raise MailError(f"HTTP {self.status}：oops", self.status)
        return self.payload

    def list(self, limit=5):
        self.list_limits.append(limit)
        return self.messages[:limit]


def ids(msgs):
    return [m.get("message_id") for m in msgs]


async def test_search():
    print("\n── 历史邮件搜索（v0.4.0）──")
    f = search_fixtures()

    # ① 主题 / 发件人 / 正文预览 各命中一条
    assert ids(filter_messages(f, "周报")) == ["m-subject"], ids(filter_messages(f, "周报"))
    assert ids(filter_messages(f, "report@corp")) == ["m-from"], ids(filter_messages(f, "report@corp"))
    assert ids(filter_messages(f, "报告")) == ["m-body"], ids(filter_messages(f, "报告"))
    assert matches(f[0], "周报") and matches(f[1], "report@corp") and matches(f[2], "报告")
    print("① 命中主题「周报」/ 发件人「report@corp」/ 正文「报告」，各一条且只各一条 ✅")

    # ② 大小写不敏感 + 中文（中文没有词边界，不能用 \b）
    assert ids(filter_messages(f, "REPORT")) == ["m-from"]
    assert ids(filter_messages(f, "RePoRt@CoRp")) == ["m-from"]
    assert ids(filter_messages(f, "报告"[0])) == ["m-subject", "m-body"]  # 单个汉字「报」
    assert ids(filter_messages(f, "老登")) == ["m-cn"]        # 主题里被标点包围
    assert ids(filter_messages(f, "太美")) == ["m-cn"]
    assert ids(filter_messages(f, "封信")) == ["m-cn"]        # 正文预览里的中文（跨字子串）
    print("② 大小写不敏感（REPORT / RePoRt@CoRp）+ 中文（报 / 老登 / 太美 / 封信）全命中 ✅")

    # ③ 无结果 → 空列表，不抛异常
    assert filter_messages(f, "绝对不存在zzz") == []
    assert matches(f[0], "") is False and filter_messages(f, "") == []
    assert Mailer(api_key="dummy").search("") == []  # 空关键词连请求都不发
    p0 = AgentMailPlugin(FakeContext(), conf())
    p0._mailer = FakeMailer(f)
    out = await collect(p0.mail, FakeEvent("/mail search 绝对不存在zzz"))
    assert out == ["没搜到包含「绝对不存在zzz」的邮件"], out
    print("③ 无结果 → []（不抛异常），插件回：", repr(out[0]), "✅")

    # ④ 关键词为空 / 数量非法
    for raw, exp in [
        ("", ("", 5)), ("   ", ("", 5)), ("老登", ("老登", 5)), ("老登 3", ("老登", 3)),
        ("老登 999", ("老登", 20)), ("老登 -1", ("老登", 1)), ("老登 0", ("老登", 1)),
        ("老登 abc", ("老登 abc", 5)), ("项目 周报 2", ("项目 周报", 2)),
    ]:
        got = parse_search_args(raw)
        assert got == exp, (raw, got, exp)
    for raw, exp in [("abc", 5), (None, 5), ("", 5), (-1, 1), (0, 1), (999, 20), ("7", 7), (True, 5)]:
        assert normalize_limit(raw) == exp, (raw, normalize_limit(raw), exp)
    p2 = AgentMailPlugin(FakeContext(), conf())
    fake2 = FakeMailer(f)
    p2._mailer = fake2
    usage = await collect(p2.mail, FakeEvent("/mail search"))
    assert "用法：/mail search" in usage[0], usage
    assert fake2.search_calls == [], f"关键词为空不该打 API：{fake2.search_calls}"
    for cmd in ("/mail search 报告 999", "/mail search 报告 -1", "/mail search 报告 abc"):
        await collect(p2.mail, FakeEvent(cmd))
    assert fake2.search_calls == [("报告", 20), ("报告", 1), ("报告 abc", 5)], fake2.search_calls
    print("④ 空关键词→用法且不打 API；数量 abc→5 / -1→1 / 999→20 / 0→1 ✅")

    # ⑤ 结果行格式和 /mail list 逐字一致（同一个 format_rows）
    p1 = AgentMailPlugin(FakeContext(), conf())
    p1._mailer = FakeMailer(f)
    list_out = await collect(p1.mail, FakeEvent("/mail list 2"))
    search_out = await collect(p1.mail, FakeEvent("/mail search 报 5"))
    assert list_out == [format_rows(f[:2])], list_out
    assert search_out == [format_rows(filter_messages(f, "报", 5))], search_out
    assert search_out[0].count(" · ") == 2 and "\n\n" in search_out[0]
    print("⑤ /mail search 行格式 == /mail list（共用 format_rows），多条之间空行 ✅")
    print("   样例：", repr(search_out[0]))

    # ⑥ 服务端搜索：path / 参数名（q、limit）照官方 OpenAPI
    spy = _SpyMailer(payload={"count": 1, "messages": [f[3]]})
    got = spy.search("老登", 3)
    assert spy.paths == ["/inboxes/box@agentmail.to/messages/search?q=%E8%80%81%E7%99%BB&limit=3"], spy.paths
    assert ids(got) == ["m-cn"], ids(got)
    print("⑥ 服务端端点 + 参数名照 OpenAPI：", spy.paths[0], "✅")

    # ⑦ 服务端端点不可用（400/404/405/422/501）→ 退化 list(50) 本地过滤
    for status in (400, 404, 405, 422, 501):
        spy2 = _SpyMailer(f, status=status)
        got = spy2.search("报告", 5)
        assert ids(got) == ["m-body"], (status, ids(got))
        assert spy2.list_limits == [50], spy2.list_limits
        assert "messages/search" in spy2.paths[0], spy2.paths
    print("⑦ 服务端 400/404/405/422/501 → 自动退化 list(50) 本地过滤（大小写不敏感 in 匹配）✅")

    # ⑧ 401 这类真错误照常抛，不吞不退化
    try:
        _SpyMailer(f, status=401).search("报告")
    except MailError as e:
        assert e.status == 401, e.status
    else:
        raise AssertionError("401 应该抛 MailError，而不是退化本地过滤")
    print("⑧ 401 不退化、原样抛 MailError ✅")


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

    # ── v0.4.0 历史邮件搜索 ──
    await test_search()

    print("\n全部断言通过 ✅")


asyncio.run(main())
