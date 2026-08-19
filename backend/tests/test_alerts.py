# -*- coding: utf-8 -*-
"""系统告警测试：去重、通知合并、阈值判断。"""
import time
from unittest.mock import patch

from app.config import get_settings
from app.services import distributed
from app.database import _init_db, _get_conn, _add_notification, _list_notifications, _unread_notification_count

_init_db()


class FakeRedisAlert:
    """内存版 Redis：告警用到的 dedup/worker/queue/leader。"""
    def __init__(self):
        self._data: dict = {}
        self._lists: dict = {}

    def ping(self):
        return True

    def set(self, key, value, nx=False, ex=None):
        now = time.time()
        if nx and key in self._data and self._data[key][1] > now:
            return False
        self._data[key] = (value, (now + ex) if ex else 0)
        return True

    def get(self, key):
        item = self._data.get(key)
        if item is None:
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
        if key in self._data:
            self._data[key] = (self._data[key][0], time.time() + seconds)
        return key in self._data

    def keys(self, pattern):
        prefix = pattern.replace("*", "")
        return [k for k in self._data if k.startswith(prefix)]

    def lpush(self, key, value):
        self._lists.setdefault(key, []).insert(0, value)
        return True

    def llen(self, key):
        return len(self._lists.get(key) or [])

    def lrange(self, key, start, end):
        q = self._lists.get(key) or []
        return q[start:end if end >= 0 else None]

    def brpop(self, key, timeout=5):
        q = self._lists.get(key) or []
        if q:
            return (key, q.pop())
        return None


def _enable_fake():
    fake = FakeRedisAlert()
    distributed._redis = fake
    distributed._redis_error = None
    return fake


def _cleanup():
    with _get_conn() as conn:
        conn.execute("DELETE FROM notifications WHERE user_id='system'")
        conn.execute("DELETE FROM notifications WHERE user_id='alert_user'")


def test_system_notifications_merged():
    """系统告警（user_id=system）合并进每个用户的通知列表。"""
    _cleanup()
    try:
        _add_notification("alert_user", "我的消息", "私人内容")
        _add_notification("system", "⚠️ 系统告警", "worker 失联")
        items = _list_notifications("alert_user", limit=20)
        titles = {i["title"] for i in items}
        assert "我的消息" in titles
        assert "⚠️ 系统告警" in titles
        # 未读数也包含系统通知
        assert _unread_notification_count("alert_user") >= 2
    finally:
        _cleanup()


def test_alert_dedup():
    """同类告警 interval 内只发一次（dedup）。"""
    import app.services.mini_tasks as mt

    fake = _enable_fake()
    settings_p = patch.object(get_settings(), "alert_enabled", True)
    settings_p2 = patch.object(get_settings(), "alert_min_interval", 1800)
    settings_p.start()
    settings_p2.start()
    sent = {"count": 0}
    try:
        # 直接测 _alert：第一次发，第二次（去重窗口内）不发
        from app.database import _add_notification as real_add

        def fake_add(user_id, title, content=""):
            sent["count"] += 1
            real_add(user_id, title, content)

        with patch("app.database._add_notification", side_effect=fake_add):
            mt._alert("queue_backlog", "标题", "内容", get_settings(), distributed)
            mt._alert("queue_backlog", "标题", "内容", get_settings(), distributed)
        assert sent["count"] == 1, "同类告警 interval 内应只发一次"
        # 不同 key 可再发
        with patch("app.database._add_notification", side_effect=fake_add):
            mt._alert("worker_down", "标题2", "内容2", get_settings(), distributed)
        assert sent["count"] == 2
    finally:
        settings_p.stop()
        settings_p2.stop()
        distributed._redis = None
        _cleanup()


def test_alert_disabled_loop_returns():
    """alert_enabled=False 时循环直接返回（不启动）。"""
    import app.services.mini_tasks as mt

    p = patch.object(get_settings(), "alert_enabled", False)
    p.start()
    try:
        loop = mt.system_monitor_loop()
        # 首轮 await 即应返回（内部检查 enabled 后 return）
        import asyncio
        try:
            asyncio.run(asyncio.wait_for(loop, timeout=2))
        except asyncio.TimeoutError:
            raise AssertionError("alert_enabled=False 时循环应直接返回而不是空转")
    finally:
        p.stop()


def test_alert_trigger_worker_down():
    """Redis 模式 0 活跃 worker → worker_down 告警发出。"""
    import app.services.mini_tasks as mt

    fake = _enable_fake()
    settings_p = patch.object(get_settings(), "alert_enabled", True)
    settings_p.start()
    sent = {"count": 0}
    try:
        from app.database import _add_notification as real_add

        def fake_add(user_id, title, content=""):
            sent["count"] += 1
            real_add(user_id, title, content)

        # 无 worker 心跳 → list_workers 为空
        assert distributed.list_workers() == []
        with patch("app.database._add_notification", side_effect=fake_add):
            mt._alert("worker_down", "⚠️ Worker 失联", "没有活跃 worker", get_settings(), distributed)
        assert sent["count"] == 1
        # DB 里有一条 system 通知
        from app.database import _get_conn
        with _get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM notifications WHERE user_id='system'").fetchone()
        assert row["c"] >= 1
    finally:
        settings_p.stop()
        distributed._redis = None
        _cleanup()
