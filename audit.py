"""发件审计（v0.6.0）：每次发送都落一条，`/mail log [n]` 按时间倒序回看。

**存储只用标准库**：`sqlite3`（requirements.txt 那条「纯标准库实现」不许破）。

**落盘位置**：AstrBot 的 data 目录，不是插件仓库目录 ——
    <data>/plugin_data/astrbot_plugin_agentmail/mail_audit.sqlite3
仓库目录是 git 工作区，往里写库会变成脏文件（而且装在 docker 里时可能是只读的）。

数据目录怎么找（`resolve_data_dir`，按优先级）：
    1. 配置项 `audit_data_dir`（留空则跳过，给人手动指定用）
    2. 环境变量 `AGENTMAIL_DATA_DIR` / `ASTRBOT_DATA_DIR`
    3. `astrbot.core.utils.astrbot_path.get_astrbot_data_path()`（AstrBot 官方工具）
    4. 插件装在 `<root>/data/plugins/<插件名>` 时，反推出 `<root>/data`
    5. `./data`（AstrBot 根目录启动时就是它）
    6. `~/AstrBot/data`、`~/.astrbot/data`
    7. 都不行 → 系统临时目录，并留一条 warning
被判定「在插件仓库目录里」的候选一律跳过（第 5 条在仓库里跑测试时会命中，直接跳过）。

表 `mail_audit` 一条记录 = 一次发送尝试：
    ts / ts_epoch   本地时间（给人看）和 epoch 秒（给限流算间隔、算当天条数）
    user_id         触发者
    session_type    private / group
    session_id      私聊的对方 ID / 群号
    umo             完整 unified_msg_origin
    recipients      收件人（逗号分隔）
    cc / bcc        抄送 / 密送（只有真的发出去了才有）
    subject         主题
    body_len        正文字数
    message_id      AgentMail 返回的 id（失败时为空）
    ok              1 = 成功，0 = 失败（含被限流拦下的）
    error           失败原因
    duration_ms     耗时毫秒
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
from datetime import datetime

PLUGIN_NAME = "astrbot_plugin_agentmail"
DB_NAME = "mail_audit.sqlite3"

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    ts_epoch     REAL    NOT NULL,
    user_id      TEXT    NOT NULL DEFAULT '',
    session_type TEXT    NOT NULL DEFAULT '',
    session_id   TEXT    NOT NULL DEFAULT '',
    umo          TEXT    NOT NULL DEFAULT '',
    recipients   TEXT    NOT NULL DEFAULT '',
    cc           TEXT    NOT NULL DEFAULT '',
    bcc          TEXT    NOT NULL DEFAULT '',
    subject      TEXT    NOT NULL DEFAULT '',
    body_len     INTEGER NOT NULL DEFAULT 0,
    message_id   TEXT    NOT NULL DEFAULT '',
    ok           INTEGER NOT NULL DEFAULT 0,
    error        TEXT    NOT NULL DEFAULT '',
    duration_ms  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mail_audit_user ON mail_audit (user_id, ok, ts_epoch);
CREATE INDEX IF NOT EXISTS idx_mail_audit_time ON mail_audit (ts_epoch);
"""

COLUMNS = (
    "ts", "ts_epoch", "user_id", "session_type", "session_id", "umo",
    "recipients", "cc", "bcc", "subject", "body_len", "message_id",
    "ok", "error", "duration_ms",
)


def _warn(warn, message: str):
    """有回调就回调（插件里传 astrbot logger.warning），没有就静默。"""
    if callable(warn):
        with contextlib.suppress(Exception):
            warn(message)


def _inside(path: str, parent: str) -> bool:
    """path 是否在 parent 目录里（含相等），用 realpath 比，抗软链接。"""
    try:
        p = os.path.realpath(path)
        q = os.path.realpath(parent)
        return p == q or os.path.commonpath([p, q]) == q
    except (ValueError, OSError):
        return False


def _candidates(explicit: str = "") -> list[tuple[str, bool]]:
    """候选数据目录，返回 `(路径, 允许新建)`。

    `允许新建=False` 的「猜出来的」路径只在**已经存在**时才用 ——
    不为了跑个单测就在别人机器上凭空造 `~/AstrBot/data`。
    """
    out: list[tuple[str, bool]] = []
    if str(explicit or "").strip():
        out.append((str(explicit).strip(), True))
    for env in ("AGENTMAIL_DATA_DIR", "ASTRBOT_DATA_DIR"):
        value = os.environ.get(env, "").strip()
        if value:
            out.append((value, True))
    try:  # AstrBot 官方工具函数（不同版本位置不同，拿不到就跳过）
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        out.append((str(get_astrbot_data_path()), True))
    except Exception:  # noqa: BLE001 没有 astrbot（跑单测）时一定失败
        pass
    # 插件装在 <root>/data/plugins/<插件名> 时，反推 <root>/data
    parent = os.path.dirname(_PLUGIN_DIR)
    grand = os.path.dirname(parent)
    if os.path.basename(parent) == "plugins" and os.path.basename(grand) == "data":
        out.append((grand, True))
    out.append((os.path.join(os.getcwd(), "data"), True))
    out.append((os.path.join(os.path.expanduser("~"), "AstrBot", "data"), False))
    out.append((os.path.join(os.path.expanduser("~"), ".astrbot", "data"), False))
    return out


def resolve_data_dir(explicit: str = "", *, warn=None) -> str:
    """找一个可写的 AstrBot data 目录；**绝不返回插件仓库里的目录**。"""
    for cand, allow_create in _candidates(explicit):
        path = os.path.realpath(os.path.abspath(os.path.expanduser(cand)))
        if _inside(path, _PLUGIN_DIR):
            _warn(warn, f"[agentmail] 跳过数据目录 {path}：它在插件仓库目录里，审计库不写仓库")
            continue
        if allow_create:
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                continue
        elif not os.path.isdir(path):
            continue
        if not os.access(path, os.W_OK):
            continue
        return path
    fallback = os.path.join(tempfile.gettempdir(), PLUGIN_NAME)
    os.makedirs(fallback, exist_ok=True)
    _warn(warn, f"[agentmail] 没找到可写的 AstrBot data 目录，审计库先放临时目录：{fallback}")
    return fallback


def db_path(data_dir: str) -> str:
    """审计库文件完整路径：<data>/plugin_data/<插件名>/mail_audit.sqlite3。"""
    return os.path.join(data_dir, "plugin_data", PLUGIN_NAME, DB_NAME)


def row_to_dict(row) -> dict:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


class AuditLog:
    """审计库（sqlite3）。每次操作现开连接：量小、免掉跨线程共享连接的坑。"""

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record(
        self,
        *,
        user_id: str = "",
        session_type: str = "",
        session_id: str = "",
        umo: str = "",
        recipients="",
        cc="",
        bcc="",
        subject: str = "",
        body_len: int = 0,
        message_id: str = "",
        ok: bool = False,
        error: str = "",
        duration_ms: int = 0,
        now: float | None = None,
        ts: str | None = None,
    ) -> int:
        """记一条；返回自增 id（失败就失败，由调用方的 try 兜着）。"""
        epoch = float(now if now is not None else datetime.now().timestamp())
        stamp = ts or datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
        values = (
            stamp, epoch, str(user_id or ""), str(session_type or ""), str(session_id or ""),
            str(umo or ""), str(recipients or ""), str(cc or ""), str(bcc or ""),
            str(subject or ""), int(body_len or 0), str(message_id or ""),
            1 if ok else 0, str(error or ""), int(duration_ms or 0),
        )
        placeholders = ", ".join("?" * len(COLUMNS))
        with self._conn() as conn:
            cur = conn.execute(
                f"INSERT INTO mail_audit ({', '.join(COLUMNS)}) VALUES ({placeholders})", values
            )
            return int(cur.lastrowid or 0)

    def recent(self, limit: int = 10) -> list[dict]:
        """按时间倒序取最近 limit 条。"""
        n = max(1, min(200, int(limit or 10)))
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM mail_audit ORDER BY ts_epoch DESC, id DESC LIMIT ?", (n,)
            ).fetchall()
        return [row_to_dict(r) for r in rows]

    def total(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM mail_audit").fetchone()
        return int(row["n"] if row else 0)

    def stats(self, user_id: str, since_epoch: float) -> dict:
        """限流用：这个人从 since_epoch 起成功发了几封 + 最后一次成功的时间。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS used, MAX(ts_epoch) AS last FROM mail_audit "
                "WHERE user_id = ? AND ok = 1 AND ts_epoch >= ?",
                (str(user_id or ""), float(since_epoch)),
            ).fetchone()
        used = int(row["used"] or 0) if row else 0
        last = row["last"] if row and row["last"] is not None else None
        return {"used": used, "last": None if last is None else float(last)}


def _mark(ok: bool) -> str:
    return "✅ 成功" if ok else "❌ 失败"


def format_log_rows(rows, total: int | None = None) -> str:
    """把审计记录排成 `/mail log` 的消息体（纯函数，selftest 直接断言）。"""
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return "还没有发件记录。发一封就会记一条（成功和失败都记）。"
    head = f"📨 发件记录（最近 {len(rows)} 条"
    if total is not None and total > len(rows):
        head += f"，库里共 {total} 条"
    head += "，按时间倒序）"
    lines = [head]
    for row in rows:
        ok = bool(row.get("ok"))
        kind = "群" if str(row.get("session_type")) == "group" else "私聊"
        lines.append(
            f"{str(row.get('ts') or '?')} · {_mark(ok)} · {kind} {row.get('session_id') or '?'}"
            f" · 触发者 {row.get('user_id') or '?'}"
        )
        to = str(row.get("recipients") or "?")
        extra = []
        if str(row.get("cc") or "").strip():
            extra.append(f"CC {row['cc']}")
        if str(row.get("bcc") or "").strip():
            extra.append(f"BCC {row['bcc']}")
        lines.append(f"  发给：{to}" + (f"（{'，'.join(extra)}）" if extra else ""))
        meta = f"  主题：{row.get('subject') or '(无主题)'} · 正文 {int(row.get('body_len') or 0)} 字"
        if ok and str(row.get("message_id") or "").strip():
            meta += f" · id：{row['message_id']}"
        lines.append(meta)
        if not ok and str(row.get("error") or "").strip():
            lines.append(f"  失败：{row['error']}")
        lines.append("────────────")
    if lines[-1] == "────────────":
        lines.pop()
    return "\n".join(lines)
