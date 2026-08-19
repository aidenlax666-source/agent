# -*- coding: utf-8 -*-
"""分布式任务队列集成测试：入队 → worker 消费 → 执行（用 FakeRedis 模拟云模式）。"""
import asyncio
import time
from unittest.mock import patch

from app.config import get_settings
from app.services import distributed


class FakeRedisQueue:
    """内存版 Redis：支持分布式层用到的 list 队列 + 锁/去重。"""
    def __init__(self):
        self._data: dict = {}          # key -> value
        self._lists: dict = {}         # key -> list
        self._locks: dict = {}         # key -> expiry_ts

    def ping(self):
        return True

    # --- 锁原语 ---
    def set(self, key, value, nx=False, ex=None):
        now = time.time()
        if nx and key in self._locks and self._locks[key] > now:
            return False
        self._locks[key] = (now + ex) if ex else 0
        return True

    def get(self, key):
        exp = self._locks.get(key)
        if exp is None:
            return None
        if exp and exp < time.time():
            self._locks.pop(key, None)
            return None
        return "1"

    def delete(self, key):
        return self._locks.pop(key, None) is not None

    def exists(self, key):
        return self.get(key) is not None

    def expire(self, key, seconds):
        if key in self._locks:
            self._locks[key] = time.time() + seconds
        return key in self._locks

    # --- 队列原语 ---
    def lpush(self, key, value):
        self._lists.setdefault(key, []).insert(0, value)
        return True

    def brpop(self, key, timeout=5):
        q = self._lists.get(key) or []
        if q:
            val = q.pop()
            return (key, val)
        return None  # 模拟无任务时立即返回（测试不真阻塞）


def _enable_fake():
    fake = FakeRedisQueue()
    distributed._redis = fake
    distributed._redis_error = None
    return fake


def test_enqueue_dequeue():
    """入队后能取到（Redis list 语义：LPUSH+BRPOP = 后进先出）。"""
    fake = _enable_fake()
    try:
        assert distributed.redis_enabled() is True
        assert distributed.enqueue_task("task-1") is True
        assert distributed.enqueue_task("task-2") is True
        # LPUSH 头插 + BRPOP 尾取 → 先取 task-1（先进先出在"消费者单worker"下等同）
        # 实际语义：BRPOP 取 list 尾部 = 最先 push 的
        assert distributed.dequeue_task() == "task-1"
        assert distributed.dequeue_task() == "task-2"
        assert distributed.dequeue_task() is None  # 队列空
    finally:
        distributed._redis = None


def test_submit_uses_queue_when_redis():
    """有 Redis 时 submit 走队列模式（不 start_background）。"""
    import app.services.mini_tasks as mt

    fake = _enable_fake()
    started_single = {"called": False}
    orig = mt.start_background

    def fake_start(key, coro):
        started_single["called"] = True  # 不应被调用
        return True

    try:
        with patch.object(mt, "start_background", side_effect=fake_start):
            record = mt.submit("计算 1 加 1", user_id="queue_test_user")
            task_id = record["id"]
        assert started_single["called"] is False, "Redis 模式下不应走单机 start_background"
        # 任务已入队
        assert distributed.dequeue_task() == task_id
        # 已落库（worker 能从 DB 重建）
        rec = mt._task_record_from_db(task_id)
        assert rec is not None
        assert rec["requirement"] == "计算 1 加 1"
    finally:
        distributed._redis = None
        # 清理测试任务
        try:
            from app.database import _get_conn
            with _get_conn() as conn:
                conn.execute("DELETE FROM mini_tasks WHERE user_id=?", ("queue_test_user",))
        except Exception:
            pass


def test_task_record_rebuild_from_db():
    """worker 从 DB 重建执行上下文（image_paths/data_paths 保留）。"""
    import app.services.mini_tasks as mt
    from app.database import _save_mini_task, _get_conn

    rec = {
        "id": "rebuild-test-1",
        "user_id": "rebuild_user",
        "requirement": "生成数据",
        "url": "",
        "status": "queued",
        "progress": 0,
        "message": "排队中",
        "result": None,
        "error": None,
        "image_paths": ["/tmp/a.png"],
        "data_paths": ["/tmp/b.csv"],
        "created_at": time.time(),
    }
    try:
        _save_mini_task(rec)
        rebuilt = mt._task_record_from_db("rebuild-test-1")
        assert rebuilt is not None
        assert rebuilt["image_paths"] == ["/tmp/a.png"]
        assert rebuilt["data_paths"] == ["/tmp/b.csv"]
        assert rebuilt["requirement"] == "生成数据"
    finally:
        with _get_conn() as conn:
            conn.execute("DELETE FROM mini_tasks WHERE id=?", ("rebuild-test-1",))


def test_worker_consumes_queue():
    """worker 循环从队列取任务并执行（真实任务入队 → worker 消费 → mock 执行）。"""
    import app.services.mini_tasks as mt

    fake = _enable_fake()
    executed = {"task_id": None}

    async def fake_run(task_id, requirement, url, record):
        executed["task_id"] = task_id
        record["status"] = "done"

    try:
        # 真实提交一个任务（Redis 模式 → 入队）
        record = mt.submit("worker 集成测试任务", user_id="worker_int_user")
        task_id = record["id"]

        # 模拟 worker 消费逻辑（手动执行队列 worker 的核心步骤）
        async def run_one():
            got = distributed.dequeue_task(timeout=0.1)
            if not got:
                return None
            assert got == task_id
            if not distributed.claim_task_lease(got, ttl_seconds=1800):
                return None
            rec = mt._task_record_from_db(got)
            assert rec is not None, "worker 应能从 DB 重建任务上下文"
            with patch.object(mt, "_run_task", side_effect=fake_run):
                await mt._run_task(got, rec["requirement"], rec.get("url") or "", rec)
            distributed.release_task_lease(got)
            return got

        got = asyncio.run(run_one())
        assert got == task_id
        assert executed["task_id"] == task_id
    finally:
        distributed._redis = None
        try:
            from app.database import _get_conn
            with _get_conn() as conn:
                conn.execute("DELETE FROM mini_tasks WHERE user_id=?", ("worker_int_user",))
        except Exception:
            pass
