# -*- coding: utf-8 -*-
"""全局沙箱并发信号量测试：云架构下跨实例精确限制（Redis 槽位 SETNX+TTL）。

单机模式（无 Redis）：acquire 直接放行（返回 0），由本地信号量兜底。
云模式（FakeRedis）：limit 个槽位，满了排队返回 None；释放后可再占；TTL 过期自动释放。
"""
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
    fake = FakeRedis()
    distributed._redis = fake
    distributed._redis_error = None
    return fake


def test_sandbox_slot_local_mode():
    """单机模式（无 Redis）：acquire 返回 0 放行，release 无操作。"""
    p = patch.object(get_settings(), "redis_url", "")
    p.start()
    distributed._redis = None
    distributed._redis_error = None
    try:
        assert distributed.acquire_sandbox_slot(limit=3) == 0
        distributed.release_sandbox_slot(0)  # 不应抛异常
        assert distributed.redis_enabled() is False
    finally:
        p.stop()


def test_sandbox_slot_redis_mode_cap():
    """云模式：limit=2 时第 3 个获取返回 None（并发被全局限制）。"""
    _enable_fake_redis()
    try:
        assert distributed.acquire_sandbox_slot(limit=2) == 0  # worker 占槽 0
        assert distributed.acquire_sandbox_slot(limit=2) == 1  # 占槽 1
        assert distributed.acquire_sandbox_slot(limit=2) is None  # 满
        # 释放后能再占
        distributed.release_sandbox_slot(1)
        assert distributed.acquire_sandbox_slot(limit=2) == 1
        distributed.release_sandbox_slot(0)
        distributed.release_sandbox_slot(1)
    finally:
        distributed._redis = None


def test_sandbox_slot_ttl_expire():
    """云模式：槽位 TTL 过期后自动释放（worker 崩溃不会永久占位）。"""
    fake = _enable_fake_redis()
    try:
        assert distributed.acquire_sandbox_slot(limit=1, ttl_seconds=30) == 0
        assert distributed.acquire_sandbox_slot(limit=1, ttl_seconds=30) is None  # 已占
        # 模拟 TTL 过期
        fake._data["aiagent:sandbox:slot:0"] = ("x", time.time() - 1)
        assert distributed.acquire_sandbox_slot(limit=1, ttl_seconds=30) == 0  # 过期后可再占
        distributed.release_sandbox_slot(0)
    finally:
        distributed._redis = None


def test_sandbox_slot_renew():
    """云模式：续期延长槽位 TTL（长任务防被抢）。"""
    fake = _enable_fake_redis()
    try:
        assert distributed.acquire_sandbox_slot(limit=1, ttl_seconds=60) == 0
        distributed.renew_sandbox_slot(0, ttl_seconds=300)
        _, expiry = fake._data["aiagent:sandbox:slot:0"]
        assert expiry - time.time() > 250
        distributed.release_sandbox_slot(0)
    finally:
        distributed._redis = None


def test_task_lease_alive():
    """task_lease_alive：租约在 → True；释放/过期 → False。"""
    _enable_fake_redis()
    try:
        assert distributed.task_lease_alive("t-1") is False
        distributed.claim_task_lease("t-1", ttl_seconds=300)
        assert distributed.task_lease_alive("t-1") is True
        distributed.release_task_lease("t-1")
        assert distributed.task_lease_alive("t-1") is False
    finally:
        distributed._redis = None
