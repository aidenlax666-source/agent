from __future__ import annotations
"""In-process async long-task runner (no Redis/Celery required).

Long-running jobs (full script runs, scheduled executions) are launched with
asyncio.create_task and tracked in a registry so concurrent runs for the same
key are deduplicated and can be inspected / cancelled.

云架构：有 Redis 时，任务执行锁走 Redis（多实例不重复执行同一任务）；
无 Redis 时保持进程内 registry（单机模式）。
"""

import asyncio
import logging

from app.services import distributed

logger = logging.getLogger(__name__)

# key -> asyncio.Task（单机模式）
_RUNNING: dict[str, asyncio.Task] = {}

# 任务锁 TTL：长任务执行期间通过 renew 续期，防锁过期被别的实例抢跑
_TASK_LOCK_TTL = 600


def _cleanup(key: str) -> None:
    _RUNNING.pop(key, None)


def start_background(key: str, coro, lock_ttl: int = _TASK_LOCK_TTL) -> bool:
    """Launch `coro` in the background, keyed by `key`.

    Returns True if started, False if a task with the same key is still running
    (or the distributed lock is held by another instance).
    """
    key = str(key)

    # 分布式锁：多实例下同一任务只在一个实例执行
    if not distributed.acquire_lock(key, ttl_seconds=lock_ttl):
        logger.info(f"[long_task:{key}] 锁被占用（其他实例正在执行），跳过")
        return False

    existing = _RUNNING.get(key)
    if existing is not None and not existing.done():
        # 本实例已在跑（单机模式）——释放刚拿的锁避免残留
        distributed.release_lock(key)
        return False

    async def _runner():
        try:
            # 长任务执行期间周期性续锁
            renew_task = asyncio.create_task(_renew_loop(key, lock_ttl))
            try:
                await coro
            finally:
                renew_task.cancel()
                try:
                    await renew_task
                except (asyncio.CancelledError, Exception):
                    pass
        except asyncio.CancelledError:
            logger.info(f"[long_task:{key}] cancelled")
        except Exception:
            logger.exception(f"[long_task:{key}] background task failed")
        finally:
            distributed.release_lock(key)
            _cleanup(key)

    task = asyncio.create_task(_runner())
    _RUNNING[key] = task
    return True


async def _renew_loop(key: str, ttl: int) -> None:
    """每 30s 续一次锁，保证长任务持锁期间不被其他实例抢走。"""
    while True:
        await asyncio.sleep(30)
        distributed.renew_lock(key, ttl_seconds=ttl)


def is_running(key: str) -> bool:
    # 单机模式检查本实例；云架构由锁保证不重复
    task = _RUNNING.get(str(key))
    if task is not None and not task.done():
        return True
    # 云架构：锁被持有 = 有其他实例在跑
    if distributed.redis_enabled():
        r = distributed._get_redis()
        if r is not None:
            try:
                return bool(r.exists(f"aiagent:lock:{key}"))
            except Exception:
                pass
    return False


def cancel(key: str) -> bool:
    task = _RUNNING.get(str(key))
    if task is not None and not task.done():
        task.cancel()
        return True
    return False
