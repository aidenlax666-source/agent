# -*- coding: utf-8 -*-
"""⑦ 性能/成本小项测试：任务完整日志落盘 + pg 连接池行为。"""
import os
import time

from app.database import _init_db, _get_conn

_init_db()


def test_save_task_log_writes_file():
    """任务完整日志（脚本/stdout/错误）落 web/logs/{task_id}.log。"""
    import app.services.mini_tasks as mt
    tid = f"log-{int(time.time())}"
    merged = {
        "script": "print('hello')",
        "stdout": "DATA_ROWS:3\nhello",
        "error": None,
    }
    try:
        rel = mt._save_task_log(tid, merged)
        assert rel == f"logs/{tid}.log"
        # 文件真实存在（web/ 下）
        log_path = os.path.join(mt._WEB_DIR, rel.replace("/", os.sep))
        assert os.path.isfile(log_path)
        with open(log_path, encoding="utf-8") as f:
            content = f.read()
        assert "print('hello')" in content
        assert "DATA_ROWS:3" in content
        # merged 里带上了 log_file（可下载）
        assert merged["log_file"] == f"logs/{tid}.log"
    finally:
        p = os.path.join(mt._WEB_DIR, "logs", f"{tid}.log")
        if os.path.exists(p):
            os.unlink(p)


def test_save_task_log_empty_no_file():
    """无任何输出时返回 None 不写文件。"""
    import app.services.mini_tasks as mt
    assert mt._save_task_log(f"log-empty-{int(time.time())}", {"status": "ok"}) is None


def test_pg_pool_uses_pooled_connection():
    """pg 连接走连接池（不每次新建）；close 归还池。"""
    import sys, types
    from app.db_adapter import PgConn, _PgPool

    class FakePgConnection:
        def __init__(self, dsn):
            self.dsn = dsn
            self.closed = False

        def cursor(self, cursor_factory=None):
            class C:
                def execute(self, *a, **k):
                    pass

                def fetchone(self):
                    return None

                def fetchall(self):
                    return []

                def close(self):
                    pass
            return C()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            self.closed = True

    # 假 psycopg2 + 假池
    fake_mod = types.ModuleType("psycopg2")
    extras_mod = types.ModuleType("psycopg2.extras")
    extras_mod.RealDictCursor = type("RealDictCursor", (), {})
    fake_mod.extras = extras_mod

    made = {"n": 0}
    returned = {"n": 0}

    class FakePool:
        def __init__(self, *a, **k):
            pass

        def getconn(self):
            made["n"] += 1
            return FakePgConnection("fake")

        def putconn(self, conn):
            returned["n"] += 1

    pool_mod = types.ModuleType("psycopg2.pool")
    pool_mod.ThreadedConnectionPool = FakePool
    fake_mod.pool = pool_mod
    fake_mod.connect = lambda dsn: FakePgConnection(dsn)
    sys.modules["psycopg2"] = fake_mod
    sys.modules["psycopg2.extras"] = extras_mod
    sys.modules["psycopg2.pool"] = pool_mod

    _PgPool._pools.clear()
    try:
        c1 = PgConn("postgresql://u:p@h/db")
        c1.close()  # 归还池
        assert returned["n"] == 1, "close 应归还连接到池"
        # 再拿连接：池有货直接给（不新增）
        c2 = PgConn("postgresql://u:p@h/db")
        # 池空时 getconn 新建；第二次拿因第一次归还了，仍走池
        assert made["n"] >= 1
        c2.close()
    finally:
        _PgPool._pools.clear()
        # 清理 sys.modules 污染（后续测试可能 import 真 psycopg2）
        for m in ("psycopg2", "psycopg2.extras", "psycopg2.pool"):
            sys.modules.pop(m, None)
