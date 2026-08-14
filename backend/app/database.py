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

            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                name TEXT DEFAULT 'Untitled',
                target_url TEXT NOT NULL DEFAULT '',
                requirement TEXT NOT NULL,
                status TEXT DEFAULT 'draft',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS script_versions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                version_number INTEGER NOT NULL,
                script_code TEXT NOT NULL,
                source TEXT DEFAULT 'generation',
                parent_version_id TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS executions (
                id TEXT PRIMARY KEY,
                script_version_id TEXT NOT NULL REFERENCES script_versions(id),
                execution_type TEXT DEFAULT 'preview',
                status TEXT DEFAULT 'pending',
                error_log TEXT,
                result_preview TEXT,
                result_file_path TEXT,
                executed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feedbacks (
                id TEXT PRIMARY KEY,
                script_version_id TEXT NOT NULL REFERENCES script_versions(id),
                user_feedback TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                requirement TEXT NOT NULL,          -- 原始需求
                script_code TEXT NOT NULL,          -- 要执行的脚本
                schedule_type TEXT DEFAULT 'interval',  -- interval(每隔X) / daily(每天X点)
                schedule_value TEXT DEFAULT '60',   -- interval:分钟数；daily: HH:MM
                enabled INTEGER DEFAULT 1,          -- 是否启用
                last_run_at TEXT,                   -- 上次执行时间
                next_run_at TEXT,                   -- 下次执行时间
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,               -- create_project / generate / run_full / upload / ...
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

            CREATE TABLE IF NOT EXISTS scheduled_task_runs (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,              -- 关联 scheduled_tasks.id
                status TEXT DEFAULT 'success',      -- success / failed
                log TEXT,                           -- 执行日志/错误信息
                result_file_path TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS script_library (
                id TEXT PRIMARY KEY,
                req_key TEXT UNIQUE NOT NULL,       -- 规范化需求 key（用于命中复用）
                requirement TEXT NOT NULL,          -- 需求描述（介绍）
                url TEXT NOT NULL,                  -- 目标 URL
                domain TEXT,                        -- 关联域名（登录态）
                needs_login INTEGER DEFAULT 0,      -- 是否涉及登录（1=是）
                script_path TEXT NOT NULL,          -- 脚本文件路径
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
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


def _update_mini_task(task_id: str, **fields) -> None:
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


def _decrement_credits(user_id: str, amount: int = 1) -> int:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE users SET credits = MAX(credits - ?, 0) WHERE id=?",
            (amount, user_id))
        row = conn.execute("SELECT credits FROM users WHERE id=?", (user_id,)).fetchone()
    return row["credits"] if row else 0


async def get_credits(user_id: str) -> int:
    return await _run_async(_get_credits, user_id)


async def decrement_credits(user_id: str, amount: int = 1) -> int:
    return await _run_async(_decrement_credits, user_id, amount)


# ============================================================
# Project operations
# ============================================================

def _create_project(user_id: str, target_url: str, requirement: str, name: str) -> dict:
    pid = _uid()
    now = _now()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, user_id, name, target_url, requirement, status, created_at, updated_at) VALUES (?,?,?,?,?,'draft',?,?)",
            (pid, user_id, name or 'Untitled', target_url, requirement, now, now))
    return {"id": pid, "user_id": user_id, "name": name, "target_url": target_url,
            "requirement": requirement, "status": "draft", "created_at": now, "updated_at": now}


def _get_project(pid: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    return dict(row) if row else None


def _list_projects(user_id: str) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM projects WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def _update_project_status(pid: str, status: str) -> dict | None:
    now = _now()
    with _get_conn() as conn:
        conn.execute("UPDATE projects SET status=?, updated_at=? WHERE id=?", (status, now, pid))
    return _get_project(pid)


async def create_project(user_id: str, target_url: str, requirement: str, name: str = "Untitled") -> dict:
    return await _run_async(_create_project, user_id, target_url, requirement, name)


async def get_project(pid: str) -> dict | None:
    return await _run_async(_get_project, pid)


async def list_projects(user_id: str) -> list[dict]:
    return await _run_async(_list_projects, user_id)


async def update_project_status(pid: str, status: str) -> dict | None:
    return await _run_async(_update_project_status, pid, status)


# ============================================================
# Script version operations
# ============================================================

def _create_version(project_id: str, version_number: int, script_code: str,
                    source: str = "generation", parent_id: str | None = None) -> dict:
    vid = _uid()
    now = _now()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO script_versions (id, project_id, version_number, script_code, source, parent_version_id, created_at) VALUES (?,?,?,?,?,?,?)",
            (vid, project_id, version_number, script_code, source, parent_id, now))
    return {"id": vid, "project_id": project_id, "version_number": version_number,
            "script_code": script_code, "source": source, "parent_version_id": parent_id, "created_at": now}


def _get_latest_version(project_id: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM script_versions WHERE project_id=? ORDER BY version_number DESC LIMIT 1",
            (project_id,)).fetchone()
    return dict(row) if row else None


def _list_versions(project_id: str) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM script_versions WHERE project_id=? ORDER BY version_number DESC",
            (project_id,)).fetchall()
    return [dict(r) for r in rows]


async def create_script_version(project_id: str, version_number: int, script_code: str,
                                source: str = "generation", parent_id: str | None = None) -> dict:
    return await _run_async(_create_version, project_id, version_number, script_code, source, parent_id)


async def get_latest_script_version(project_id: str) -> dict | None:
    return await _run_async(_get_latest_version, project_id)


async def list_script_versions(project_id: str) -> list[dict]:
    return await _run_async(_list_versions, project_id)


# ============================================================
# Execution operations
# ============================================================

def _create_execution(version_id: str, exec_type: str = "preview", status: str = "pending",
                      error_log: str | None = None, result_preview: dict | None = None,
                      result_file_path: str | None = None) -> dict:
    eid = _uid()
    now = _now()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO executions (id, script_version_id, execution_type, status, error_log, result_preview, result_file_path, executed_at) VALUES (?,?,?,?,?,?,?,?)",
            (eid, version_id, exec_type, status, error_log,
             json.dumps(result_preview, ensure_ascii=False) if result_preview else None,
             result_file_path, now))
    return {"id": eid, "script_version_id": version_id, "execution_type": exec_type,
            "status": status, "error_log": error_log, "result_file_path": result_file_path, "executed_at": now}


def _get_latest_execution(version_id: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM executions WHERE script_version_id=? ORDER BY executed_at DESC LIMIT 1",
            (version_id,)).fetchone()
    if row:
        d = dict(row)
        if d.get("result_preview") and isinstance(d["result_preview"], str):
            d["result_preview"] = json.loads(d["result_preview"])
        return d
    return None


async def create_execution(version_id: str, exec_type: str = "preview", status: str = "pending",
                           error_log: str | None = None, result_preview: dict | None = None,
                           result_file_path: str | None = None) -> dict:
    return await _run_async(_create_execution, version_id, exec_type, status, error_log, result_preview, result_file_path)


async def get_latest_execution(version_id: str) -> dict | None:
    return await _run_async(_get_latest_execution, version_id)


# ============================================================
# Feedback operations
# ============================================================

def _create_feedback(version_id: str, feedback: str) -> dict:
    fid = _uid()
    now = _now()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO feedbacks (id, script_version_id, user_feedback, created_at) VALUES (?,?,?,?)",
            (fid, version_id, feedback, now))
    return {"id": fid, "script_version_id": version_id, "user_feedback": feedback, "created_at": now}


async def create_feedback(version_id: str, feedback: str) -> dict:
    return await _run_async(_create_feedback, version_id, feedback)


# ============================================================
# Script cache（脚本缓存复用 - 相同需求直接调，不调大模型）
# ============================================================

def normalize_requirement(requirement: str) -> str:
    """规范化需求文本，作为缓存 key。

    去除空格、标点、统一小写，让「提取书籍」和「提取 书籍」命中同一个缓存。
    """
    import re
    q = requirement.lower().strip()
    # 去除所有标点和空白
    q = re.sub(r'[\s\W]+', '', q)
    return q


# ============================================================
# Script library（脚本库 - 运行完整版成功后保存，复用脚本 + 登录态刷新）
# ============================================================

def _save_library_script(req_key: str, requirement: str, url: str,
                         domain: str | None, needs_login: bool, script_path: str) -> dict:
    now = _now()
    with _get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM script_library WHERE req_key=?", (req_key,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE script_library SET requirement=?, url=?, domain=?, needs_login=?, script_path=?, updated_at=? WHERE req_key=?",
                (requirement, url, domain, 1 if needs_login else 0, script_path, now, req_key))
        else:
            conn.execute(
                "INSERT INTO script_library (id, req_key, requirement, url, domain, needs_login, script_path, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (_uid(), req_key, requirement, url, domain, 1 if needs_login else 0, script_path, now, now))
    return {"req_key": req_key, "saved": True}


def _get_library_script(req_key: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM script_library WHERE req_key=?", (req_key,)).fetchone()
    return dict(row) if row else None


async def save_library_script(req_key: str, requirement: str, url: str,
                              domain: str | None, needs_login: bool, script_path: str) -> dict:
    return await _run_async(_save_library_script, req_key, requirement, url, domain, needs_login, script_path)


async def get_library_script(req_key: str) -> dict | None:
    return await _run_async(_get_library_script, req_key)


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
