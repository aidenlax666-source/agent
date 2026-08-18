# -*- coding: utf-8 -*-
"""CRUD 数据层测试：提醒/监控 增改删（数据库层，不依赖网络）。"""
import asyncio

from app.database import (add_reminder, list_reminders, update_reminder, delete_reminder,
                          add_monitor, list_monitors, update_monitor, delete_monitor)


def test_reminder_crud():
    """提醒：增 → 查 → 改 → 删。"""
    async def run():
        uid = "test_rem_user"
        await add_reminder(uid, "08:00", "喝水")
        items = await list_reminders(uid)
        assert len(items) == 1
        rid = items[0]["id"]
        assert items[0]["time"] == "08:00" and items[0]["text"] == "喝水"

        ok = await update_reminder(uid, rid, time_str="09:30")
        assert ok
        items = await list_reminders(uid)
        assert items[0]["time"] == "09:30" and items[0]["text"] == "喝水"

        # 更新他人提醒（归属校验）
        ok = await update_reminder("someone_else", rid, text="hack")
        assert not ok

        assert await delete_reminder(uid, rid)
        assert len(await list_reminders(uid)) == 0

    asyncio.run(run())


def test_monitor_crud():
    """监控：增 → 查 → 改 → 删。"""
    async def run():
        uid = "test_mon_user"
        mid = await add_monitor(uid, "window", "微信", "", "提醒我", 30)
        items = await list_monitors(uid)
        assert len(items) == 1 and items[0]["id"] == mid

        ok = await update_monitor(uid, mid, keywords="浏览器", action_requirement="打开网页")
        assert ok
        items = await list_monitors(uid)
        assert items[0]["keywords"] == "浏览器" and items[0]["action_requirement"] == "打开网页"

        # 他人归属校验
        ok = await update_monitor("someone_else", mid, keywords="hack")
        assert not ok

        assert await delete_monitor(uid, mid)
        assert len(await list_monitors(uid)) == 0

    asyncio.run(run())
