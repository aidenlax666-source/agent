# -*- coding: utf-8 -*-
"""PostgreSQL 适配层测试：SQL 转换（?→%s）、脚本拆分、PRAGMA 跳过、URL 识别。

不真连 PostgreSQL（沙箱无 pg 服务）：验证纯函数转换逻辑 + 用假 cursor 模拟
psycopg2 的行为，保证业务 SQL 在 pg 模式下语法正确。
"""
import pytest

from app.db_adapter import PgConn, _split_sql_statements, is_postgres_url, _Q_RE


# ---- URL 识别 ----

def test_is_postgres_url():
    assert is_postgres_url("postgresql://user:pass@host:5432/db") is True
    assert is_postgres_url("postgres://user@host/db") is True
    assert is_postgres_url("") is False
    assert is_postgres_url("sqlite:///x.db") is False
    assert is_postgres_url("  postgresql://a:b@c/d  ") is True  # 带空白


# ---- SQL 语句拆分（executescript 用） ----

def test_split_statements_basic():
    script = """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL
        );

        -- 注释行
        CREATE TABLE IF NOT EXISTS audit_logs (
            id TEXT PRIMARY KEY
        );
    """
    stmts = _split_sql_statements(script)
    assert len(stmts) == 2
    assert "users" in stmts[0]
    assert "audit_logs" in stmts[1]


def test_split_statements_drops_comments():
    script = "-- 开头注释\nCREATE TABLE t (id TEXT);\n"
    stmts = _split_sql_statements(script)
    assert len(stmts) == 1
    assert "CREATE TABLE" in stmts[0]


# ---- SQL 占位符转换 ----

def test_q_to_percent_s():
    assert PgConn._adapt(None, "SELECT * FROM users WHERE id=?") == "SELECT * FROM users WHERE id=%s"
    assert PgConn._adapt(None, "UPDATE t SET a=?, b=? WHERE id=?") == "UPDATE t SET a=%s, b=%s WHERE id=%s"
    assert PgConn._adapt(None, "SELECT COUNT(*) AS c FROM mini_tasks") == "SELECT COUNT(*) AS c FROM mini_tasks"  # 无占位符


def test_pragma_skipped():
    """PRAGMA 语句（sqlite 专属）在 pg 模式下返回 None（跳过执行）。"""
    assert PgConn._adapt(None, "PRAGMA journal_mode=WAL") is None
    assert PgConn._adapt(None, "PRAGMA busy_timeout=10000") is None
    assert PgConn._adapt(None, "PRAGMA foreign_keys=ON") is None
    assert PgConn._adapt(None, "  pragma table_info(x)  ") is None  # 大小写不敏感


def test_empty_or_comment_skipped():
    assert PgConn._adapt(None, "") is None
    assert PgConn._adapt(None, "   ") is None


# ---- 用假 psycopg2 验证执行路径（不真连 pg） ----

class FakePgCursor:
    """模拟 psycopg2 cursor：记录 SQL 与参数。"""
    def __init__(self, factory):
        self._factory = factory
        self.executed: list = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return self

    def executemany(self, sql, seq):
        self.executed.append((sql, list(seq)))
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass


class FakePsycopg2:
    """伪造 psycopg2 模块：PgConn 导入它做连接。"""
    class extras:
        class RealDictCursor:
            pass

    def connect(self, dsn):
        return FakePgConnection(dsn)


class FakePgConnection:
    def __init__(self, dsn):
        self.dsn = dsn
        self.autocommit = None
        self._cursors = []

    def cursor(self, cursor_factory=None):
        c = FakePgCursor(cursor_factory)
        self._cursors.append(c)
        return c

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _pgconn_with_fake() -> tuple[PgConn, FakePgConnection]:
    import sys, types
    # 构造完整 psycopg2 包（含 extras + pool 子模块），避免 PgConn 导入失败
    fake_mod = types.ModuleType("psycopg2")
    extras_mod = types.ModuleType("psycopg2.extras")
    extras_mod.RealDictCursor = type("RealDictCursor", (), {})
    fake_mod.extras = extras_mod

    # 假连接池：直接返回新连接（测试无真实 pg）
    class FakePool:
        def __init__(self, *a, **k):
            pass

        def getconn(self):
            return FakePgConnection("fake")

        def putconn(self, conn):
            pass

    pool_mod = types.ModuleType("psycopg2.pool")
    pool_mod.ThreadedConnectionPool = FakePool
    fake_mod.pool = pool_mod
    fake_mod.connect = lambda dsn: FakePgConnection(dsn)
    sys.modules["psycopg2"] = fake_mod
    sys.modules["psycopg2.extras"] = extras_mod
    sys.modules["psycopg2.pool"] = pool_mod
    # 清掉跨测试残留的池引用（防止旧 fake 泄漏）
    from app.db_adapter import _PgPool
    _PgPool._pools.clear()
    conn = PgConn("postgresql://u:p@h/db")
    return conn, conn._conn


def test_execute_converts_placeholder():
    conn, raw = _pgconn_with_fake()
    try:
        conn.execute("SELECT * FROM users WHERE id=?", ("abc",))
        sql, params = raw._cursors[-1].executed[0]
        assert sql == "SELECT * FROM users WHERE id=%s"
        assert params == ("abc",)
    finally:
        conn.close()


def test_execute_skips_pragma():
    conn, raw = _pgconn_with_fake()
    try:
        cur = conn.execute("PRAGMA journal_mode=WAL")
        assert cur.fetchone() is None  # 空游标，调用方不崩
        assert raw._cursors == []  # 未真正执行
    finally:
        conn.close()


def test_executescript_splits_and_converts():
    conn, raw = _pgconn_with_fake()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS t1 (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS t2 (name TEXT);
        """)
        executed = [c.executed for c in raw._cursors]
        assert len(executed) == 2
        assert "t1" in executed[0][0][0]
        assert "t2" in executed[1][0][0]
    finally:
        conn.close()
