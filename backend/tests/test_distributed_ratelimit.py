# -*- coding: utf-8 -*-
"""全局限流测试：Redis 计数器跨实例共享；单机模式进程内计数。"""
import time
from unittest.mock import patch

from app.config import get_settings
from app.services import distributed


class FakeRedisRate:
    """内存版 Redis：实现 rate_limit 用到的 incr/expire。"""
    def __init__(self):
        self._data: dict[str, tuple[int, float]] = {}  # key -> (count, expiry_ts)

    def ping(self):
        return True

    def incr(self, key):
        item = self._data.get(key)
        now = time.time()
        if item is None or (item[1] and item[1] < now):
            self._data[key] = (1, 0)
            return 1
        self._data[key] = (item[0] + 1, item[1])
        return self._data[key][0]

    def expire(self, key, seconds):
        item = self._data.get(key)
        if item:
            self._data[key] = (item[0], time.time() + seconds)
        return bool(item)

    def get(self, key):
        return None

    def delete(self, key):
        return self._data.pop(key, None) is not None

    def exists(self, key):
        return key in self._data


def _enable_fake():
    fake = FakeRedisRate()
    distributed._redis = fake
    distributed._redis_error = None
    return fake


def test_rate_limit_redis_mode():
    """云模式：跨实例共享计数——第 limit+1 次被拒。"""
    fake = _enable_fake()
    try:
        assert distributed.redis_enabled() is True
        # limit=3：前 3 次放行，第 4 次拒绝
        assert distributed.rate_limit("ip:1.2.3.4", limit=3) is True
        assert distributed.rate_limit("ip:1.2.3.4", limit=3) is True
        assert distributed.rate_limit("ip:1.2.3.4", limit=3) is True
        assert distributed.rate_limit("ip:1.2.3.4", limit=3) is False
    finally:
        distributed._redis = None


def test_rate_limit_redis_mode_isolated_keys():
    """云模式：不同 IP 互不影响。"""
    _enable_fake()
    try:
        assert distributed.rate_limit("ip:A", limit=2) is True
        assert distributed.rate_limit("ip:A", limit=2) is True
        assert distributed.rate_limit("ip:A", limit=2) is False
        assert distributed.rate_limit("ip:B", limit=2) is True  # B 不受 A 影响
    finally:
        distributed._redis = None


def test_rate_limit_redis_window_reset():
    """云模式：窗口过期后计数重置。"""
    fake = _enable_fake()
    try:
        assert distributed.rate_limit("ip:W", limit=2) is True
        assert distributed.rate_limit("ip:W", limit=2) is True
        assert distributed.rate_limit("ip:W", limit=2) is False
        # 手动让窗口过期
        fake._data["aiagent:ratelimit:ip:W"] = (99, time.time() - 1)
        assert distributed.rate_limit("ip:W", limit=2) is True  # 新窗口
    finally:
        distributed._redis = None


def test_rate_limit_local_mode():
    """单机模式（无 Redis）：进程内计数同样生效。"""
    p = patch.object(get_settings(), "redis_url", "")
    p.start()
    distributed._redis = None
    distributed._redis_error = None
    distributed._rate_counts.clear()
    try:
        assert distributed.redis_enabled() is False
        assert distributed.rate_limit("ip:L", limit=2) is True
        assert distributed.rate_limit("ip:L", limit=2) is True
        assert distributed.rate_limit("ip:L", limit=2) is False
        # 不同 key 不受影响
        assert distributed.rate_limit("ip:M", limit=2) is True
    finally:
        p.stop()
        distributed._rate_counts.clear()
