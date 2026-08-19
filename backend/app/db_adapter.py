# -*- coding: utf-8 -*-
from __future__ import annotations

"""PostgreSQL 兼容适配层（云架构可选数据库）。

默认 SQLite（零依赖，单机/低并发够用）；配置 DATABASE_URL=postgresql://... 时
走 PostgreSQL（多实例高并发推荐）。本模块把 psycopg2 包装成 sqlite3 风格的
连接接口，业务 SQL 完全不用改：

- 占位符：业务 SQL 用 `?`（sqlite 风格），这里在 execute 时自动转 `%s`
- 行访问：sqlite3.Row 支持 row["col"] / dict(row)，pg 用 RealDictCursor 对齐
- executescript：sqlite 支持整段执行，pg 按分号拆分逐条执行
- PRAGMA 语句：pg 没有，执行时静默跳过（列检查走 information_schema）
- with conn 语义：__enter__ 返回自身，__exit__ 提交/回滚（与 sqlite3 一致）

注意：业务 SQL 里不能有**字面** `?`（如 JSON 操作符），否则会被误转——
本项目所有 SQL 均只用 `?` 做占位符，已确认无冲突。
"""

import re
import threading

_thread_local = threading.local()

# sqlite3 占位符 -> psycopg2 占位符
_Q_RE = re.compile(r"\?")

# 建表脚本里 sqlite 专属、pg 不需要/不支持的语句（执行时忽略）
_SQLITE_ONLY_PREFIXES = (
    "PRAGMA",
)


class PgRow(dict):
    """psycopg2 RealDictRow 的轻量替代：支持 .keys() 与下标（dict(row) 兼容）。"""
    pass


class PgCursor:
    """包装 psycopg2 cursor：适配 fetchone/fetchall/rowcount。"""
    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        row = self._cur.fetchone()
        return dict(row) if row else None

    def fetchall(self):
        return [dict(r) for r in self._cur.fetchall()]

    @property
    def rowcount(self):
        return self._cur.rowcount

    def __getattr__(self, item):
        return getattr(self._cur, item)


class PgConn:
    """sqlite3.Connection 风格的 PostgreSQL 连接包装。"""

    _pool = None
    _pool_lock = threading.Lock()

    def __init__(self, dsn: str):
        import psycopg2
        import psycopg2.extras
        self._dsn = dsn
        self._cur_factory = psycopg2.extras.RealDictCursor
        self._conn = _PgPool.get_connection(dsn)
        self._conn.autocommit = False

    # ---- 连接生命周期 ----
    def close(self):
        """归还连接到池（不是真关闭）。"""
        _PgPool.return_connection(self._dsn, self._conn)
        self._conn = None

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        except Exception:
            pass
        self.close()
        return False

    # ---- 执行 ----
    def _adapt(self, sql: str) -> str | None:
        """适配单条 SQL：忽略 sqlite 专属语句，转占位符。返回 None 表示跳过。"""
        stripped = sql.strip()
        if not stripped:
            return None
        if stripped.upper().startswith(_SQLITE_ONLY_PREFIXES):
            return None
        return _Q_RE.sub("%s", sql)

    def execute(self, sql, params=None):
        adapted = self._adapt(sql)
        if adapted is None:
            # 返回一个"空游标"，fetchone/fetchall 返回空，保持调用方不崩
            return _EmptyCursor()
        cur = self._conn.cursor(cursor_factory=self._cur_factory)
        if params is None:
            cur.execute(adapted)
        else:
            cur.execute(adapted, tuple(params) if not isinstance(params, tuple) else params)
        return PgCursor(cur)

    def executemany(self, sql, seq_of_params):
        adapted = self._adapt(sql)
        if adapted is None:
            return _EmptyCursor()
        cur = self._conn.cursor(cursor_factory=self._cur_factory)
        cur.executemany(adapted, [tuple(p) if not isinstance(p, tuple) else p for p in seq_of_params])
        return PgCursor(cur)

    def executescript(self, script: str):
        """sqlite 的 executescript：按分号拆分逐条执行（忽略注释/PRAGMA）。"""
        for stmt in _split_sql_statements(script):
            adapted = self._adapt(stmt)
            if adapted is None:
                continue
            cur = self._conn.cursor(cursor_factory=self._cur_factory)
            try:
                cur.execute(adapted)
            finally:
                cur.close()

    # ---- 表列信息（替代 PRAGMA table_info）----
    def table_columns(self, table: str) -> list[str]:
        cur = self._conn.cursor(cursor_factory=self._cur_factory)
        try:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (table,),
            )
            return [r["column_name"] for r in cur.fetchall()]
        finally:
            cur.close()


class _PgPool:
    """psycopg2 连接池（ThreadedConnectionPool）：避免每次操作新建连接。

    按 dsn 分池；池空时新建（默认 maxconn=10）。连接归还时自动回滚
    未提交事务（防脏状态污染下一使用方）。
    """

    _pools: dict[str, object] = {}
    _lock = threading.Lock()

    @classmethod
    def get_connection(cls, dsn: str):
        import psycopg2.pool
        with cls._lock:
            pool = cls._pools.get(dsn)
            if pool is None:
                pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn)
                cls._pools[dsn] = pool
            try:
                return pool.getconn()
            except Exception:
                # 池满：临时建一个直连（极端并发兜底，用完即关）
                import psycopg2
                return psycopg2.connect(dsn)

    @classmethod
    def return_connection(cls, dsn: str, conn) -> None:
        try:
            conn.rollback()  # 清掉未提交事务，防脏状态
        except Exception:
            pass
        try:
            pool = cls._pools.get(dsn)
            if pool is not None:
                pool.putconn(conn)
                return
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


class _EmptyCursor:
    """PRAGMA 等被跳过语句的返回：空结果，调用方 fetchone/fetchall 拿到空。"""
    rowcount = 0

    def fetchone(self):
        return None

    def fetchall(self):
        return []


def _split_sql_statements(script: str) -> list[str]:
    """简单按分号拆分 SQL 脚本（本项目建表脚本无存储过程/触发器，够用）。"""
    stmts = []
    buf = []
    for line in script.splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        buf.append(line)
        if line.endswith(";"):
            stmts.append("\n".join(buf))
            buf = []
    if buf:
        stmts.append("\n".join(buf))
    return stmts


def is_postgres_url(url: str) -> bool:
    return bool(url) and url.strip().lower().startswith(("postgres://", "postgresql://"))
