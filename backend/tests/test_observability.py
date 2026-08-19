# -*- coding: utf-8 -*-
"""可观测性测试：任务统计聚合 + worker 心跳 + 队列深度。"""
import time
import json
from unittest.mock import patch

from app.config import get_settings
from app.database import _init_db, _get_conn, task_stats
from app.services import distributed

_init_db()


class FakeRedisObs:
    """内存版 Redis：可观测性用到的 set(ex)/get/keys/llen/lpush。"""
    def __init__(self):
        self._data: dict = {}
        self._lists: dict = {}

    def ping(self):
        return True

    def set(self, key, value, nx=False, ex=None):
        self._data[key] = (value, time.time() + ex if ex else 0)
        return True

    def get(self, key):
        item = self._data.get(key)
        if item is None:
            return None
        if item[1] and item[1] < time.time():
            self._data.pop(key, None)
            return None
        return item[0]

    def delete(self, key):
        return self._data.pop(key, None) is not None

    def keys(self, pattern):
        # 简化：只支持 aiagent:worker:* 前缀匹配
        prefix = pattern.replace("*", "")
        return [k for k in self._data if k.startswith(prefix)]

    def lpush(self, key, value):
        self._lists.setdefault(key, []).insert(0, value)
        return True

    def llen(self, key):
        return len(self._lists.get(key) or [])

    def exists(self, key):
        return key in self._data or key in self._lists


def _enable_fake():
    fake = FakeRedisObs()
    distributed._redis = fake
    distributed._redis_error = None
    return fake


def _insert_task(task_id: str, status: str, created_at: float, updated_at: float, user_id: str = "obs_user") -> None:
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO mini_tasks (id, user_id, requirement, url, status, message, created_at, updated_at,"
            " result, error, image_paths, data_paths) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, user_id, "统计测试", "", status, "", created_at, updated_at,
             json.dumps({"ok": True}) if status == "done" else None, None, "[]", "[]"),
        )


def _cleanup(*task_ids):
    with _get_conn() as conn:
        for tid in task_ids:
            conn.execute("DELETE FROM mini_tasks WHERE id=?", (tid,))


def test_task_stats_aggregation():
    """统计：总量/今日/状态分布/成功率/平均耗时。"""
    now = time.time()
    ids = [f"obs-{i}" for i in range(5)]
    try:
        _insert_task(ids[0], "done", now - 100, now - 50)     # 今日成功（耗时 50s）
        _insert_task(ids[1], "done", now - 200, now - 150)    # 今日成功（耗时 50s）
        _insert_task(ids[2], "failed", now - 300, now - 290)  # 今日失败
        _insert_task(ids[3], "running", now - 10, now)        # 进行中
        _insert_task(ids[4], "done", now - 90000, now - 89900)  # 昨日成功（不计今日）

        s = asyncio_run(task_stats())
        assert s["total"] >= 5
        assert s["today"] >= 4
        assert s["by_status"].get("done", 0) >= 3
        assert s["by_status"].get("failed", 0) >= 1
        assert s["done_today"] >= 2
        assert s["failed_today"] >= 1
        assert s["success_rate_today"] is not None and 0 < s["success_rate_today"] <= 1
        assert s["avg_elapsed_today"] is not None and s["avg_elapsed_today"] > 0
    finally:
        _cleanup(*ids)


def test_worker_heartbeat_and_list():
    """worker 心跳注册/查询；失联（TTL 过期）消失。"""
    fake = _enable_fake()
    try:
        distributed.worker_heartbeat(worker_id="w-1")
        distributed.worker_heartbeat(worker_id="w-2")
        workers = distributed.list_workers()
        assert {w["id"] for w in workers} == {"w-1", "w-2"}
        # 手动让 w-1 心跳过期
        fake._data["aiagent:worker:w-1"] = ("x", time.time() - 1)
        workers2 = distributed.list_workers()
        assert {w["id"] for w in workers2} == {"w-2"}
    finally:
        distributed._redis = None


def test_queue_depth():
    """队列深度：普通/高优分开统计。"""
    _enable_fake()
    try:
        distributed.enqueue_task("n-1")
        distributed.enqueue_task("n-2")
        distributed.enqueue_task("h-1", priority="high")
        depth = distributed.queue_depth()
        assert depth == {"normal": 2, "high": 1}
    finally:
        distributed._redis = None


def test_stats_local_mode_no_redis():
    """单机模式：接口可用，workers 为空、queue_depth 为空。"""
    p = patch.object(get_settings(), "redis_url", "")
    p.start()
    distributed._redis = None
    distributed._redis_error = None
    try:
        assert distributed.list_workers() == []
        assert distributed.queue_depth() == {}
        assert distributed.redis_enabled() is False
    finally:
        p.stop()


def asyncio_run(coro):
    import asyncio
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
