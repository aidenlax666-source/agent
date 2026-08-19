# -*- coding: utf-8 -*-
"""任务过程日志 steps 落库测试：执行中每步落库，跨实例/崩溃后可读。"""
import time
import json
from unittest.mock import patch

from app.database import _init_db, _get_conn, _get_mini_task, update_mini_task

_init_db()


def _cleanup(task_id: str):
    with _get_conn() as conn:
        conn.execute("DELETE FROM mini_tasks WHERE id=?", (task_id,))


def test_steps_update_persisted():
    """update_mini_task(steps=[...]) 落库，_get_mini_task 读回解析为 list。"""
    tid = "steps-persist-1"
    try:
        from app.database import _save_mini_task
        _save_mini_task({
            "id": tid, "user_id": "u-steps", "requirement": "测试", "url": "",
            "status": "running", "message": "执行中", "created_at": time.time(),
            "result": None, "error": None, "image_paths": [], "data_paths": [],
        })
        # 模拟执行中每步落库
        asyncio_run(update_mini_task(tid, status="running", message="正在生成视频...",
                                     progress=30, steps=["生成视频", "生成报告"]))
        rec = _get_mini_task(tid)
        assert rec["steps"] == ["生成视频", "生成报告"]
        assert rec["status"] == "running"
        assert rec["progress"] == 30
        # 增量更新：追加步骤
        asyncio_run(update_mini_task(tid, steps=["生成视频", "生成报告", "执行代码任务"]))
        rec2 = _get_mini_task(tid)
        assert len(rec2["steps"]) == 3
        assert rec2["steps"][-1] == "执行代码任务"
    finally:
        _cleanup(tid)


def test_steps_survive_restart():
    """模拟 worker 崩溃重启：steps 从 DB 读回（跨实例可见）。"""
    tid = "steps-restart-1"
    try:
        from app.database import _save_mini_task
        _save_mini_task({
            "id": tid, "user_id": "u-steps2", "requirement": "测试2", "url": "",
            "status": "running", "message": "执行中", "created_at": time.time(),
            "result": None, "error": None, "image_paths": [], "data_paths": [],
        })
        asyncio_run(update_mini_task(tid, steps=["步骤A", "步骤B"]))
        # 模拟"另一个实例"读取（新连接）
        rec = _get_mini_task(tid)
        assert rec["steps"] == ["步骤A", "步骤B"]
    finally:
        _cleanup(tid)


def test_steps_empty_default():
    """无 steps 的任务读回为空 list（不报错）。"""
    tid = "steps-empty-1"
    try:
        from app.database import _save_mini_task
        _save_mini_task({
            "id": tid, "user_id": "u-steps3", "requirement": "测试3", "url": "",
            "status": "queued", "message": "", "created_at": time.time(),
            "result": None, "error": None, "image_paths": [], "data_paths": [],
        })
        rec = _get_mini_task(tid)
        assert rec["steps"] == []
    finally:
        _cleanup(tid)


def test_get_status_returns_steps():
    """get_status 返回 steps（内存路径 + DB 恢复路径）。"""
    import app.services.mini_tasks as mt
    tid = "steps-status-1"
    try:
        # 直接走 DB 恢复路径（内存无记录）
        from app.database import _save_mini_task
        _save_mini_task({
            "id": tid, "user_id": "u-steps4", "requirement": "测试4", "url": "",
            "status": "done", "message": "完成", "created_at": time.time(),
            "result": {"status": "ok"}, "error": None, "image_paths": [], "data_paths": [],
            "steps": ["生成", "校验"],
        })
        st = mt.get_status(tid, user_id="u-steps4")
        assert st is not None
        assert st["steps"] == ["生成", "校验"]
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
