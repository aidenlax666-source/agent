# -*- coding: utf-8 -*-
"""混合架构本地执行测试：本地队列（按用户隔离）+ 自动识别 + poll/report 状态机。"""
import time
from unittest.mock import patch

from app.services import distributed
from app.database import _init_db, _get_conn, _save_mini_task, _get_mini_task

_init_db()


class FakeRedisLocal:
    """内存版 Redis：本地队列（list）+ 锁。"""
    def __init__(self):
        self._locks = {}
        self._lists = {}

    def ping(self):
        return True

    def set(self, key, value, nx=False, ex=None):
        now = time.time()
        if nx and key in self._locks and self._locks[key] > now:
            return False
        self._locks[key] = (now + ex) if ex else 0
        return True

    def get(self, key):
        exp = self._locks.get(key)
        if exp is None:
            return None
        if exp and exp < time.time():
            self._locks.pop(key, None)
            return None
        return "1"

    def delete(self, key):
        return self._locks.pop(key, None) is not None

    def exists(self, key):
        return self.get(key) is not None

    def expire(self, key, seconds):
        if key in self._locks:
            self._locks[key] = time.time() + seconds
        return key in self._locks

    def lpush(self, key, value):
        self._lists.setdefault(key, []).insert(0, value)
        return True

    def brpop(self, key, timeout=5):
        q = self._lists.get(key) or []
        if q:
            return (key, q.pop())
        return None

    def lrange(self, key, start, end):
        q = self._lists.get(key) or []
        return q[start:end if end >= 0 else None]


def _enable_fake():
    fake = FakeRedisLocal()
    distributed._redis = fake
    distributed._redis_error = None
    return fake


def _cleanup(*tids):
    with _get_conn() as conn:
        for t in tids:
            conn.execute("DELETE FROM mini_tasks WHERE id=?", (t,))


def test_is_local_task_detection():
    """自动识别：本地文件操作意图 → True；普通抓取 → False。"""
    import app.services.mini_tasks as mt
    assert mt._is_local_task("在 D:/我的文档 创建 报告.txt") is True
    assert mt._is_local_task("创建一个 excel 文件保存到本地") is True
    assert mt._is_local_task("把 C:/照片 里的图片批量重命名") is True
    assert mt._is_local_task("生成一段视频") is False
    assert mt._is_local_task("抓取百度前10条结果导出excel") is False


def test_local_queue_isolated_by_user():
    """本地队列按用户隔离：A 的任务 B 领不到。"""
    fake = _enable_fake()
    try:
        assert distributed.enqueue_local_task("t-1", "user-A") is True
        assert distributed.enqueue_local_task("t-2", "user-B") is True
        assert distributed.dequeue_local_task("user-A", timeout=0.1) == "t-1"
        assert distributed.dequeue_local_task("user-A", timeout=0.1) is None
        assert distributed.dequeue_local_task("user-B", timeout=0.1) == "t-2"
    finally:
        distributed._redis = None


def test_submit_local_task_auto():
    """submit 自动识别本地任务 → 进本地队列 + 状态 local_queued。"""
    import app.services.mini_tasks as mt

    fake = _enable_fake()
    try:
        record = mt.submit("在 D:/test 创建 说明.txt", user_id="local_user")
        tid = record["id"]
        assert record["status"] == "local_queued"
        assert distributed.dequeue_local_task("local_user", timeout=0.1) == tid
        rec = _get_mini_task(tid)
        assert rec["status"] == "local_queued"
    finally:
        distributed._redis = None
        _cleanup()


def test_submit_normal_task_cloud():
    """普通任务（无本地意图）走云端队列。"""
    import app.services.mini_tasks as mt

    fake = _enable_fake()
    try:
        record = mt.submit("抓取网站数据导出excel", user_id="normal_user")
        tid = record["id"]
        assert distributed.task_in_queue(tid) is True
        assert distributed.dequeue_local_task("normal_user", timeout=0.1) is None
    finally:
        distributed._redis = None
        _cleanup()


def test_poll_report_flow():
    """poll 领取 → report 回传 → 云端标记 done + result 落库。"""
    import app.api.local_exec as local_api

    fake = _enable_fake()
    tid = "local-poll-%d" % int(time.time())
    try:
        _save_mini_task({
            "id": tid, "user_id": "poll_user", "requirement": "在本地创建 hello.txt",
            "url": "", "status": "local_queued", "message": "排队中",
            "created_at": time.time(), "result": None, "error": None,
            "image_paths": [], "data_paths": [],
        })
        distributed.enqueue_local_task(tid, "poll_user")

        user = {"id": "poll_user"}
        with patch("app.services.local_exec.chat_completion", return_value="print('hello')"):
            resp = asyncio_run(local_api.poll_local_task({}, user))
        assert resp["task"] is not None
        assert resp["task"]["task_id"] == tid
        assert resp["task"]["script"] == "print('hello')"

        rec = _get_mini_task(tid)
        assert rec["status"] == "running"

        resp2 = asyncio_run(local_api.report_local_task({
            "task_id": tid, "success": True, "stdout": "hello\n[OUTPUT_FILE] C:/local/hello.txt",
            "stderr": "", "exit_code": 0, "output_file": "C:/local/hello.txt",
        }, user))
        assert resp2["ok"] is True
        rec2 = _get_mini_task(tid)
        assert rec2["status"] == "done"
        assert rec2["result"]["local_execution"] is True
        assert rec2["result"]["output_file"] == "C:/local/hello.txt"
    finally:
        distributed._redis = None
        _cleanup(tid)


def test_report_failure_marks_failed():
    """report 失败 → 云端标记 failed + error。"""
    import app.api.local_exec as local_api

    fake = _enable_fake()
    tid = "local-fail-%d" % int(time.time())
    try:
        _save_mini_task({
            "id": tid, "user_id": "fail_user", "requirement": "任务",
            "url": "", "status": "running", "message": "执行中",
            "created_at": time.time(), "result": None, "error": None,
            "image_paths": [], "data_paths": [],
        })
        user = {"id": "fail_user"}
        resp = asyncio_run(local_api.report_local_task({
            "task_id": tid, "success": False, "stdout": "",
            "stderr": "Traceback: boom", "exit_code": 1, "output_file": "",
            "detail": "脚本报错",
        }, user))
        assert resp["ok"] is True
        rec = _get_mini_task(tid)
        assert rec["status"] == "failed"
        assert "脚本报错" in (rec["error"] or "")
    finally:
        distributed._redis = None
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