# -*- coding: utf-8 -*-
"""调度器 leader 选举测试：单机放行；云模式 SETNX 唯一 leader + 失联接管。"""
import time
from unittest.mock import patch

from app.config import get_settings
from app.services import distributed


class FakeRedisLeader:
    """内存版 Redis：leader 选举用到的 set(nx,ex)/get/expire。"""
    def __init__(self):
        self._data: dict[str, tuple[str, float]] = {}

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

    def expire(self, key, seconds):
        item = self._data.get(key)
        if item:
            self._data[key] = (item[0], time.time() + seconds)
        return bool(item)

    def delete(self, key):
        return self._data.pop(key, None) is not None

    def exists(self, key):
        return self.get(key) is not None


def _enable_fake():
    fake = FakeRedisLeader()
    distributed._redis = fake
    distributed._redis_error = None
    return fake


def test_leader_local_mode():
    """单机模式（无 Redis）：直接成为 leader（本来就只有本实例）。"""
    p = patch.object(get_settings(), "redis_url", "")
    p.start()
    distributed._redis = None
    distributed._redis_error = None
    try:
        assert distributed.try_become_leader() is True
        assert distributed.renew_leadership() is True  # 单机续期恒成功
    finally:
        p.stop()


def test_leader_single_wins():
    """云模式：第一个实例拿到 leader，第二个拿不到。"""
    fake = _enable_fake()
    try:
        # 实例 A（worker id 固定为测试值）
        with patch.object(distributed, "_worker_id", "worker-A"):
            assert distributed.try_become_leader() is True
        # 实例 B 抢不到
        with patch.object(distributed, "_worker_id", "worker-B"):
            assert distributed.try_become_leader() is False
        # A 能续期
        with patch.object(distributed, "_worker_id", "worker-A"):
            assert distributed.renew_leadership() is True
        # B 续期失败（不是持有者）
        with patch.object(distributed, "_worker_id", "worker-B"):
            assert distributed.renew_leadership() is False
    finally:
        distributed._redis = None


def test_leader_takeover_after_expiry():
    """leader 失联（租约过期）后，其他实例自动接管。"""
    fake = _enable_fake()
    try:
        with patch.object(distributed, "_worker_id", "worker-A"):
            assert distributed.try_become_leader(ttl_seconds=30) is True
        # 手动让租约过期
        fake._data[distributed.LEADER_KEY] = ("worker-A", time.time() - 1)
        # B 现在能抢到
        with patch.object(distributed, "_worker_id", "worker-B"):
            assert distributed.try_become_leader() is True
            assert distributed.renew_leadership() is True
    finally:
        distributed._redis = None


def test_leader_ttl_extend():
    """续期延长租约 TTL。"""
    fake = _enable_fake()
    try:
        with patch.object(distributed, "_worker_id", "worker-A"):
            assert distributed.try_become_leader(ttl_seconds=30) is True
            distributed.renew_leadership(ttl_seconds=300)
            _, expiry = fake._data[distributed.LEADER_KEY]
            assert expiry - time.time() > 250
    finally:
        distributed._redis = None
