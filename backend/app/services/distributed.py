from __future__ import annotations
"""分布式协调层（云架构）：Redis 提供跨实例共享的锁/去重/调度。

设计：**可选依赖**——配置 REDIS_URL 时启用 Redis（多实例部署），
留空时自动回退进程内内存实现（单机模式，保持原有行为不变）。

这层封装了云架构需要的三个原语：
- 锁（SETNX + 过期）：任务执行锁，防止多实例重复执行同一任务
- 去重（带 TTL 的集合）：提醒/监控触发去重，跨实例生效
- 原子递增：调度计数等
"""

import logging
import os
import time

from app.config import get_settings

logger = logging.getLogger("app.services.distributed")

_redis = None
_redis_error: str | None = None


def _get_redis():
    """惰性连接 Redis；失败记录错误并返回 None（回退单机模式）。"""
    global _redis, _redis_error
    if _redis is not None:
        return _redis
    if _redis_error:
        return None
    url = get_settings().redis_url.strip()
    if not url:
        return None
    try:
        import redis as _redis_mod
        _redis = _redis_mod.Redis.from_url(url, decode_responses=True, socket_timeout=3)
        _redis.ping()
        logger.info("[distributed] Redis 已连接（云架构多实例模式）")
        return _redis
    except Exception as e:
        _redis_error = f"{type(e).__name__}: {str(e)[:100]}"
        logger.warning("[distributed] Redis 不可用，回退单机内存模式: %s", _redis_error)
        return None


def redis_enabled() -> bool:
    return _get_redis() is not None


# ============================================================
# 1. 分布式锁（任务执行 / 调度抢占）
# ============================================================

# 单机模式用的内存锁表：{key: expiry_ts}
_local_locks: dict[str, float] = {}


def acquire_lock(key: str, ttl_seconds: int = 300) -> bool:
    """尝试获取锁。成功返回 True（调用方必须 release）。"""
    r = _get_redis()
    if r is not None:
        try:
            return bool(r.set(f"aiagent:lock:{key}", "1", nx=True, ex=ttl_seconds))
        except Exception:
            return True  # Redis 抖动时放行（避免任务卡死），靠 TTL 自愈
    # 单机模式：进程内锁
    now = time.time()
    expiry = _local_locks.get(key, 0)
    if expiry > now:
        return False  # 已锁
    _local_locks[key] = now + ttl_seconds
    return True


def release_lock(key: str) -> None:
    r = _get_redis()
    if r is not None:
        try:
            r.delete(f"aiagent:lock:{key}")
        except Exception:
            pass
        return
    _local_locks.pop(key, None)


def renew_lock(key: str, ttl_seconds: int = 300) -> None:
    """长任务续锁（防止锁在任务执行期间过期被别的实例抢走）。"""
    r = _get_redis()
    if r is not None:
        try:
            r.expire(f"aiagent:lock:{key}", ttl_seconds)
        except Exception:
            pass


# ============================================================
# 2. 分布式去重（提醒/监控触发：跨实例只发一次）
# ============================================================

_local_set: set[tuple[str, float]] = set()  # (key, expiry_ts)


def dedup_mark(key: str, ttl_seconds: int) -> bool:
    """标记 key 在 ttl 内已触发。返回 True 表示"这是第一次"（应触发）。"""
    r = _get_redis()
    if r is not None:
        try:
            return bool(r.set(f"aiagent:dedup:{key}", "1", nx=True, ex=ttl_seconds))
        except Exception:
            pass
    # 单机模式
    now = time.time()
    # 清理过期
    expired = [k for k, exp in list(_local_set) if exp <= now]
    for k in expired:
        _local_set.discard(k)
    if any(k == key for k, _ in _local_set):
        return False
    _local_set.add((key, now + ttl_seconds))
    return True


def dedup_clear(key: str) -> None:
    global _local_set
    r = _get_redis()
    if r is not None:
        try:
            r.delete(f"aiagent:dedup:{key}")
        except Exception:
            pass
        return
    _local_set = {x for x in _local_set if x[0] != key}


# ============================================================
# 3. 分布式调度（原子抢占）
# ============================================================

def scheduler_claim(task_id: str, expected_next: float, new_next: float) -> bool:
    """定时任务抢占：仅当 next_run_at 仍是期望值时更新（多实例不重复执行）。

    Redis 用 WATCH/MULTI 或 Lua；简化用 lock + 数据库原子更新兜底。
    这里返回 True 表示"可以执行"（锁获取成功）。
    """
    return acquire_lock(f"sched:{task_id}", ttl_seconds=120)


def scheduler_release(task_id: str) -> None:
    release_lock(f"sched:{task_id}")
