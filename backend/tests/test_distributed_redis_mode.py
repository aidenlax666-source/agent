# -*- coding: utf-8 -*-
"""分布式层 Redis 分支验证：用内存 fake redis 模拟云模式（沙箱无真实 Redis 网络）。"""
import time
from unittest.mock import patch

from app.config import get_settings
from app.services import distributed


class FakeRedis:
    """内存版 redis：实现分布式层用到的 set(nx,ex)/get/delete/exists/expire/ping。"""
    def __init__(self):
        self._data: dict[str, tuple[str, float]] = {}  # key -> (value, expiry_ts)

    def ping(self):
        return True

    def set(self, key, value, nx=False, ex=None):
        now = time.time()
        existing = self._data.get(key)
        if nx and existing and existing[1] > now:
            return False
        self._data[key] = (value, (now + ex) if ex else 0)
        return True

    def get(self, key):
        item = self._data.get(key)
        if not item:
            return None
        if item[1] and item[1] < time.time():
            self._data.pop(key, None)
            return None
        return item[0]

    def delete(self, key):
        return self._data.pop(key, None) is not None

    def exists(self, key):
        return self.get(key) is not None

    def expire(self, key, seconds):
        item = self._data.get(key)
        if item:
            self._data[key] = (item[0], time.time() + seconds)
        return bool(item)


def _enable_fake_redis():
    """让 distributed 使用 FakeRedis（模拟云模式）。"""
    fake = FakeRedis()
    distributed._redis = fake
    distributed._redis_error = None
    return fake


def test_redis_mode_lock():
    """云模式：锁 SETNX——第二实例拿不到，释放后可拿。"""
    fake = _enable_fake_redis()
    try:
        assert distributed.redis_enabled() is True
        assert distributed.acquire_lock("task-9", ttl_seconds=60) is True
        # 模拟"另一个实例"：同一把锁拿不到
        assert distributed.acquire_lock("task-9", ttl_seconds=60) is False
        distributed.release_lock("task-9")
        assert distributed.acquire_lock("task-9", ttl_seconds=60) is True
        distributed.release_lock("task-9")
    finally:
        distributed._redis = None


def test_redis_mode_lock_ttl_expire():
    """云模式：锁 TTL 过期后其他实例可拿（模拟：直接让 fake 时间前进）。"""
    fake = _enable_fake_redis()
    try:
        assert distributed.acquire_lock("k", ttl_seconds=5) is True
        # 手动让 key 过期（fakeredis 语义：直接删）
        fake._data["aiagent:lock:k"] = ("1", time.time() - 1)  # 已过期
        assert distributed.acquire_lock("k", ttl_seconds=5) is True  # 过期后可拿
        distributed.release_lock("k")
    finally:
        distributed._redis = None


def test_redis_mode_dedup_cross_instance():
    """云模式：去重跨实例生效——实例 A 标记，实例 B 拿不到。"""
    fake = _enable_fake_redis()
    try:
        # 实例 A
        assert distributed.dedup_mark("mon:1:微信", ttl_seconds=300) is True
        # 实例 B（同 redis）拿不到
        assert distributed.dedup_mark("mon:1:微信", ttl_seconds=300) is False
        # 不同 key 可以
        assert distributed.dedup_mark("mon:1:浏览器", ttl_seconds=300) is True
        # 清除后可再触发
        distributed.dedup_clear("mon:1:微信")
        assert distributed.dedup_mark("mon:1:微信", ttl_seconds=300) is True
    finally:
        distributed._redis = None


def test_redis_mode_renew():
    """云模式：续锁延长 TTL。"""
    fake = _enable_fake_redis()
    try:
        assert distributed.acquire_lock("long-task", ttl_seconds=60) is True
        distributed.renew_lock("long-task", ttl_seconds=300)
        _, expiry = fake._data["aiagent:lock:long-task"]
        assert expiry - time.time() > 250  # 续到了 300s
        distributed.release_lock("long-task")
    finally:
        distributed._redis = None


def test_redis_mode_is_running_via_lock():
    """云模式：is_running 检查 Redis 锁（其他实例持锁时返回 True）。"""
    fake = _enable_fake_redis()
    from app.services import long_task
    try:
        # 无锁 → 不 running
        assert long_task.is_running("ghost-task") is False
        # 持锁（模拟其他实例在跑）→ running
        distributed.acquire_lock("ghost-task", ttl_seconds=300)
        assert long_task.is_running("ghost-task") is True
        distributed.release_lock("ghost-task")
    finally:
        distributed._redis = None
        long_task._RUNNING.clear()
