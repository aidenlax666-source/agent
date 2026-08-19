# -*- coding: utf-8 -*-
"""队列优先级测试：高优任务先于普通任务消费（双队列，高优插队）。"""
import time
from unittest.mock import patch

from app.services import distributed


class FakeRedisPriority:
    """内存版 Redis：双队列（普通/高优）+ 锁原语。"""
    def __init__(self):
        self._locks: dict = {}
        self._lists: dict = {}

    def ping(self):
        return True

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

    def lpush(self, key, value):
        self._lists.setdefault(key, []).insert(0, value)
        return True

    def brpop(self, key, timeout=5):
        q = self._lists.get(key) or []
        if q:
            return (key, q.pop())
        return None  # 测试不真阻塞

    def lrange(self, key, start, end):
        q = self._lists.get(key) or []
        return q[start:end if end >= 0 else None]


def _enable_fake():
    fake = FakeRedisPriority()
    distributed._redis = fake
    distributed._redis_error = None
    return fake


def test_high_priority_consumed_first():
    """高优任务先于普通任务被消费（普通先入队，高优后入队仍先取）。"""
    _enable_fake()
    try:
        # 普通任务先入队
        assert distributed.enqueue_task("normal-1") is True
        assert distributed.enqueue_task("normal-2") is True
        # 高优任务后入队
        assert distributed.enqueue_task("high-1", priority="high") is True
        assert distributed.enqueue_task("high-2", priority="high") is True
        # 消费顺序：高优优先（先进先出），然后普通
        assert distributed.dequeue_task(timeout=0.1) == "high-1"
        assert distributed.dequeue_task(timeout=0.1) == "high-2"
        assert distributed.dequeue_task(timeout=0.1) == "normal-1"
        assert distributed.dequeue_task(timeout=0.1) == "normal-2"
        assert distributed.dequeue_task(timeout=0.1) is None
    finally:
        distributed._redis = None


def test_high_queue_isolated():
    """高优队列不影响普通队列（互不干扰）。"""
    fake = _enable_fake()
    try:
        distributed.enqueue_task("h-1", priority="high")
        distributed.enqueue_task("n-1")
        # 高优队列只有 h-1
        assert fake._lists[distributed.TASK_QUEUE_KEY_HIGH] == ["h-1"]
        assert fake._lists[distributed.TASK_QUEUE_KEY] == ["n-1"]
        # task_in_queue 两个队列都查
        assert distributed.task_in_queue("h-1") is True
        assert distributed.task_in_queue("n-1") is True
        assert distributed.task_in_queue("ghost") is False
    finally:
        distributed._redis = None


def test_priority_param_flows_to_submit():
    """submit(priority=high) → 任务进高优队列。"""
    import app.services.mini_tasks as mt

    _enable_fake()
    try:
        record = mt.submit("高优测试任务", user_id="prio_user", priority="high")
        tid = record["id"]
        # 高优队列里有该任务
        assert distributed.task_in_queue(tid) is True
        # 消费时先取到它
        assert distributed.dequeue_task(timeout=0.1) == tid
    finally:
        distributed._redis = None
        try:
            from app.database import _get_conn
            with _get_conn() as conn:
                conn.execute("DELETE FROM mini_tasks WHERE user_id=?", ("prio_user",))
        except Exception:
            pass
