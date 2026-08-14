from __future__ import annotations
"""In-process async long-task runner (no Redis/Celery required).

Long-running jobs (full script runs, scheduled executions) are launched with
asyncio.create_task and tracked in a registry so concurrent runs for the same
key are deduplicated and can be inspected / cancelled.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

# key -> asyncio.Task
_RUNNING: dict[str, asyncio.Task] = {}


def _cleanup(key: str) -> None:
    _RUNNING.pop(key, None)


def start_background(key: str, coro) -> bool:
    """Launch `coro` in the background, keyed by `key`.

    Returns True if started, False if a task with the same key is still running.
    """
    key = str(key)
    existing = _RUNNING.get(key)
    if existing is not None and not existing.done():
        return False

    async def _runner():
        try:
            await coro
        except asyncio.CancelledError:
            logger.info(f"[long_task:{key}] cancelled")
        except Exception:
            logger.exception(f"[long_task:{key}] background task failed")
        finally:
            _cleanup(key)

    task = asyncio.create_task(_runner())
    _RUNNING[key] = task
    return True


def is_running(key: str) -> bool:
    task = _RUNNING.get(str(key))
    return task is not None and not task.done()


def cancel(key: str) -> bool:
    task = _RUNNING.get(str(key))
    if task is not None and not task.done():
        task.cancel()
        return True
    return False
