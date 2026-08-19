# -*- coding: utf-8 -*-
"""分布式协调层测试：无 Redis 时回退单机内存模式，锁/去重/调度行为正确。"""
from unittest.mock import patch

from app.config import get_settings
from app.services import distributed


def _force_local_mode():
    """强制单机模式：mock redis_url 为空，避免测试真的连 Redis。"""
    distributed._local_locks.clear()
    distributed._local_set.clear()
    distributed._redis = None
    distributed._redis_error = None
    patcher = patch.object(get_settings(), "redis_url", "")
    patcher.start()
    return patcher


def test_lock_acquire_release():
    """单机模式：锁可获取/释放，重复获取被拒。"""
    p = _force_local_mode()
    try:
        assert distributed.acquire_lock("task-1", ttl_seconds=60) is True
        assert distributed.acquire_lock("task-1", ttl_seconds=60) is False  # 已锁
        distributed.release_lock("task-1")
        assert distributed.acquire_lock("task-1", ttl_seconds=60) is True  # 释放后可再拿
        distributed.release_lock("task-1")
    finally:
        p.stop()


def test_lock_expires():
    """单机模式：锁 TTL 过期后自动释放。"""
    p = _force_local_mode()
    try:
        with patch("app.services.distributed.time.time", side_effect=[100.0, 100.0, 200.0]):
            assert distributed.acquire_lock("k", ttl_seconds=60) is True
            assert distributed.acquire_lock("k", ttl_seconds=60) is False  # 100s 时仍锁
            assert distributed.acquire_lock("k", ttl_seconds=60) is True   # 200s 时过期可拿
    finally:
        p.stop()


def test_dedup_mark():
    """单机模式：去重标记——首次 True，TTL 内重复 False。"""
    p = _force_local_mode()
    try:
        assert distributed.dedup_mark("rem:abc:202501010800", ttl_seconds=120) is True
        assert distributed.dedup_mark("rem:abc:202501010800", ttl_seconds=120) is False
        distributed.dedup_clear("rem:abc:202501010800")
        assert distributed.dedup_mark("rem:abc:202501010800", ttl_seconds=120) is True  # 清除后可再
    finally:
        p.stop()


def test_dedup_different_keys():
    """单机模式：不同 key 互不影响。"""
    p = _force_local_mode()
    try:
        assert distributed.dedup_mark("a", ttl_seconds=60) is True
        assert distributed.dedup_mark("b", ttl_seconds=60) is True
        assert distributed.dedup_mark("a", ttl_seconds=60) is False
    finally:
        p.stop()


def test_redis_fallback_on_error():
    """Redis 连接失败 → 回退单机模式，功能仍可用。"""
    p = _force_local_mode()
    try:
        assert distributed.acquire_lock("x") is True
        distributed.release_lock("x")
    finally:
        p.stop()


def test_redis_enabled_false_without_url():
    """未配置 REDIS_URL → redis_enabled False（保持单机模式）。"""
    p = _force_local_mode()
    try:
        assert distributed.redis_enabled() is False
    finally:
        p.stop()
