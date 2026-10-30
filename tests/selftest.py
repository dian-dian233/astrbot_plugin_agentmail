"""不装 AstrBot 也能测：用假的 astrbot.api 模块桩，把插件真正跑一遍（v0.5.0）。

覆盖：帮助 / 缺参 / 白名单拦截 / 真发信 / 查收件箱 / 会话ID / 收信轮询（基线+推送）
      + v0.3.0 推送模板：默认模板与 v0.2.0 逐字一致 / 自定义模板渲染 / 非法模板回退不抛异常
      + v0.4.0 历史邮件搜索：主题/发件人/正文命中 + 大小写不敏感 + 中文 + 无结果 +
        数量非法夹取 + 服务端搜索参数（q/limit）与本地兜底
      + v0.5.0 功能 A 附件随推送一并送达：清单渲染 / notify_attachments=true 时消息链里
        出现 Comp.File(name, url) / 取下载地址失败降级成清单 / 平台不支持文件组件降级 /
        attachments 脏数据（None、字符串、空 list…）不崩 / 超长清单折叠
      + v0.5.0 功能 B 每日未读摘要：到点触发一次 / 同一天第二次（含重启）不推 /
        digest_time 非法回退 09:00 / 摘要正文（未读数量 + 最新 3 封）/ 会话不可达只记 warning
      + v0.6.0 功能 A 权限拒绝原因化：私聊 / 群聊 / allow_all 类型不对 三种原因各一条，
        文案带「原因 + 解决」，放行四条路（allow_all / 管理员 / allowed_users /
        allowed_groups）都对，指令和 LLM 工具共用同一段文案，日志记下「谁 / 哪个会话 / 哪条规则」
      + v0.6.0 功能 B 发件审计：成功 / 失败都落 sqlite3 一条（时间、触发者、会话类型/ID、
        收件人、CC/BCC、主题、正文长度、message_id、结果）/ 库不在插件仓库目录 /
        `/mail log` 默认 10 条按时间倒序 / `/mail log n` 限条数 / 空库提示 / 没权限看不到
      + v0.6.0 功能 C 发送限流 + CC/BCC：冷却未到期拦住并给出剩余秒数、到期放行、
        每日额度用完拦住并给出剩余条数、两者都超优先报额度、0 = 关掉限制、
        失败不占额度、被拦也记审计；`--cc/--bcc` 解析、真实 cc/bcc 进 payload、
        服务端不认 cc 时降级并把 CC 写进正文且如实回报

底部会打印「通过 / 失败条数」（实测，不是硬编码）。

用法：
    PYTHONDONTWRITEBYTECODE=1 python3 tests/selftest.py            # 含真实 API 抽查（没 key/没网 自动降级为 offline）
    PYTHONDONTWRITEBYTECODE=1 python3 tests/selftest.py --offline  # 只跑本地断言（不需要真 key，用假的 am_test_dummy）

CI 里跑的就是 --offline：不读 ~/.secrets/agentmail.key、不连网络，纯本地断言。
"""
import asyncio
import os
import sys
import tempfile
import types
from datetime import datetime

# 本文件现在在 tests/ 下：HERE 仍然指「仓库根目录」（往上两级），语义和重构前一样，
# 下面的「审计库不许写进仓库目录」断言就靠它。仓库根进 sys.path，包才能被 import。
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFFLINE = "--offline" in sys.argv
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # 兼容放在 plugins/ 下、以包形式导入

# --offline（以及没凭证时自动降级）用的假 key：所有断言都是桩，不会真的打网络。
DUMMY_KEY = "am_test_dummy"

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

    def __repr__(self):
        return f"Plain({self.text[:20]!r})"


class _File:
    """对应 astrbot 的 Comp.File(name=..., url=...)（file 是懒下载的 property，这里不需要）。"""

    def __init__(self, name="", file="", url=""):
        self.name = name
        self.file_ = file
        self.url = url

    def __repr__(self):
        return f"File(name={self.name!r}, url={self.url!r})"


class _MessageChain:
    def __init__(self, components=None):
        self.components = components or []


astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = _log
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

# 目录重构后：模块都在 core/ push/ tools/ 子包里。这里只保留包方式导入，
# 原来的平铺兜底（from attach import ...）随重构一起删掉——根目录已经没有那些模块了。
from astrbot_plugin_agentmail.core.audit import (  # noqa: E402
    AuditLog,
    db_path,
    format_log_rows,
    resolve_data_dir,
)
from astrbot_plugin_agentmail.core.limits import (  # noqa: E402
    after_success,
    check_send_limit,
    format_limit_message,
    format_quota_note,
    read_cooldown,
    read_daily_limit,
)
from astrbot_plugin_agentmail.core.mailer import Mailer, MailError  # noqa: E402
from astrbot_plugin_agentmail.core.perm import (  # noqa: E402
    REASON_BAD_BOOL,
    REASON_DM,
    REASON_GROUP,
    evaluate,
)
from astrbot_plugin_agentmail.core.sendargs import (  # noqa: E402
    SendArgs,
    SendArgsError,
    parse_send_args,
)
from astrbot_plugin_agentmail.main import DIGEST_KEY, AgentMailPlugin  # noqa: E402
from astrbot_plugin_agentmail.push.attach import (  # noqa: E402
    attachment_block,
    extract_attachments,
    human_size,
)
from astrbot_plugin_agentmail.push.digest import (  # noqa: E402
    format_digest,
    is_due,
    next_check_delay,
    parse_time,
)
from astrbot_plugin_agentmail.push.notify import render_notice  # noqa: E402
from astrbot_plugin_agentmail.tools.search import (  # noqa: E402
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
    """假的邮件客户端：messages 可随时替换。search 走 search.py 的本地过滤，不打网络。

    v0.5.0 加 `attachment_info()`：默认按 message_id / attachment_id 拼一个假 download_url，
    传 attachment_error 就抛 MailError（模拟取下载地址失败）。
    v0.6.0 加 `send()`：记下每次发送的收件人 / cc / bcc / 主题 / 正文，返回假 message_id；
    传 send_error 就抛 MailError（模拟发送失败）。
    """

    def __init__(self, messages, attachment_error=None, send_error=None, cc_fallback=False):
        self.messages = messages
        self.search_calls = []
        self.attachment_calls = []
        self.attachment_error = attachment_error
        self.send_error = send_error
        self.cc_fallback = cc_fallback
        self.send_calls = []
        self._seq = 0

    def list(self, limit=5):
        return self.messages[:limit]

    def send(self, to, subject, text="", html="", cc=None, bcc=None):
        self._seq += 1
        self.send_calls.append(
            {"to": to, "subject": subject, "text": text, "html": html, "cc": cc, "bcc": bcc}
        )
        if self.send_error:
            raise MailError(str(self.send_error), 400)
        res = {"message_id": f"fake-msg-{self._seq}", "thread_id": f"fake-thread-{self._seq}"}
        if self.cc_fallback:
            res["cc_fallback"] = True
        return res

    def search(self, keyword, limit=5):
        self.search_calls.append((keyword, limit))
        return filter_messages(self.messages, keyword, limit)

    def attachment_info(self, message_id, attachment_id):
        self.attachment_calls.append((message_id, attachment_id))
        if self.attachment_error:
            raise MailError(str(self.attachment_error), 404)
        return {
            "attachment_id": attachment_id,
            "filename": attachment_id,
            "download_url": f"https://dl.example.com/{message_id}/{attachment_id}",
            "text_url": None,
            "expires_at": "2026-10-27T11:00:00.000Z",
        }


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
    "send_cooldown": 30,
    "send_daily_limit": 50,
    "audit_data_dir": "",  # conf() 会换成每次独立的临时目录
    "poll_enabled": True,
    "poll_interval": 300,
    "notify_sessions": ["aiocqhttp:FriendMessage:1150880493"],
    "notify_preview_len": 300,
    "notify_on_first_poll": False,
    "notify_attachments": False,
    "digest_enabled": False,
    "digest_time": "09:00",
    "digest_sessions": [],
}

# 审计库一律写临时目录（顺带验证「不写插件仓库目录」）
TMP_ROOT = tempfile.mkdtemp(prefix="agentmail-selftest-")


def conf(**over):
    c = dict(BASE_CONF)
    if "audit_data_dir" not in over:  # 每个用例一个干净审计库，互不干扰
        c["audit_data_dir"] = tempfile.mkdtemp(prefix="audit-", dir=TMP_ROOT)
    c.update(over)
    return c


def audit_db_of(plugin) -> str:
    """拿到某个插件实例实际用的审计库路径。"""
    return plugin._audit_log().path


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

    # ③″ 不给 warn 回调时：走 SILENT 兜底（静默、不抛、不依赖标准库日志模块）
    #     上架规则禁止各模块自建日志器，运行期由 main.py 注入 astrbot 的 logger
    assert render_notice(m, template="{foo}") == baseline, "无 warn 时应静默回退，不许抛"
    print("   无 warn 回调 → SILENT 静默兜底，不抛异常 ✅")

    # ③‴ 静态检查：源码里不许出现标准库日志模块（上架硬规定）
    import pathlib as _pl

    _root = _pl.Path(__file__).resolve().parent.parent
    _bad = []
    for _f in _root.rglob("*.py"):
        if ".git" in _f.parts or "tests" in _f.parts:
            continue
        for _i, _line in enumerate(_f.read_text(encoding="utf-8").splitlines(), 1):
            _code = _line.split("#", 1)[0]
            if ("import " + "logging") in _code or ("logging" + ".getLogger") in _code:
                _bad.append(f"{_f.relative_to(_root)}:{_i}")
    assert not _bad, f"源码里出现标准库日志模块（应用 astrbot.api.logger 或注入）：{_bad}"
    print("   静态检查：源码零标准库日志模块引用 ✅")

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


def att(name, size, ctype="application/pdf", aid=None):
    """造一个 AgentMail 风格的附件元素。"""
    return {"attachment_id": aid or f"att-{name}", "filename": name, "size": size, "content_type": ctype}


def attach_msg(mid, atts, subject="带附件的邮件", **kw):
    """造一封带附件的邮件。"""
    m = mail(mid, subject, **kw)
    m["attachments"] = atts
    return m


async def test_attach():
    print("\n── 附件随推送一并送达（v0.5.0 功能 A）──")
    atts = [att("report.pdf", 12600), att("数据.csv", 3234567, "text/csv")]

    # ① 有附件 → 正文预览后面追加清单；没有 attachments 字段 → 文案逐字不变
    p = AgentMailPlugin(FakeContext(), conf())
    text = p._compose_notice(attach_msg("m-att", atts))
    assert "📬 新邮件" in text and "📎 附件 2 个" in text, text
    assert "· report.pdf（12.3KB · application/pdf）" in text, text
    assert "· 数据.csv（3.1MB · text/csv）" in text, text
    print("① 清单渲染 →", repr(text.splitlines()[-3]), repr(text.splitlines()[-2]))
    assert human_size(29) == "29B" and human_size(12600) == "12.3KB" and human_size(None) == "?"
    plain = mail("m-noatt", "没有附件")
    assert p._compose_notice(plain) == legacy_v020_notice(plain), "没有附件时文案必须和 v0.4.0 逐字一致"
    assert attachment_block(None) == "" and attachment_block([]) == ""
    print("① 有附件清单渲染（12.3KB / 3.1MB）+ 无附件时文案与 v0.4.0 逐字一致 ✅")

    # ② notify_attachments=true → 同一条消息链里出现 File 组件，name / url 都对
    ctx = FakeContext()
    p2 = AgentMailPlugin(ctx, conf(notify_attachments=True, notify_on_first_poll=True))
    fake = FakeMailer([attach_msg("m-att", atts)])
    p2._mailer = fake
    s = await p2._poll_once()
    assert s["pushed"] == 1 and s["files"] == 2, s
    session, chain = ctx.sent[-1]
    assert session == "aiocqhttp:FriendMessage:1150880493", session
    comps = chain.components
    assert isinstance(comps[0], _Plain) and "📎 附件 2 个" in comps[0].text, comps
    files = [c for c in comps if isinstance(c, _File)]
    assert [f.name for f in files] == ["report.pdf", "数据.csv"], files
    assert files[0].url == "https://dl.example.com/m-att/att-report.pdf", files[0].url
    assert files[1].url == "https://dl.example.com/m-att/att-数据.csv", files[1].url
    assert fake.attachment_calls == [("m-att", "att-report.pdf"), ("m-att", "att-数据.csv")], fake.attachment_calls
    print("② notify_attachments=true → 一条消息链 = Plain + 2 个 File")
    for f in files:
        print("   File", f.name, "→", f.url)
    assert s["degraded"] == 0

    # ②′ notify_attachments=false（默认）→ 只有清单，不出现 File 组件、不问下载地址
    ctx0 = FakeContext()
    p2b = AgentMailPlugin(ctx0, conf(notify_on_first_poll=True))
    fake0 = FakeMailer([attach_msg("m-att", atts)])
    p2b._mailer = fake0
    s0 = await p2b._poll_once()
    assert s0["pushed"] == 1 and s0["files"] == 0, s0
    assert [c for c in ctx0.sent[-1][1].components if isinstance(c, _File)] == []
    assert fake0.attachment_calls == [], f"notify_attachments=false 不该问下载地址：{fake0.attachment_calls}"
    print("②′ 默认 notify_attachments=false → 只有文字清单，零 File 组件、零下载请求 ✅")

    # ③ 取下载地址失败 → 只记 warning，正文 + 清单照发（绝不挂整轮推送）
    ctx3 = FakeContext()
    p3 = AgentMailPlugin(ctx3, conf(notify_attachments=True, notify_on_first_poll=True))
    p3._mailer = FakeMailer([attach_msg("m-att", atts)], attachment_error="HTTP 404：附件不存在")
    WARNINGS.clear()
    s3 = await p3._poll_once()
    assert s3["pushed"] == 1 and s3["files"] == 0, s3
    text3 = ctx3.sent[-1][1].components[0].text
    assert "📬 新邮件" in text3 and "📎 附件 2 个" in text3, text3
    assert "⚠️ 附件没能一起发" in text3 and "report.pdf" in text3, text3
    assert any("取下载地址失败" in w for w in WARNINGS), WARNINGS
    print("③ 下载地址取不到 → warning + 降级成清单（正文照发，files=0）✅")
    print("   ", [ln for ln in text3.splitlines() if "⚠️" in ln][0][:60], "…")

    # ③′ 平台不支持 file 组件 → 自动降级为纯文字清单重试
    class _NoFileContext(FakeContext):
        async def send_message(self, session, chain):
            if any(isinstance(c, _File) for c in chain.components):
                raise RuntimeError("该平台不支持 file 组件")
            return await super().send_message(session, chain)

    ctx4 = _NoFileContext()
    p4 = AgentMailPlugin(ctx4, conf(notify_attachments=True, notify_on_first_poll=True))
    p4._mailer = FakeMailer([attach_msg("m-att", atts)])
    WARNINGS.clear()
    s4 = await p4._poll_once()
    assert s4["pushed"] == 1 and s4["degraded"] == 1, s4
    assert "📎 附件 2 个" in ctx4.sent[-1][1].components[0].text
    assert any("降级" in w for w in WARNINGS), WARNINGS
    print("③′ 平台不支持 file 组件 → 自动降级纯文字清单重试成功（degraded=1）✅")

    # ④ attachments 字段异常（None / 字符串 / 空 list / 脏元素 / 非 dict）→ 不崩
    weird_vals = [None, "", "report.pdf", 123, {}, [], [None, 1, "x"], ("a", "b")]
    for weird in weird_vals:
        mm = mail("m-weird", "脏数据")
        mm["attachments"] = weird
        got = p._compose_notice(mm)  # 不能抛
        assert got == legacy_v020_notice(mm), (weird, got)
        assert extract_attachments(mm) == [], (weird, extract_attachments(mm))
    mm_no = mail("m-missing", "连字段都没有")
    assert p._compose_notice(mm_no) == legacy_v020_notice(mm_no)
    mm_partial = mail("m-partial", "缺字段的附件")
    mm_partial["attachments"] = [{"attachment_id": "x"}]
    got2 = p._compose_notice(mm_partial)
    assert "📎 附件 1 个" in got2 and "未知类型" in got2, got2
    ctx5 = FakeContext()
    p5 = AgentMailPlugin(ctx5, conf(notify_attachments=True, notify_on_first_poll=True))
    p5._mailer = FakeMailer([mail("m-weird", "脏数据"), mm_partial])
    p5._mailer.messages[0]["attachments"] = None
    s5 = await p5._poll_once()  # 整轮推送也不许崩
    assert s5["pushed"] >= 1, s5
    print(f"④ attachments 异常（{len(weird_vals)} 种脏值 + 字段缺失 + 缺子字段）全部安全跳过、整轮推送不崩 ✅")

    # ⑤ 附件太多 / 清单太长：折叠 + 总长度兜底
    many = [att(f"file-{i}.txt", 1024 * i, "text/plain") for i in range(1, 21)]
    block = attachment_block(many)
    assert "📎 附件 20 个" in block and "…还有 12 个附件（共 20 个）" in block, block
    assert block.count("· file-") == 8, block.count("· file-")
    long_text = p._compose_notice(attach_msg("m-many", many))
    assert len(long_text) <= 1501, len(long_text)
    print("⑤ 20 个附件 → 只列 8 条 + 「…还有 12 个」，整条推送长度 ≤ 1500 字 ✅")


def dmail(mid, subject, ts, labels=("received", "unread"), frm="boss@corp.com"):
    """造一封带指定时间的邮件（摘要排序用）。"""
    m = mail(mid, subject, labels=labels, frm=frm)
    m["timestamp"] = ts
    return m


async def test_digest():
    print("\n── 每日未读摘要（v0.5.0 功能 B）──")
    today = datetime.now().date().isoformat()

    # ① 到点触发一次：3 封未读 + 2 封已读/自己发的
    inbox = [
        dmail("d5", "最新的这封", "2026-10-27T12:00:00.000Z"),
        dmail("d4", "第二新", "2026-10-27T11:00:00.000Z"),
        dmail("d3", "第三新", "2026-10-27T10:00:00.000Z"),
        dmail("d2", "已读的", "2026-10-27T09:00:00.000Z", labels=("received", "read")),
        dmail("d1", "自己发的", "2026-10-27T08:00:00.000Z", labels=("sent",)),
    ]
    ctx = FakeContext()
    p = AgentMailPlugin(ctx, conf(digest_enabled=True, digest_time="00:00"))
    p._mailer = FakeMailer(inbox)
    s1 = await p._digest_once()
    assert s1 == {"unread": 3, "pushed": 1, "skipped": False}, s1
    assert len(ctx.sent) == 1, ctx.sent
    session, chain = ctx.sent[0]
    assert session == "aiocqhttp:FriendMessage:1150880493", session
    text = chain.components[0].text
    assert await p.get_kv_data(DIGEST_KEY, "") == today, await p.get_kv_data(DIGEST_KEY, "")
    print("① 到点触发一次 →", s1, "｜KV 记住推送日期：", today, "✅")

    # ② 同一天第二次不推（含「进程重启」：新实例共享同一份 KV，也不许重复推）
    s2 = await p._digest_once()
    assert s2 == {"unread": 0, "pushed": 0, "skipped": True}, s2
    assert len(ctx.sent) == 1, "同一天不该推第二次"
    ctx_restart = FakeContext()
    p_restart = AgentMailPlugin(ctx_restart, conf(digest_enabled=True, digest_time="00:00"))
    p_restart._kv = dict(p._kv)  # 模拟重启后读回同一份插件 KV
    p_restart._mailer = FakeMailer(inbox)
    s2b = await p_restart._digest_once()
    assert s2b["pushed"] == 0 and ctx_restart.sent == [], s2b
    print("② 同一天第二次不推 + 重启后（KV 里已有今天）也不重复推 ✅")

    # ②′ 跨天逻辑用纯函数验（不依赖跑测的时刻）：不到点不推 / 到点推 / 隔天再推
    assert is_due(datetime(2026, 10, 27, 8, 59), 9, 0, "") is False
    assert is_due(datetime(2026, 10, 27, 9, 0), 9, 0, "") is True
    assert is_due(datetime(2026, 10, 27, 9, 0), 9, 0, "2026-10-27") is False
    assert is_due(datetime(2026, 10, 28, 9, 0), 9, 0, "2026-10-27") is True
    assert is_due(datetime(2026, 10, 27, 23, 59), 9, 0, "") is True  # 中途启动补推当天
    assert 0 < next_check_delay(datetime(2026, 10, 27, 8, 0), 9, 0) == 3600
    assert next_check_delay(datetime(2026, 10, 27, 9, 0), 9, 0) == 86400
    print("②′ is_due 纯函数：08:59 不推 / 09:00 推 / 当天已推不推 / 隔天再推 / 23:59 补推 ✅")

    # ③ digest_time 格式非法 → 回退 09:00 + warning，不抛异常
    bad_times = ["25:00", "9点", "", "9:70", "abc", "24:00", "09:00:00"]
    for raw in bad_times:
        WARNINGS.clear()
        assert parse_time(raw, warn=_warn) == (9, 0), raw
        assert any("digest_time" in w for w in WARNINGS), (raw, WARNINGS)
    for raw, exp in [("09:00", (9, 0)), ("7:30", (7, 30)), ("21：30", (21, 30)), ("0:00", (0, 0)), (None, (9, 0))]:
        WARNINGS.clear()
        assert parse_time(raw, warn=_warn) == exp, (raw, parse_time(raw, warn=_warn))
        assert WARNINGS == [], (raw, WARNINGS)
    WARNINGS.clear()
    p_bad = AgentMailPlugin(FakeContext(), conf(digest_enabled=True, digest_time="25:00"))
    assert p_bad._digest_clock() == (9, 0) and any("digest_time" in w for w in WARNINGS)
    print("③ digest_time 非法（25:00 / 9点 / 空 / 9:70 / abc / 24:00 / 09:00:00）→ 全回退 09:00 + warning，零异常 ✅")
    print("   合法写法 09:00 / 7:30 / 21：30 / 0:00 / 未配置(默认) 都不吵 warning ✅")

    # ④ 摘要正文内容：数量 + 最新 3 封的「时间 · 发件人 · 主题」
    assert "未读邮件：3 封" in text, text
    assert "最新 3 封：" in text, text
    lines = [ln for ln in text.splitlines() if ln[:2] in ("1.", "2.", "3.")]
    assert len(lines) == 3, lines
    assert "最新的这封" in lines[0] and "第二新" in lines[1] and "第三新" in lines[2], lines
    for ln in lines:  # 时间 · 发件人 · 主题
        assert ln.count(" · ") == 2, ln
    assert "boss@corp.com" in lines[0], lines[0]
    print("④ 摘要正文 →", repr(text.splitlines()[0]), "｜", repr(lines[0]))
    assert "未读邮件：3 封" in text and len(lines) == 3
    print("④ 正文内容正确：数量（3 封）+ 最新 3 封倒序（时间 · 发件人 · 主题）✅")

    # ④′ 0 封未读 / 拉满 50 封两种边界文案
    assert "未读邮件：0 封，邮箱是干净的" in format_digest(0, [])
    assert "未读邮件：50+ 封" in format_digest(50, inbox[:3])
    print("④′ 边界：0 封 →「邮箱是干净的」，拉满 50 封 →「50+ 封」✅")

    # ⑤ digest_sessions 留空 → 回退 notify_sessions；会话不可达 → 只记 warning
    ctx5 = FakeContext()
    p5 = AgentMailPlugin(ctx5, conf(digest_enabled=True, digest_time="00:00", digest_sessions=[]))
    p5._mailer = FakeMailer(inbox)
    assert p5._digest_sessions() == ["aiocqhttp:FriendMessage:1150880493"], p5._digest_sessions()
    ctx6 = FakeContext()
    p6 = AgentMailPlugin(
        ctx6, conf(digest_enabled=True, digest_time="00:00", digest_sessions=["aiocqhttp:GroupMessage:999"])
    )
    p6._mailer = FakeMailer(inbox)
    assert p6._digest_sessions() == ["aiocqhttp:GroupMessage:999"]
    s5 = await p5._digest_once()
    assert s5["pushed"] == 1 and ctx5.sent[0][0] == "aiocqhttp:FriendMessage:1150880493", s5

    class _DeadContext(FakeContext):
        async def send_message(self, session, chain):
            raise RuntimeError("会话不可达")

    p7 = AgentMailPlugin(_DeadContext(), conf(digest_enabled=True, digest_time="00:00"))
    p7._mailer = FakeMailer(inbox)
    WARNINGS.clear()
    s7 = await p7._digest_once()
    assert s7["pushed"] == 0 and s7["skipped"] is True, s7
    assert any("未读摘要推送到" in w for w in WARNINGS), WARNINGS
    assert await p7.get_kv_data(DIGEST_KEY, "") == "", "一个会话都没推成功时不该标记今天已推"
    print("⑤ digest_sessions 留空回退 notify_sessions；推送失败只记 warning 且不标记当天 ✅")

    # ⑥ 和收信轮询共用同一个 Mailer 实例（不另开连接）
    ctx8 = FakeContext()
    p8 = AgentMailPlugin(ctx8, conf(digest_enabled=True, digest_time="00:00"))
    shared = FakeMailer(inbox)
    p8._mailer = shared
    assert p8._mailer_or_raise() is shared
    await p8._digest_once()
    await p8._poll_once()
    assert p8._mailer is shared, "摘要和轮询必须共用同一个 Mailer"
    print("⑥ 摘要和收信轮询共用同一个 Mailer 实例（同一个 _mailer_or_raise）✅")


def test_permission():
    print("\n── 权限拒绝原因化（v0.6.0 功能 A）──")
    base = {"allow_all": False, "allowed_users": ["1150880493"], "allowed_groups": ["680787260"]}

    # ① 私聊：allow_all 关 + 用户不在 allowed_users + 会话也不在 allowed_groups
    d = evaluate({**base, "allowed_users": []}, sender="999", group="", umo="aiocqhttp:FriendMessage:999")
    assert d.allowed is False and d.reason == REASON_DM, d
    assert "这个会话没有邮件权限" in d.message
    assert "原因：" in d.message and "解决：" in d.message
    assert "999 不在 allowed_users" in d.message, d.message
    assert "WebUI → 插件配置 → allowed_users" in d.message, d.message
    assert "私聊" in d.message and "allowed_groups 只对群聊生效" in d.message
    print("① 私聊被拒 →", d.reason)
    for line in d.message.splitlines():
        print("   ", line)

    # ② 群聊：allow_all 关 + 用户不在 allowed_users + 群不在 allowed_groups
    d2 = evaluate(base, sender="999", group="111222", umo="aiocqhttp:GroupMessage:111222")
    assert d2.allowed is False and d2.reason == REASON_GROUP, d2
    assert "999 不在 allowed_users" in d2.message and "群 111222 也不在 allowed_groups" in d2.message, d2.message
    assert "把群号 111222 填进 allowed_groups" in d2.message, d2.message
    print("② 群聊被拒 →", d2.reason)
    for line in d2.message.splitlines():
        print("   ", line)

    # ③ allow_all 的类型不对（老代码会把 "off" 这种非空字符串当成「开」，直接放行所有人）
    for raw in ("off", "false", "关闭吧", "maybe", [1], {}):
        d3 = evaluate({"allow_all": raw, "allowed_users": [], "allowed_groups": []},
                      sender="999", group="111222", umo="aiocqhttp:GroupMessage:111222")
        assert d3.allowed is False, (raw, d3)
        assert d3.reason == REASON_BAD_BOOL, (raw, d3.reason)
        assert "不是真正的布尔值" in d3.message and "allow_all" in d3.message, (raw, d3.message)
    d3b = evaluate({"allow_all": "off", "allowed_users": [], "allowed_groups": []}, sender="999")
    assert "已按「关闭」处理" in d3b.message, d3b.message
    print("③ allow_all 类型不对（off / false / 关闭吧 / maybe / [1] / {}）→ 按关闭处理 + 指出正确写法 ✅")

    # ④ 四种放行路径：allow_all / 管理员 / allowed_users / allowed_groups
    allow_cases = [
        ({"allow_all": True, "allowed_users": [], "allowed_groups": []}, "999", "", False, "allow_all"),
        ({"allow_all": False, "allowed_users": [], "allowed_groups": []}, "999", "", True, "admin"),
        (base, "1150880493", "", False, "allowed_users"),
        (base, "999", "680787260", False, "allowed_groups"),
        ({"allow_all": "true", "allowed_users": [], "allowed_groups": []}, "999", "", False, "allow_all"),
        ({"allow_all": 2, "allowed_users": [], "allowed_groups": []}, "999", "", False, "allow_all"),
    ]
    for cfg, sender, group, admin, rule in allow_cases:
        got = evaluate(cfg, sender=sender, group=group, is_admin=admin)
        assert got.allowed is True and got.rule == rule, (cfg, sender, group, admin, got)
        assert got.message == "" and got.reason == "ok"
    print("④ 放行四条路：allow_all / 管理员 / allowed_users / allowed_groups（含字符串 \"true\"、数字 2）✅")

    # ⑤ 拿不到 ID 也不能崩，要说清「没法比白名单」
    d5 = evaluate(base, sender="", group="", umo="")
    assert d5.allowed is False and d5.reason == "not_whitelisted_no_id", d5
    assert "没法跟白名单比对" in d5.message
    print("⑤ 拿不到 sender/群号 →", d5.reason, "（不崩，给出替代解法）✅")
    return d


async def test_permission_integration():
    """/mail 和 LLM 工具都要用新的原因文案，且日志里记下「谁 / 哪个会话 / 被哪条规则挡」。"""
    print("\n── 权限拒绝：指令 / LLM 工具 / 日志（v0.6.0 功能 A）──")
    ctx = FakeContext()
    p = AgentMailPlugin(ctx, conf(allowed_users=["1150880493"], allowed_groups=[]))
    p._mailer = FakeMailer([])

    WARNINGS.clear()
    out = await collect(p.mail, FakeEvent("/mail", admin=False, sender="1150880493"))
    assert len(out) == 1 and "AgentMail 邮件工具" in out[0], out  # 本人在白名单里 → 放行，回帮助
    WARNINGS.clear()
    evt = FakeEvent("/mail send a@x.com 测试 | 正文", admin=False, sender="999")
    out = await collect(p.mail, evt)
    assert "原因：" in out[0] and "解决：" in out[0], out
    assert "这个会话没开邮件权限" not in out[0], "还在用 v0.5.x 那句老文案"
    assert any("权限拒绝" in w and "reason=not_whitelisted_dm" in w and "user=999" in w for w in WARNINGS), WARNINGS
    print("① /mail send 被拒 → 原因 + 解决（不再是一句「发不了」）")
    for line in out[0].splitlines():
        print("   ", line)
    print("   日志 →", [w for w in WARNINGS if "权限拒绝" in w][0])

    WARNINGS.clear()
    res = await p.agentmail_send(evt, "a@x.com", "测试", "正文")
    assert res == p._permission(evt).message, res
    assert "原因：" in res and "解决：" in res and "allowed_users" in res, res
    assert any("权限拒绝" in w for w in WARNINGS), WARNINGS
    print("② LLM 工具 agentmail_send 返回同一段原因文案（模型能直接转述给用户）✅")

    # 群聊场景：文案里要同时出现 QQ 号和群号
    WARNINGS.clear()
    g_evt = FakeEvent("/mail send a@x.com 群里试试 | 正文", admin=False, sender="999",
                      group="111222", umo="aiocqhttp:GroupMessage:111222")
    out = await collect(p.mail, g_evt)
    assert "111222" in out[0] and "999" in out[0] and "allowed_groups" in out[0], out
    assert any("reason=not_whitelisted_group" in w for w in WARNINGS), WARNINGS
    print("③ 群聊被拒 → 群里文案同时点出 QQ 号 999 和群号 111222 ✅")


async def test_audit():
    print("\n── 发件审计 + /mail log（v0.6.0 功能 B）──")
    repo = os.path.realpath(HERE)
    ctx = FakeContext()
    p = AgentMailPlugin(ctx, conf(send_cooldown=0, send_daily_limit=0))
    p._mailer = FakeMailer([])
    db = audit_db_of(p)
    print("审计库：", db)
    assert db.endswith(os.path.join("plugin_data", "astrbot_plugin_agentmail", "mail_audit.sqlite3")), db
    assert db_path(os.path.dirname(os.path.dirname(os.path.dirname(db)))) == db, db
    assert not os.path.realpath(db).startswith(repo), f"审计库不许写进插件仓库目录：{db}"
    default_dir = resolve_data_dir()
    assert not os.path.realpath(default_dir).startswith(repo), f"默认数据目录也不许落在仓库里：{default_dir}"
    assert os.path.isdir(default_dir), default_dir
    deep = os.path.join(tempfile.mkdtemp(prefix="audit-deep-", dir=TMP_ROOT), "a", "b", "x.sqlite3")
    fresh = AuditLog(deep)
    assert fresh.total() == 0 and fresh.recent(5) == [] and os.path.isfile(deep), "新建库应自动建目录、空表可读"
    print("① 落盘位置：<data>/plugin_data/<插件名>/mail_audit.sqlite3，且不在仓库目录里 ✅")

    # ② 成功发送 → 落一条完整记录
    out = await collect(p.mail, FakeEvent("/mail send 1150880493@qq.com 第一封 | 正文四个字"))
    assert "发出去了" in out[0], out
    rows = p._audit_log().recent(10)
    assert len(rows) == 1, rows
    r = rows[0]
    assert r["ok"] == 1, r
    assert r["user_id"] == "1150880493" and r["session_type"] == "private", r
    assert r["session_id"] == "1150880493", r
    assert r["recipients"] == "1150880493@qq.com" and r["subject"] == "第一封", r
    assert r["body_len"] == 5 and r["message_id"] == "fake-msg-1", r
    assert r["ts"] and int(r["duration_ms"]) >= 0 and not r["error"], r
    print("② 成功一条落库：", r["ts"], r["session_type"], r["session_id"], "→", r["recipients"], "id=", r["message_id"])

    # ③ 失败也要落（成功和失败都记）
    p._mailer = FakeMailer([], send_error="HTTP 400：bad request")
    out2 = await collect(p.mail, FakeEvent("/mail send x@y.com 会失败的 | 正文"))
    assert "邮件接口出错" in out2[0], out2
    r2 = p._audit_log().recent(1)[0]
    assert r2["ok"] == 0 and "HTTP 400" in r2["error"] and r2["message_id"] == "", r2
    print("③ 发送失败也落库：ok=0，error =", r2["error"])

    # ④ 群聊记录带群号和 session_type=group；CC/BCC 也落库
    p._mailer = FakeMailer([])
    out3 = await collect(p.mail, FakeEvent(
        "/mail send a@x.com --cc b@x.com,c@x.com --bcc d@x.com 群里带抄送 | 正文",
        group="680787260", umo="aiocqhttp:GroupMessage:680787260",
    ))
    assert "发出去了" in out3[0], out3
    call = p._mailer.send_calls[-1]
    assert call["cc"] == ["b@x.com", "c@x.com"] and call["bcc"] == ["d@x.com"], call
    r3 = p._audit_log().recent(1)[0]
    assert r3["session_type"] == "group" and r3["session_id"] == "680787260", r3
    assert r3["cc"] == "b@x.com, c@x.com" and r3["bcc"] == "d@x.com", r3
    print("④ 群聊 + CC/BCC 落库：", r3["session_type"], r3["session_id"], "cc=", r3["cc"], "bcc=", r3["bcc"])

    # ⑤ 读取：/mail log 默认 10 条、按时间倒序、格式固定
    total = p._audit_log().total()
    assert total == 3, total
    log_out = await collect(p.mail, FakeEvent("/mail log"))
    assert log_out[0] == format_log_rows(p._audit_log().recent(10), total), log_out[0]
    assert "发件记录（最近 3 条，按时间倒序）" in log_out[0], log_out[0]
    blocks = log_out[0].split("────────────")
    assert "群里带抄送" in blocks[0], "最新的一条必须排在最前面"
    assert "第一封" in blocks[-1], blocks[-1]
    print("⑤ /mail log 默认 10 条、倒序：最新在最前 ✅")
    print("   " + log_out[0].splitlines()[0])

    # ⑥ /mail log n 限条数；n 乱写回默认 10；上限 50
    log2 = await collect(p.mail, FakeEvent("/mail log 2"))
    assert log2[0].count("发给：") == 2, log2[0]
    assert "最近 2 条" in log2[0] and "库里共 3 条" in log2[0], log2[0]
    assert (await collect(p.mail, FakeEvent("/mail log abc")))[0].count("发给：") == 3
    print("⑥ /mail log 2 → 2 条（并标出库里共 3 条）；/mail log abc → 回默认 10 ✅")

    # ⑦ 空库友好提示 + 没权限的人看不到记录
    p_empty = AgentMailPlugin(FakeContext(), conf())
    p_empty._mailer = FakeMailer([])
    empty = await collect(p_empty.mail, FakeEvent("/mail log"))
    assert "还没有发件记录" in empty[0], empty
    denied = await collect(p.mail, FakeEvent("/mail log", admin=False, sender="999"))
    assert "没有邮件权限" in denied[0] and "发给：" not in denied[0], denied
    print("⑦ 空库提示「还没有发件记录」；非白名单用户 /mail log 被拦（看不到别人的记录）✅")

    # ⑧ 纯函数排版：脏数据不崩
    assert format_log_rows([]) == "还没有发件记录。发一封就会记一条（成功和失败都记）。"
    assert format_log_rows([{"ok": 1, "ts": "2026-10-30 10:00:00"}]).startswith("📨 发件记录（最近 1 条")
    assert format_log_rows(None) == format_log_rows([])
    print("⑧ 排版纯函数：空列表 / None / 缺字段的脏记录都不崩 ✅")


async def test_limits():
    print("\n── 发送限流 + CC/BCC（v0.6.0 功能 C）──")

    # ① 冷却：还没到期 → 拦住，并说清「还要等几秒」
    d = check_send_limit(now=1000.0, last_sent=990.0, used_today=0, cooldown=30, daily_limit=50)
    assert d["allowed"] is False and d["reason"] == "cooldown", d
    assert d["retry_after"] == 20 and d["elapsed"] == 10, d
    msg = format_limit_message(d)
    assert "还要等 20 秒" in msg and "剩余：20 秒" in msg and "send_cooldown" in msg, msg
    assert "上一次是 10 秒前发的" in msg, msg
    print("① 冷却未到期 →", d["retry_after"], "秒后可发")
    for line in msg.splitlines():
        print("   ", line)

    # ② 冷却到期 → 放行
    d2 = check_send_limit(now=1030.0, last_sent=990.0, used_today=0, cooldown=30, daily_limit=50)
    assert d2["allowed"] is True and d2["reason"] == "ok" and d2["remaining"] == 50, d2
    assert format_limit_message(d2) == "", "放行时不该有拦截文案"
    d2b = check_send_limit(now=1000.0, last_sent=None, cooldown=30, daily_limit=50)
    assert d2b["allowed"] is True, "第一次发（没有历史）必须放行"
    assert check_send_limit(now=1000.0, last_sent=1000.0, cooldown=0, daily_limit=50)["allowed"] is True
    print("② 冷却到期 / 第一次发 / send_cooldown=0 → 放行 ✅")

    # ③ 每日额度：用完 → 拦住，并说清「剩下 0 封」
    d4 = check_send_limit(now=5000.0, last_sent=None, used_today=50, cooldown=0, daily_limit=50)
    assert d4["allowed"] is False and d4["reason"] == "daily_limit" and d4["remaining"] == 0, d4
    msg4 = format_limit_message(d4)
    assert "今天已经发满 50 封" in msg4 and "剩余：0 封（今天已用 50/50）" in msg4, msg4
    assert "明天 0 点" in msg4 and "send_daily_limit" in msg4, msg4
    print("③ 每日额度用完 →", d4["used"], "/", d4["daily_limit"], "剩", d4["remaining"], "封")
    for line in msg4.splitlines():
        print("   ", line)
    # 两个都超 → 报额度（别让人白等冷却）
    both = check_send_limit(now=1000.0, last_sent=999.0, used_today=50, cooldown=30, daily_limit=50)
    assert both["reason"] == "daily_limit", both
    assert check_send_limit(now=0, used_today=999, cooldown=0, daily_limit=0)["allowed"] is True
    print("④ 冷却和额度同时超 → 优先报额度；send_daily_limit=0 / send_cooldown=0 → 关掉限制 ✅")

    # ⑤ 配置读取容错
    assert read_cooldown(None) == 30 and read_cooldown("0") == 0 and read_cooldown("abc") == 30
    assert read_cooldown(-5) == 0 and read_cooldown("45") == 45 and read_cooldown(True) == 1
    assert read_daily_limit(None) == 50 and read_daily_limit("7") == 7 and read_daily_limit("x") == 50
    assert read_daily_limit(-1) == 0
    assert format_quota_note(after_success({"daily_limit": 50, "used": 49, "remaining": 1})) == \
        "今日额度：剩 0/50 封（今天已发 50 封）"
    assert format_quota_note({"daily_limit": 0, "remaining": -1}) == ""
    print("⑤ 配置读取/额度提示：默认 30 秒 / 50 封，写错回默认，0 = 关掉 ✅")

    # ⑥ 集成：连发第二封被冷却挡住，换个人不受影响，冷却过了能发
    p = AgentMailPlugin(FakeContext(), conf(send_cooldown=30, send_daily_limit=50))
    p._mailer = FakeMailer([])
    evt = FakeEvent("/mail send a@x.com 第一封 | 111")
    o1 = await collect(p.mail, evt)
    assert "发出去了" in o1[0] and "今日额度：剩 49/50 封" in o1[0], o1
    o2 = await collect(p.mail, evt)
    assert "还要等" in o2[0] and "秒" in o2[0] and "send_cooldown" in o2[0], o2
    assert len(p._mailer.send_calls) == 1, "被限流的那次不许真的调发送接口"
    p.conf["allowed_users"] = ["1150880493", "888"]
    o3 = await collect(p.mail, FakeEvent("/mail send a@x.com 第二封 | 222", sender="888"))
    assert "发出去了" in o3[0], "限流是按人算的，别人不该被连坐"
    import sqlite3

    log = p._audit_log()
    with sqlite3.connect(log.path) as conn:  # 把本人那条成功记录往前挪 60 秒＝冷却已过
        conn.execute("UPDATE mail_audit SET ts_epoch = ts_epoch - 60 WHERE user_id = ? AND ok = 1",
                     ("1150880493",))
    o4 = await collect(p.mail, evt)
    assert "发出去了" in o4[0], o4
    assert len(p._mailer.send_calls) == 3, p._mailer.send_calls
    print("⑥ 集成：第二封被冷却拦（不调接口，回「还要等 N 秒」）→ 换人照发 → 冷却过后放行 ✅")

    # ⑦ 集成：每日额度用完的文案 + 被拦也记审计
    p2 = AgentMailPlugin(FakeContext(), conf(send_cooldown=0, send_daily_limit=1))
    p2._mailer = FakeMailer([])
    a1 = await collect(p2.mail, FakeEvent("/mail send a@x.com 一 | 1"))
    assert "发出去了" in a1[0] and "剩 0/1 封" in a1[0], a1
    a2 = await collect(p2.mail, FakeEvent("/mail send a@x.com 二 | 2"))
    assert "今天已经发满 1 封" in a2[0] and "剩余：0 封" in a2[0], a2
    assert len(p2._mailer.send_calls) == 1
    blocked = p2._audit_log().recent(1)[0]
    assert blocked["ok"] == 0 and "限流：daily_limit" in blocked["error"], blocked
    print("⑦ 集成：send_daily_limit=1 → 第二封回「今天已发满 1 封，剩 0 封」，且被拦也留审计 ✅")

    # ⑧ 失败的那次不吃冷却/额度
    p3 = AgentMailPlugin(FakeContext(), conf(send_cooldown=30, send_daily_limit=50))
    p3._mailer = FakeMailer([], send_error="HTTP 500：boom")
    f1 = await collect(p3.mail, FakeEvent("/mail send a@x.com 失败 | x"))
    assert "邮件接口出错" in f1[0], f1
    p3._mailer = FakeMailer([])
    f2 = await collect(p3.mail, FakeEvent("/mail send a@x.com 接着发 | x"))
    assert "发出去了" in f2[0], "失败的一次不该占冷却/额度，紧接着必须能发"
    print("⑧ 发送失败不占冷却和额度（只统计成功记录），紧接着能重发 ✅")

    # ⑨ CC/BCC 参数解析
    a = parse_send_args("a@x.com,b@y.com --cc c@x.com,d@x.com --bcc e@x.com 明天见 | 正文第一行")
    assert isinstance(a, SendArgs), a
    assert a.to == ["a@x.com", "b@y.com"], a
    assert a.cc == ["c@x.com", "d@x.com"] and a.bcc == ["e@x.com"], a
    assert a.subject == "明天见" and a.body == "正文第一行", a
    b = parse_send_args("a@x.com --cc c@x.com 明天见 | 正文")
    assert b.cc == ["c@x.com"] and b.subject == "明天见", "紧跟 --cc 的中文词不能被当成抄送地址"
    c = parse_send_args("a@x.com --CC c@x.com 周报")
    assert c.cc == ["c@x.com"] and c.subject == "周报" and c.body == "", c
    d9 = parse_send_args("a@x.com 没有正文只有主题")
    assert d9.subject == "没有正文只有主题" and d9.body == "" and d9.cc == [], d9
    for bad in ("", "   ", "a@x.com --cc", "a@x.com --cc 明天见 | 正文", "--cc a@x.com 主题 | 正文"):
        try:
            parse_send_args(bad)
        except SendArgsError:
            continue
        raise AssertionError(f"这些参数应该报错：{bad!r}")
    print("⑨ --cc/--bcc 解析：多个地址 / 大小写 / 中文主题不被吃掉 / 缺地址报错 ✅")

    # ⑩ Mailer 真的把 cc/bcc 放进 payload；服务端不认时降级并如实回报
    class _CaptureMailer(Mailer):
        def __init__(self, fail_first=None):
            super().__init__(api_key="k", inbox="box@agentmail.to")
            self.payloads = []
            self.fail_first = fail_first

        def request(self, path, method="GET", body=None):
            self.payloads.append(body)
            if self.fail_first and len(self.payloads) == 1:
                raise MailError(self.fail_first, 400)
            return {"message_id": "real-1", "thread_id": "t-1"}

    m = _CaptureMailer()
    m.send(["a@x.com"], "主题", "正文", cc=["c@x.com"], bcc=["d@x.com"])
    assert m.payloads[-1]["cc"] == ["c@x.com"] and m.payloads[-1]["bcc"] == ["d@x.com"], m.payloads[-1]
    m2 = _CaptureMailer()
    m2.send(["a@x.com"], "主题", "正文")
    assert "cc" not in m2.payloads[-1] and "bcc" not in m2.payloads[-1], "没写 cc/bcc 时请求体和 v0.5.x 一致"
    print("⑩ 真实 cc/bcc 进 payload：", m.payloads[-1])

    m3 = _CaptureMailer(fail_first="unknown field 'cc'")
    res = m3.send(["a@x.com"], "主题", "正文", cc=["c@x.com"])
    assert res.get("cc_fallback") is True and len(m3.payloads) == 2, (res, m3.payloads)
    assert "cc" not in m3.payloads[1] and "CC: c@x.com" in m3.payloads[1]["text"], m3.payloads[1]
    print("⑩′ 服务端不认 cc → 去掉字段重发 + CC 写进正文 + 回报 cc_fallback=True（不假装支持）✅")

    m4 = _CaptureMailer(fail_first="invalid 'to' address")
    try:
        m4.send(["a@x.com"], "主题", "正文", cc=["c@x.com"])
    except MailError as e:
        assert e.status == 400 and len(m4.payloads) == 1, (e, m4.payloads)
    else:
        raise AssertionError("别的 400 不该被当成 cc 不支持")
    print("⑩″ 与 cc 无关的 400 不退化，照常抛（不吞错）✅")


async def main():
    global OFFLINE
    key = read_key()
    real_api = True
    if OFFLINE:
        # 离线模式：不需要真 key，用假 key 跑（断言全是桩，不打网络）
        key = key or DUMMY_KEY
        real_api = False
        print(f"[offline] 只跑本地断言，使用假 key {DUMMY_KEY!r}")
    elif not key:
        # 没 --offline 又读不到 key：友好提示后自动降级，不要 traceback
        print("[提示] 没找到 ~/.secrets/agentmail.key —— 自动降级为 offline 模式（只跑本地断言，不连网络）。")
        print("       想要真连抽查：把 AgentMail 的 API Key 写进 ~/.secrets/agentmail.key 再跑一遍。")
        OFFLINE = True
        key = DUMMY_KEY
        real_api = False

    ctx = FakeContext()
    p = AgentMailPlugin(ctx, conf(**{"api_key": key}))

    await run(p.mail, FakeEvent("/mail"), "无参数 → 帮助")
    await run(p.mail, FakeEvent("/mail send"), "缺参数 → 友好报错")
    await run(p.mail, FakeEvent("/mail send 1150880493@qq.com 拦截测试 | 不该发出去",
                                admin=False, sender="999"), "非白名单 → 拦截")
    await run(p.mail, FakeEvent("/mail here"), "拿当前会话 ID")
    if real_api:
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

    # ── v0.5.0 功能 A：附件随推送一并送达 ──
    await test_attach()

    # ── v0.5.0 功能 B：每日未读摘要 ──
    await test_digest()

    # ── v0.6.0 功能 A：权限拒绝原因化 ──
    test_permission()
    await test_permission_integration()

    # ── v0.6.0 功能 B：发件审计 + /mail log ──
    await test_audit()

    # ── v0.6.0 功能 C：发送限流 + CC/BCC ──
    await test_limits()


# ── 断言计数（v0.6.0）：让「通过/失败条数」是实测出来的，不是拍脑袋 ─────
ASSERT_STATS = {"pass": 0, "fail": 0}


def _install_assert_counter():
    """用 sys.settrace 数「执行到的 assert 语句」条数。

    不重写历史断言（828 行里的 assert 一个都不动），只在行事件里看这行源码
    是不是以 `assert` 开头；失败的那条会让脚本抛 AssertionError，
    由 `__main__` 兜住并记一条失败。
    """
    import linecache

    target = os.path.abspath(__file__)
    cache: dict[int, str] = {}

    def local_trace(frame, event, arg):
        if event == "line" and os.path.abspath(frame.f_code.co_filename) == target:
            lineno = frame.f_lineno
            text = cache.get(lineno)
            if text is None:
                text = linecache.getline(target, lineno).lstrip()
                cache[lineno] = text
            if text.startswith("assert ") or text.startswith("assert("):
                ASSERT_STATS["pass"] += 1
        return local_trace

    def global_trace(frame, event, arg):
        if event == "call" and os.path.abspath(frame.f_code.co_filename) == target:
            return local_trace
        return None

    sys.settrace(global_trace)


def _summary() -> int:
    ok, fail = ASSERT_STATS["pass"], ASSERT_STATS["fail"]
    print("\n── 断言统计 ──")
    print(f"通过：{ok} 条   失败：{fail} 条")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    _install_assert_counter()
    try:
        asyncio.run(main())
    except AssertionError as e:
        ASSERT_STATS["fail"] += 1
        print("\n[FAIL] 断言失败：", e)
        sys.exit(_summary())
    print("\n全部断言通过 ✅")
    sys.exit(_summary())
