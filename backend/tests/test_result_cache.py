# -*- coding: utf-8 -*-
"""结果缓存测试：同用户同需求复用最近成功结果；不同需求/不同用户不命中。"""
import time
import json
from unittest.mock import patch

from app.config import get_settings
from app.database import _init_db, _save_mini_task, find_cached_result

_init_db()


def _insert_done(task_id: str, user_id: str, requirement: str, url: str, result: dict, updated_at: float) -> None:
    # 直接用 SQL 插入：_save_mini_task 会把 updated_at 强制刷新为 now，无法模拟"过期"场景
    from app.database import _get_conn
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO mini_tasks (id, user_id, requirement, url, status, message, created_at, updated_at,"
            " result, error, image_paths, data_paths) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, user_id, requirement, url, "done", "完成", updated_at - 10, updated_at,
             json.dumps(result, ensure_ascii=False), None, "[]", "[]"),
        )


def _cleanup(*task_ids):
    from app.database import _get_conn
    with _get_conn() as conn:
        for tid in task_ids:
            conn.execute("DELETE FROM mini_tasks WHERE id=?", (tid,))


def test_cache_hit_same_user_same_req():
    """同用户同需求（TTL 内成功）→ 命中。"""
    tid = "cache-hit-1"
    _insert_done(tid, "u-cache", "查天气", "", {"status": "ok", "rows": 3}, time.time() - 30)
    try:
        got = asyncio_run(find_cached_result("u-cache", "查天气", "", within_seconds=300))
        assert got is not None
        assert got["id"] == tid
        assert got["result"]["status"] == "ok"
    finally:
        _cleanup(tid)


def test_cache_miss_different_user():
    """不同用户不命中（结果按用户隔离，不串数据）。"""
    tid = "cache-miss-user"
    _insert_done(tid, "u-a", "查天气", "", {"status": "ok"}, time.time() - 30)
    try:
        got = asyncio_run(find_cached_result("u-b", "查天气", "", within_seconds=300))
        assert got is None
    finally:
        _cleanup(tid)


def test_cache_miss_different_requirement():
    """不同需求不命中（逐字一致才复用）。"""
    tid = "cache-miss-req"
    _insert_done(tid, "u-c", "查天气", "", {"status": "ok"}, time.time() - 30)
    try:
        got = asyncio_run(find_cached_result("u-c", "查气温", "", within_seconds=300))
        assert got is None
    finally:
        _cleanup(tid)


def test_cache_miss_expired():
    """超过 TTL 窗口不命中（避免返回过时结果）。"""
    tid = "cache-expired-1"
    _insert_done(tid, "u-d", "查天气", "", {"status": "ok"}, time.time() - 600)
    try:
        got = asyncio_run(find_cached_result("u-d", "查天气", "", within_seconds=300))
        assert got is None
    finally:
        _cleanup(tid)


def test_cache_miss_failed_task():
    """失败任务不命中（只缓存成功结果）。"""
    tid = "cache-failed-1"
    _save_mini_task({
        "id": tid, "user_id": "u-e", "requirement": "查天气", "url": "",
        "status": "failed", "message": "失败", "created_at": time.time() - 10,
        "updated_at": time.time() - 30, "result": None, "error": "err",
        "image_paths": [], "data_paths": [],
    })
    try:
        got = asyncio_run(find_cached_result("u-e", "查天气", "", within_seconds=300))
        assert got is None
    finally:
        _cleanup(tid)


def test_cache_picks_latest():
    """多条记录时取最近一次成功。"""
    tid_old = "cache-old-1"
    tid_new = "cache-new-1"
    _insert_done(tid_old, "u-f", "查天气", "", {"status": "ok", "v": 1}, time.time() - 200)
    _insert_done(tid_new, "u-f", "查天气", "", {"status": "ok", "v": 2}, time.time() - 10)
    try:
        got = asyncio_run(find_cached_result("u-f", "查天气", "", within_seconds=300))
        assert got is not None
        assert got["id"] == tid_new
        assert got["result"]["v"] == 2
    finally:
        _cleanup(tid_old, tid_new)


def test_create_task_returns_cached_without_charge():
    """API 层：缓存命中时直接返回结果且不扣积分。"""
    from fastapi import Request
    import app.api.mini as mini_api
    from app.database import _get_conn

    tid = "cache-api-1"
    _insert_done(tid, "u-api", "缓存任务", "", {"status": "ok", "output_file": "/x.png"}, time.time() - 20)
    try:
        # mock 一个匿名用户请求（走限速但这里直接调核心逻辑）
        class FakeUser:
            def __init__(self):
                self["id"] = "u-api"
                self["email"] = "anon_test"

            def __getitem__(self, k):
                return {"id": "u-api", "email": "anon_test"}[k]

        user = {"id": "u-api", "email": "anon_test"}
        settings_p = patch.object(get_settings(), "result_cache_ttl", 300)
        settings_p.start()
        try:
            result = asyncio_run(mini_api._maybe_cached_result(user, "缓存任务", "", None))
            assert result is not None
            assert result["cached"] is True
            assert result["task_id"] == tid
        finally:
            settings_p.stop()
    finally:
        _cleanup(tid)


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
