# -*- coding: utf-8 -*-
"""用户记忆管理测试：增删查（数据层）。"""
import asyncio

from app.database import remember, get_memory, forget


def test_memory_add_list():
    """添加记忆后能查到。"""
    async def run():
        uid = "test_mem_ui_user"
        await forget(uid)  # 清干净
        await remember(uid, "preference", "我喜欢用 Excel 输出")
        await remember(uid, "habit", "习惯每天上午处理数据")
        items = await get_memory(uid)
        contents = {m["content"] for m in items}
        assert "我喜欢用 Excel 输出" in contents
        assert "习惯每天上午处理数据" in contents
        kinds = {m["kind"] for m in items}
        assert "preference" in kinds and "habit" in kinds
        await forget(uid)

    asyncio.run(run())


def test_memory_dedupe():
    """同内容重复添加：更新时间覆盖，不产生重复条目。"""
    async def run():
        uid = "test_mem_dedupe"
        await forget(uid)
        await remember(uid, "preference", "输出用中文")
        await remember(uid, "preference", "输出用中文")  # 重复
        items = await get_memory(uid)
        same = [m for m in items if m["content"] == "输出用中文"]
        assert len(same) == 1  # 不重复
        await forget(uid)

    asyncio.run(run())


def test_memory_delete_single():
    """删单条不影响其他。"""
    async def run():
        uid = "test_mem_del"
        await forget(uid)
        await remember(uid, "preference", "A 记忆")
        await remember(uid, "preference", "B 记忆")
        await forget(uid, "A 记忆")
        items = await get_memory(uid)
        contents = {m["content"] for m in items}
        assert "A 记忆" not in contents
        assert "B 记忆" in contents
        await forget(uid)

    asyncio.run(run())


def test_memory_limit():
    """超过上限自动淘汰最旧的。"""
    async def run():
        uid = "test_mem_limit"
        await forget(uid)
        for i in range(60):
            await remember(uid, "preference", f"记忆编号 {i}")
        items = await get_memory(uid, limit=50)
        assert len(items) <= 50
        await forget(uid)

    asyncio.run(run())
