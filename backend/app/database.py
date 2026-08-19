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

def _get_conn():
    """返回数据库连接：配置 DATABASE_URL=postgresql://... 时用 PostgreSQL 兼容连接，
    否则 SQLite（默认，保持现状）。"""
    from app.config import get_settings
    from app.db_adapter import PgConn, is_postgres_url
    url = (get_settings().database_url or "").strip()
    if is_postgres_url(url):
        return PgConn(url)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")  # 高并发写不抛 locked，等待 10s
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_columns(conn, table: str) -> set[str]:
    """统一取表列名：sqlite 用 PRAGMA table_info，pg 用 information_schema。"""
    from app.db_adapter import PgConn
    if isinstance(conn, PgConn):
        return {c.lower() for c in conn.table_columns(table)}
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


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
                next_run_at REAL,
                image_paths TEXT DEFAULT '',        -- JSON 数组（上传图片路径，随任务持久化）
                data_paths TEXT DEFAULT '',         -- JSON 数组（上传数据文件路径）
                retry_count INTEGER DEFAULT 0,      -- 崩溃恢复重试次数（分布式 worker）
                steps TEXT DEFAULT '',              -- JSON 数组：执行过程日志（跨实例可见/崩溃不丢）
                progress INTEGER DEFAULT 0          -- 进度 0-100（前端进度条）
            );

            -- 站内通知（定时提醒 / 监控触发 / 任务完成提醒）
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT DEFAULT '',
                created_at REAL NOT NULL,
                read INTEGER DEFAULT 0
            );

            -- 定时提醒项（"每天8点提醒我打卡" → time='08:00', text='打卡'）
            CREATE TABLE IF NOT EXISTS reminders (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                time TEXT NOT NULL,                 -- HH:MM
                text TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                source_task TEXT DEFAULT '',        -- 来源任务 id（可空）
                created_at REAL NOT NULL
            );

            -- 监控任务（软件/窗口/屏幕变化，条件满足触发）
            CREATE TABLE IF NOT EXISTS monitors (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                monitor_type TEXT NOT NULL,         -- window | screen
                keywords TEXT DEFAULT '',           -- 窗口标题关键词（逗号分隔）
                condition TEXT DEFAULT '',          -- 画面变化条件（screen）
                action_requirement TEXT DEFAULT '', -- 触发后要执行的任务需求（可空=仅提醒）
                enabled INTEGER DEFAULT 1,
                check_interval INTEGER DEFAULT 60,  -- 检查间隔（秒）
                last_checked_at REAL,
                last_state TEXT DEFAULT '',         -- 屏幕哈希等状态
                source_task TEXT DEFAULT '',
                created_at REAL NOT NULL
            );

            -- 用户长期记忆（记住用户偏好/习惯，任务提交时注入上下文）
            CREATE TABLE IF NOT EXISTS user_memory (
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,                 -- preference(偏好) | fact(事实) | habit(习惯)
                content TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (user_id, content)
            );

            -- Guest user created by API on first request if needed
        """)
    # 老库迁移：补 image_paths/data_paths/retry_count/steps/progress 列（不存在才加）
    with _get_conn() as conn:
        cols = _table_columns(conn, "mini_tasks")
        for col in ("image_paths", "data_paths"):
            if col not in cols:
                conn.execute(f"ALTER TABLE mini_tasks ADD COLUMN {col} TEXT DEFAULT ''")
        if "retry_count" not in cols:
            conn.execute("ALTER TABLE mini_tasks ADD COLUMN retry_count INTEGER DEFAULT 0")
        if "steps" not in cols:
            conn.execute("ALTER TABLE mini_tasks ADD COLUMN steps TEXT DEFAULT ''")
        if "progress" not in cols:
            conn.execute("ALTER TABLE mini_tasks ADD COLUMN progress INTEGER DEFAULT 0")


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
    # INSERT ... ON CONFLICT DO UPDATE：不用 INSERT OR REPLACE（REPLACE 会先删旧行，
    # 把 schedule_type/schedule_value/enabled/last_run_at/next_run_at 等列静默清空）。
    # 新记录同时持久化 image_paths/data_paths（JSON）与 schedule 列（提交时直接带定时）。
    # ON CONFLICT 分支不更新 schedule 列 → iterate/重存不会覆盖已有调度配置。
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO mini_tasks (id, user_id, requirement, url, status, message, created_at, updated_at,
                                       result, error, image_paths, data_paths, steps,
                                       schedule_type, schedule_value, enabled, next_run_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 requirement=excluded.requirement, url=excluded.url,
                 status=excluded.status, message=excluded.message, updated_at=excluded.updated_at,
                 result=excluded.result, error=excluded.error,
                 image_paths=excluded.image_paths, data_paths=excluded.data_paths, steps=excluded.steps""",
            (
                record["id"], record.get("user_id", ""), record["requirement"], record.get("url", ""),
                record.get("status", "queued"), record.get("message", ""),
                record.get("created_at", 0), time.time(),
                json.dumps(record.get("result"), ensure_ascii=False) if record.get("result") else None,
                record.get("error"),
                json.dumps(record.get("image_paths") or [], ensure_ascii=False),
                json.dumps(record.get("data_paths") or [], ensure_ascii=False),
                json.dumps(record.get("steps") or [], ensure_ascii=False),
                record.get("schedule_type", ""), record.get("schedule_value", ""),
                1 if record.get("enabled") else 0, record.get("next_run_at"),
            ),
        )


def _decode_json_list(v) -> list:
    if not v:
        return []
    try:
        val = json.loads(v)
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# mini_tasks 允许动态更新的列白名单（防 SQL 注入：字段名只能来自白名单）
_ALLOWED_MINI_UPDATE_FIELDS = {
    "status", "message", "result", "error", "progress", "url", "requirement",
    "updated_at", "image_paths", "data_paths", "steps",
}


def _update_mini_task(task_id: str, **fields) -> None:
    # 只保留白名单内的列，键名不可由外部输入控制
    fields = {k: v for k, v in fields.items() if k in _ALLOWED_MINI_UPDATE_FIELDS}
    if not fields:
        return
    import time as _t
    # image_paths/data_paths/steps 传 list 时序列化为 JSON
    for k in ("image_paths", "data_paths", "steps"):
        if k in fields and isinstance(fields[k], list):
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
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
    d["image_paths"] = _decode_json_list(d.get("image_paths"))
    d["data_paths"] = _decode_json_list(d.get("data_paths"))
    d["steps"] = _decode_json_list(d.get("steps"))
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
        d["steps"] = _decode_json_list(d.get("steps"))
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
            cols = _table_columns(conn, "mini_tasks")
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


def _claim_mini_run(task_id: str, now: float, next_run_at: float | None) -> bool:
    """原子抢占到期定时任务：仅当 next_run_at 仍是到期值（<=now）时才更新并返回 True。

    用于多 worker / 任务运行超时重叠时防止同一任务被重复提交。
    """
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE mini_tasks SET last_run_at=?, next_run_at=?, updated_at=? "
            "WHERE id=? AND enabled=1 AND next_run_at IS NOT NULL AND next_run_at <= ?",
            (now, next_run_at, time.time(), task_id, now),
        )
        return cur.rowcount > 0


async def ensure_mini_schedule_columns() -> None:
    return await _run_async(_ensure_mini_schedule_columns)


async def get_due_mini_tasks(now: float) -> list[dict]:
    return await _run_async(_get_due_mini_tasks, now)


async def set_mini_schedule(task_id: str, schedule_type: str, schedule_value: str, enabled: bool, next_run_at: float | None) -> None:
    return await _run_async(_set_mini_schedule, task_id, schedule_type, schedule_value, enabled, next_run_at)


# ---- 分布式崩溃恢复（reaper） ----

def _get_stale_mini_tasks(older_than: float) -> list[dict]:
    """查疑似失联的任务：status 仍为 queued/running 且 updated_at 很久未变。

    分布式 worker 崩溃后任务会卡在 queued/running（租约过期但无人标记），
    reaper 据此扫描候选，再结合 Redis 租约/队列判断是否真的失联。
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM mini_tasks WHERE status IN ('queued','running') AND updated_at IS NOT NULL AND updated_at <= ?",
            (older_than,),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        if d.get("result"):
            try:
                d["result"] = json.loads(d["result"])
            except (json.JSONDecodeError, TypeError):
                d["result"] = None
        d["image_paths"] = _decode_json_list(d.get("image_paths"))
        d["data_paths"] = _decode_json_list(d.get("data_paths"))
        d["steps"] = _decode_json_list(d.get("steps"))
        out.append(d)
    return out


def _bump_retry_count(task_id: str) -> int:
    """重试计数 +1，返回新的计数。"""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE mini_tasks SET retry_count = COALESCE(retry_count,0) + 1, updated_at=? WHERE id=?",
            (time.time(), task_id),
        )
        row = conn.execute("SELECT retry_count FROM mini_tasks WHERE id=?", (task_id,)).fetchone()
    return int(row["retry_count"]) if row else 0


def _mark_task_dead(task_id: str, reason: str) -> None:
    """死信：重试超限，标记失败不再重试（用户可见错误原因）。"""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE mini_tasks SET status='failed', error=?, message=?, updated_at=? WHERE id=?",
            (reason, "执行多次失败，已停止重试", time.time(), task_id),
        )


async def get_stale_mini_tasks(older_than: float) -> list[dict]:
    return await _run_async(_get_stale_mini_tasks, older_than)


async def bump_retry_count(task_id: str) -> int:
    return await _run_async(_bump_retry_count, task_id)


async def mark_task_dead(task_id: str, reason: str) -> None:
    return await _run_async(_mark_task_dead, task_id, reason)


# ---- 结果缓存（省 LLM 成本：同用户同需求复用最近成功结果） ----

def _find_cached_result(user_id: str, requirement: str, url: str, within_seconds: float) -> dict | None:
    """查同一用户最近一次完全相同的成功任务（含结果）。

    仅当 status='done'、result 非空、updated_at 在窗口内才命中。
    返回任务记录（含解析后的 result），未命中返回 None。
    """
    if not user_id or not requirement:
        return None
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM mini_tasks WHERE user_id=? AND requirement=? AND url=? "
            "AND status='done' AND result IS NOT NULL AND result != '' "
            "AND updated_at IS NOT NULL AND updated_at >= ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (user_id, requirement, url or "", time.time() - within_seconds),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["result"] = json.loads(d["result"])
    except (json.JSONDecodeError, TypeError):
        return None
    d["image_paths"] = _decode_json_list(d.get("image_paths"))
    d["data_paths"] = _decode_json_list(d.get("data_paths"))
    return d


async def find_cached_result(user_id: str, requirement: str, url: str, within_seconds: float) -> dict | None:
    return await _run_async(_find_cached_result, user_id, requirement, url, within_seconds)


# ---- 可观测性：任务统计 ----

def _task_stats() -> dict:
    """任务执行统计（运维/监控）：总量、状态分布、成功率、平均耗时、今日新增。"""
    with _get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM mini_tasks").fetchone()["c"]
        today = conn.execute(
            "SELECT COUNT(*) AS c FROM mini_tasks WHERE created_at >= ?",
            (time.time() - 86400,),
        ).fetchone()["c"]
        by_status = {
            r["status"]: r["c"] for r in conn.execute(
                "SELECT status, COUNT(*) AS c FROM mini_tasks GROUP BY status").fetchall()
        }
        done = conn.execute(
            "SELECT COUNT(*) AS c FROM mini_tasks WHERE status='done' AND updated_at IS NOT NULL AND updated_at >= ?",
            (time.time() - 86400,),
        ).fetchone()["c"]
        failed_today = conn.execute(
            "SELECT COUNT(*) AS c FROM mini_tasks WHERE status='failed' AND updated_at IS NOT NULL AND updated_at >= ?",
            (time.time() - 86400,),
        ).fetchone()["c"]
        avg_row = conn.execute(
            "SELECT AVG(updated_at - created_at) AS a FROM mini_tasks "
            "WHERE status='done' AND updated_at IS NOT NULL AND created_at IS NOT NULL "
            "AND updated_at >= created_at AND updated_at >= ?",
            (time.time() - 86400,),
        ).fetchone()
    finished_today = done + failed_today
    return {
        "total": total,
        "today": today,
        "by_status": by_status,
        "success_rate_today": round(done / finished_today, 3) if finished_today else None,
        "done_today": done,
        "failed_today": failed_today,
        "avg_elapsed_today": round(avg_row["a"], 1) if avg_row and avg_row["a"] is not None else None,
    }


async def task_stats() -> dict:
    return await _run_async(_task_stats)


async def update_mini_run(task_id: str, last_run_at: float, next_run_at: float | None) -> None:
    return await _run_async(_update_mini_run, task_id, last_run_at, next_run_at)


async def claim_mini_run(task_id: str, now: float, next_run_at: float | None) -> bool:
    return await _run_async(_claim_mini_run, task_id, now, next_run_at)


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


def _add_credits(user_id: str, amount: int = 1) -> None:
    """补回积分（自动化创建失败退款等场景）。"""
    with _get_conn() as conn:
        conn.execute("UPDATE users SET credits = credits + ? WHERE id=?", (amount, user_id))


async def get_credits(user_id: str) -> int:
    return await _run_async(_get_credits, user_id)


async def try_decrement_credits(user_id: str, amount: int = 1) -> bool:
    return await _run_async(_try_decrement_credits, user_id, amount)


async def add_credits(user_id: str, amount: int = 1) -> None:
    return await _run_async(_add_credits, user_id, amount)


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
# Notifications（站内消息：定时提醒/监控触发/任务完成）
# ============================================================

def _add_notification(user_id: str, title: str, content: str = "") -> None:
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO notifications (id, user_id, title, content, created_at) VALUES (?,?,?,?,?)",
            (_uid(), user_id, title, content, time.time()),
        )


def _list_notifications(user_id: str, limit: int = 20) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE user_id=? OR user_id='system' "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _unread_notification_count(user_id: str) -> int:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM notifications WHERE (user_id=? OR user_id='system') AND read=0",
            (user_id,),
        ).fetchone()
    return int(row["c"]) if row else 0


def _mark_notifications_read(user_id: str, ids: list[str] | None = None) -> None:
    with _get_conn() as conn:
        if ids:
            conn.executemany(
                "UPDATE notifications SET read=1 WHERE id=? AND user_id=?", [(i, user_id) for i in ids]
            )
        else:
            conn.execute("UPDATE notifications SET read=1 WHERE user_id=?", (user_id,))


async def add_notification(user_id: str, title: str, content: str = "") -> None:
    return await _run_async(_add_notification, user_id, title, content)


async def list_notifications(user_id: str, limit: int = 20) -> list[dict]:
    return await _run_async(_list_notifications, user_id, limit)


async def unread_notification_count(user_id: str) -> int:
    return await _run_async(_unread_notification_count, user_id)


async def mark_notifications_read(user_id: str, ids: list[str] | None = None) -> None:
    return await _run_async(_mark_notifications_read, user_id, ids)


# ============================================================
# User Memory（用户长期记忆：偏好/事实/习惯）
# ============================================================

def _remember(user_id: str, kind: str, content: str, max_items: int = 50) -> None:
    """记住一条记忆（同内容覆盖更新时间）；超上限删最旧的。"""
    content = (content or "").strip()
    if not content or not kind:
        return
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO user_memory (user_id, kind, content, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, content) DO UPDATE SET updated_at=excluded.updated_at",
            (user_id, kind, content, time.time()),
        )
        # 上限控制：保留最近 max_items 条
        rows = conn.execute(
            "SELECT content FROM user_memory WHERE user_id=? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
        if len(rows) > max_items:
            for r in rows[max_items:]:
                conn.execute("DELETE FROM user_memory WHERE user_id=? AND content=?", (user_id, r["content"]))


def _get_memory(user_id: str, limit: int = 20) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT kind, content, updated_at FROM user_memory WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _forget(user_id: str, content: str | None = None) -> None:
    """删除记忆：content 给定时删单条，否则清空该用户全部。"""
    with _get_conn() as conn:
        if content:
            conn.execute("DELETE FROM user_memory WHERE user_id=? AND content=?", (user_id, content))
        else:
            conn.execute("DELETE FROM user_memory WHERE user_id=?", (user_id,))


async def remember(user_id: str, kind: str, content: str, max_items: int = 50) -> None:
    return await _run_async(_remember, user_id, kind, content, max_items)


async def get_memory(user_id: str, limit: int = 20) -> list[dict]:
    return await _run_async(_get_memory, user_id, limit)


async def forget(user_id: str, content: str | None = None) -> None:
    return await _run_async(_forget, user_id, content)


# ============================================================
# Reminders（定时提醒项）
# ============================================================

def _add_reminder(user_id: str, time_str: str, text: str, source_task: str = "") -> None:
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO reminders (id, user_id, time, text, enabled, source_task, created_at) VALUES (?,?,?,?,1,?,?)",
            (_uid(), user_id, time_str, text, source_task, time.time()),
        )


def _list_reminders(user_id: str, enabled_only: bool = False) -> list[dict]:
    with _get_conn() as conn:
        if enabled_only:
            if user_id and user_id != "*":
                rows = conn.execute(
                    "SELECT * FROM reminders WHERE user_id=? AND enabled=1 ORDER BY time", (user_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM reminders WHERE enabled=1 ORDER BY time"
                ).fetchall()
        else:
            if user_id and user_id != "*":
                rows = conn.execute(
                    "SELECT * FROM reminders WHERE user_id=? ORDER BY created_at DESC", (user_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM reminders ORDER BY created_at DESC"
                ).fetchall()
    return [dict(r) for r in rows]


def _delete_reminder(user_id: str, reminder_id: str) -> bool:
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM reminders WHERE id=? AND user_id=?", (reminder_id, user_id))
        return cur.rowcount > 0


def _toggle_reminder(user_id: str, reminder_id: str) -> bool | None:
    """切换提醒启用状态，返回新状态（True/False）；不存在返回 None。"""
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE reminders SET enabled = 1 - enabled WHERE id=? AND user_id=?",
            (reminder_id, user_id),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT enabled FROM reminders WHERE id=?", (reminder_id,)).fetchone()
        return bool(row["enabled"]) if row else None


def _update_reminder(user_id: str, reminder_id: str, time_str: str | None = None,
                     text: str | None = None) -> bool:
    """更新提醒的时间/内容（None 的字段保持不变）。返回是否更新成功。"""
    sets, vals = [], []
    if time_str is not None:
        sets.append("time=?")
        vals.append(time_str)
    if text is not None:
        sets.append("text=?")
        vals.append(text)
    if not sets:
        return True
    vals += [reminder_id, user_id]
    with _get_conn() as conn:
        cur = conn.execute(f"UPDATE reminders SET {', '.join(sets)} WHERE id=? AND user_id=?", vals)
        return cur.rowcount > 0


async def add_reminder(user_id: str, time_str: str, text: str, source_task: str = "") -> None:
    return await _run_async(_add_reminder, user_id, time_str, text, source_task)


async def list_reminders(user_id: str, enabled_only: bool = False) -> list[dict]:
    return await _run_async(_list_reminders, user_id, enabled_only)


async def delete_reminder(user_id: str, reminder_id: str) -> bool:
    return await _run_async(_delete_reminder, user_id, reminder_id)


async def toggle_reminder(user_id: str, reminder_id: str) -> bool | None:
    return await _run_async(_toggle_reminder, user_id, reminder_id)


# ============================================================
# Monitors（监控任务：软件/窗口/屏幕变化）
# ============================================================

def _add_monitor(user_id: str, monitor_type: str, keywords: str = "", condition: str = "",
                 action_requirement: str = "", check_interval: int = 60, source_task: str = "") -> str:
    mid = _uid()
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO monitors (id, user_id, monitor_type, keywords, condition, action_requirement,
                                     enabled, check_interval, source_task, created_at)
               VALUES (?,?,?,?,?,?,1,?,?,?)""",
            (mid, user_id, monitor_type, keywords, condition, action_requirement,
             max(5, int(check_interval)), source_task, time.time()),
        )
    return mid


def _list_monitors(user_id: str, enabled_only: bool = False) -> list[dict]:
    with _get_conn() as conn:
        if enabled_only:
            if user_id and user_id != "*":
                rows = conn.execute(
                    "SELECT * FROM monitors WHERE user_id=? AND enabled=1 ORDER BY created_at DESC", (user_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM monitors WHERE enabled=1 ORDER BY created_at DESC"
                ).fetchall()
        else:
            if user_id and user_id != "*":
                rows = conn.execute(
                    "SELECT * FROM monitors WHERE user_id=? ORDER BY created_at DESC", (user_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM monitors ORDER BY created_at DESC"
                ).fetchall()
    return [dict(r) for r in rows]


def _update_monitor_state(monitor_id: str, last_checked_at: float, last_state: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE monitors SET last_checked_at=?, last_state=? WHERE id=?",
            (last_checked_at, last_state, monitor_id),
        )


def _delete_monitor(user_id: str, monitor_id: str) -> bool:
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM monitors WHERE id=? AND user_id=?", (monitor_id, user_id))
        return cur.rowcount > 0


def _toggle_monitor(user_id: str, monitor_id: str) -> bool | None:
    """切换监控启用状态，返回新状态（True/False）；不存在返回 None。"""
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE monitors SET enabled = 1 - enabled WHERE id=? AND user_id=?",
            (monitor_id, user_id),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT enabled FROM monitors WHERE id=?", (monitor_id,)).fetchone()
        return bool(row["enabled"]) if row else None


def _update_monitor(user_id: str, monitor_id: str, keywords: str | None = None,
                    condition: str | None = None, action_requirement: str | None = None,
                    check_interval: int | None = None, monitor_type: str | None = None) -> bool:
    """更新监控配置（None 的字段保持不变）。返回是否更新成功。"""
    sets, vals = [], []
    if keywords is not None:
        sets.append("keywords=?")
        vals.append(keywords)
    if condition is not None:
        sets.append("condition=?")
        vals.append(condition)
    if action_requirement is not None:
        sets.append("action_requirement=?")
        vals.append(action_requirement)
    if check_interval is not None:
        sets.append("check_interval=?")
        vals.append(max(5, min(int(check_interval), 3600)))
    if monitor_type is not None:
        sets.append("monitor_type=?")
        vals.append(monitor_type)
    if not sets:
        return True
    vals += [monitor_id, user_id]
    with _get_conn() as conn:
        cur = conn.execute(f"UPDATE monitors SET {', '.join(sets)} WHERE id=? AND user_id=?", vals)
        return cur.rowcount > 0


async def add_monitor(user_id: str, monitor_type: str, keywords: str = "", condition: str = "",
                      action_requirement: str = "", check_interval: int = 60, source_task: str = "") -> str:
    return await _run_async(_add_monitor, user_id, monitor_type, keywords, condition,
                            action_requirement, check_interval, source_task)


async def list_monitors(user_id: str, enabled_only: bool = False) -> list[dict]:
    return await _run_async(_list_monitors, user_id, enabled_only)


async def update_monitor_state(monitor_id: str, last_checked_at: float, last_state: str) -> None:
    return await _run_async(_update_monitor_state, monitor_id, last_checked_at, last_state)


async def delete_monitor(user_id: str, monitor_id: str) -> bool:
    return await _run_async(_delete_monitor, user_id, monitor_id)


async def toggle_monitor(user_id: str, monitor_id: str) -> bool | None:
    return await _run_async(_toggle_monitor, user_id, monitor_id)


async def update_monitor(user_id: str, monitor_id: str, keywords: str | None = None,
                         condition: str | None = None, action_requirement: str | None = None,
                         check_interval: int | None = None, monitor_type: str | None = None) -> bool:
    return await _run_async(_update_monitor, user_id, monitor_id, keywords, condition,
                            action_requirement, check_interval, monitor_type)


async def update_reminder(user_id: str, reminder_id: str, time_str: str | None = None,
                          text: str | None = None) -> bool:
    return await _run_async(_update_reminder, user_id, reminder_id, time_str, text)


# ============================================================
# Init
# ============================================================

async def init_db():
    await _run_async(_init_db)
    print(f"[DB] SQLite initialized at {DB_PATH}")
