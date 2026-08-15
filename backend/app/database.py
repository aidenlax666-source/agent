from __future__ import annotations
"""SQLite persistence layer. Replaces in-memory storage with real database."""

import sqlite3, uuid, json, os, asyncio, time
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

DB_PATH = Path(__file__).parent.parent / "data" / "automation.db"
DB_PATH.parent.mkdir(exist_ok=True)

_executor = ThreadPoolExecutor(max_workers=4)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


# ============================================================
# Synchronous DB operations (run in thread pool)
# ============================================================

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_db():
    """Create tables if not exist."""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                name TEXT,
                password_hash TEXT NOT NULL,
                credits INTEGER DEFAULT 10,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                project_id TEXT,
                detail TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS mini_tasks (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,              -- 匿名 id 或登录用户 id
                requirement TEXT NOT NULL,
                url TEXT DEFAULT '',
                status TEXT DEFAULT 'queued',
                message TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL,
                result TEXT,                        -- JSON 结果
                error TEXT,
                schedule_type TEXT DEFAULT '',
                schedule_value TEXT DEFAULT '',
                enabled INTEGER DEFAULT 0,
                last_run_at REAL,
                next_run_at REAL
            );

            -- Guest user created by API on first request if needed
        """)


# ============================================================
# Async wrapper
# ============================================================

async def _run_async(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, lambda: fn(*args, **kwargs))


# ============================================================
# User operations
# ============================================================

def _create_user(email: str, name: str | None, password_hash: str) -> dict:
    import sqlite3
    uid = _uid()
    now = _now()
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO users (id, email, name, password_hash, credits, created_at) VALUES (?,?,?,?,10,?)",
                (uid, email, name, password_hash, now))
        return {"id": uid, "email": email, "name": name, "credits": 10, "created_at": now}
    except sqlite3.IntegrityError:
        # 并发创建竞态：email 已存在（另一请求刚插入），回读已有用户
        existing = _get_user_by_email(email)
        if existing:
            return existing
        raise


def _get_user_by_email(email: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    return dict(row) if row else None


def _get_user(uid: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(row) if row else None


async def get_user(uid: str) -> dict | None:
    return await _run_async(_get_user, uid)


async def get_user_by_email(email: str) -> dict | None:
    return await _run_async(_get_user_by_email, email)


async def create_user(email: str, name: str | None, password_hash: str) -> dict:
    return await _run_async(_create_user, email, name, password_hash)


# ============================================================
# Mini tasks（一句话自动化任务，持久化，重启不丢）
# ============================================================

def _save_mini_task(record: dict) -> None:
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO mini_tasks (id, user_id, requirement, url, status, message, created_at, updated_at, result, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                record["id"], record.get("user_id", ""), record["requirement"], record.get("url", ""),
                record.get("status", "queued"), record.get("message", ""),
                record.get("created_at", 0), time.time(),
                json.dumps(record.get("result"), ensure_ascii=False) if record.get("result") else None,
                record.get("error"),
            ),
        )


# mini_tasks 允许动态更新的列白名单（防 SQL 注入：字段名只能来自白名单）
_ALLOWED_MINI_UPDATE_FIELDS = {"status", "message", "result", "error", "progress", "url", "requirement", "updated_at"}


def _update_mini_task(task_id: str, **fields) -> None:
    # 只保留白名单内的列，键名不可由外部输入控制
    fields = {k: v for k, v in fields.items() if k in _ALLOWED_MINI_UPDATE_FIELDS}
    if not fields:
        return
    import time as _t
    fields["updated_at"] = _t.time()
    sets = ", ".join(f"{k}=?" for k in fields)
    with _get_conn() as conn:
        conn.execute(f"UPDATE mini_tasks SET {sets} WHERE id=?", (*fields.values(), task_id))


def _get_mini_task(task_id: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM mini_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("result"):
        try:
            d["result"] = json.loads(d["result"])
        except (json.JSONDecodeError, TypeError):
            d["result"] = None
    return d


def _list_mini_tasks(limit: int = 30) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM mini_tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        if d.get("result"):
            try:
                d["result"] = json.loads(d["result"])
            except (json.JSONDecodeError, TypeError):
                d["result"] = None
        out.append(d)
    return out


async def save_mini_task(record: dict) -> None:
    return await _run_async(_save_mini_task, record)


async def update_mini_task(task_id: str, **fields) -> None:
    return await _run_async(_update_mini_task, task_id, **fields)


async def get_mini_task(task_id: str) -> dict | None:
    return await _run_async(_get_mini_task, task_id)


async def list_mini_tasks(limit: int = 30) -> list[dict]:
    return await _run_async(_list_mini_tasks, limit)


# ---- mini 定时执行 ----

def _ensure_mini_schedule_columns() -> None:
    """老库补列（新表已含）。"""
    try:
        with _get_conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(mini_tasks)")}
            for col, ddl in [
                ("schedule_type", "TEXT DEFAULT ''"),
                ("schedule_value", "TEXT DEFAULT ''"),
                ("enabled", "INTEGER DEFAULT 0"),
                ("last_run_at", "REAL"),
                ("next_run_at", "REAL"),
            ]:
                if col not in cols:
                    conn.execute(f"ALTER TABLE mini_tasks ADD COLUMN {col} {ddl}")
    except Exception:
        pass


def _get_due_mini_tasks(now: float) -> list[dict]:
    _ensure_mini_schedule_columns()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM mini_tasks WHERE enabled=1 AND next_run_at IS NOT NULL AND next_run_at <= ?",
            (now,),
        ).fetchall()
    return [dict(r) for r in rows]


def _set_mini_schedule(task_id: str, schedule_type: str, schedule_value: str, enabled: bool, next_run_at: float | None) -> None:
    _ensure_mini_schedule_columns()
    with _get_conn() as conn:
        conn.execute(
            "UPDATE mini_tasks SET schedule_type=?, schedule_value=?, enabled=?, next_run_at=?, updated_at=? WHERE id=?",
            (schedule_type, schedule_value, 1 if enabled else 0, next_run_at, time.time(), task_id),
        )


def _update_mini_run(task_id: str, last_run_at: float, next_run_at: float | None) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE mini_tasks SET last_run_at=?, next_run_at=?, updated_at=? WHERE id=?",
            (last_run_at, next_run_at, time.time(), task_id),
        )


async def ensure_mini_schedule_columns() -> None:
    return await _run_async(_ensure_mini_schedule_columns)


async def get_due_mini_tasks(now: float) -> list[dict]:
    return await _run_async(_get_due_mini_tasks, now)


async def set_mini_schedule(task_id: str, schedule_type: str, schedule_value: str, enabled: bool, next_run_at: float | None) -> None:
    return await _run_async(_set_mini_schedule, task_id, schedule_type, schedule_value, enabled, next_run_at)


async def update_mini_run(task_id: str, last_run_at: float, next_run_at: float | None) -> None:
    return await _run_async(_update_mini_run, task_id, last_run_at, next_run_at)


# ============================================================
# Credits（用量配额）
# ============================================================

def _get_credits(user_id: str) -> int:
    with _get_conn() as conn:
        row = conn.execute("SELECT credits FROM users WHERE id=?", (user_id,)).fetchone()
    return row["credits"] if row else 0


def _try_decrement_credits(user_id: str, amount: int = 1) -> bool:
    """原子扣减积分：仅当余额足够才扣，返回是否成功（防并发竞态透支）。"""
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE users SET credits = credits - ? WHERE id=? AND credits >= ?",
            (amount, user_id, amount))
        return cur.rowcount == 1


async def get_credits(user_id: str) -> int:
    return await _run_async(_get_credits, user_id)


async def try_decrement_credits(user_id: str, amount: int = 1) -> bool:
    return await _run_async(_try_decrement_credits, user_id, amount)


# ============================================================
# Audit logs
# ============================================================

def _log_audit(user_id: str, action: str, detail: str = "", project_id: str | None = None) -> None:
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_logs (id, user_id, action, project_id, detail, created_at) VALUES (?,?,?,?,?,?)",
            (_uid(), user_id, action, project_id, detail, _now()),
        )


async def log_audit(user_id: str, action: str, detail: str = "", project_id: str | None = None) -> None:
    return await _run_async(_log_audit, user_id, action, detail, project_id)


# ============================================================
# Init
# ============================================================

async def init_db():
    await _run_async(_init_db)
    print(f"[DB] SQLite initialized at {DB_PATH}")
