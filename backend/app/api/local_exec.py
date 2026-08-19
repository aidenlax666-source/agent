# -*- coding: utf-8 -*-
"""本地执行模式 API：用户本地 exe 端领取/回传任务（混合架构）。

端点（JWT 认证，只操作自己的本地队列）：
- POST /api/local/tasks/poll：从该用户本地队列取一个任务（BRPOP），
  懒生成脚本后返回给本地端执行
- POST /api/local/tasks/report：本地端回传执行结果（stdout/stderr/退出码/产物），
  云端标记 done/failed 并写回 result

单机模式（无 Redis）：poll 返回空（本地执行是云架构能力）。
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import get_current_user
from app.services import distributed as _dist

logger = logging.getLogger("app.api.local_exec")

router = APIRouter()


@router.post("/local/tasks/poll")
async def poll_local_task(data: dict | None = None, user=Depends(get_current_user)):
    """领取该用户的一个本地执行任务（BRPOP 短阻塞，无任务返回空）。

    本地端可长轮询：连续请求本接口直到拿到任务。领取时云端懒生成脚本。
    """
    user_id = user["id"]
    if not _dist.redis_enabled():
        return {"task": None, "reason": "single_node"}
    try:
        from app.services.mini_tasks import _task_record_from_db
        from app.services.local_exec import prepare_local_task
    except Exception as e:
        logger.warning("[local] 领取准备失败: %s", str(e)[:100])
        return {"task": None}

    # 短阻塞等自己的队列（最多 2 秒）
    task_id = _dist.dequeue_local_task(user_id, timeout=2.0)
    if not task_id:
        return {"task": None}

    # 从 DB 重建任务（拿到 requirement）
    rec = _task_record_from_db(task_id)
    if rec is None:
        # 任务记录丢失：直接丢弃（防僵尸任务循环领取）
        logger.warning("[local] 任务 %s 无 DB 记录，丢弃", task_id[:8])
        return {"task": None}

    # 懒生成脚本
    payload = await prepare_local_task(task_id, rec.get("requirement") or "")
    # 标记为执行中
    try:
        from app.database import update_mini_task as _umt
        await _umt(task_id, status="running", message="本地设备正在执行...")
    except Exception:
        pass
    logger.info("[local] 任务 %s 派发给用户 %s 的本地设备", task_id[:8], user_id[:8])
    return {"task": payload}


@router.post("/local/tasks/report")
async def report_local_task(data: dict, user=Depends(get_current_user)):
    """本地端回传执行结果：云端收尾（标记 done/failed + 写 result）。"""
    task_id = (data.get("task_id") or "").strip()
    if not task_id:
        raise HTTPException(status_code=400, detail="task_id 必填")
    success = bool(data.get("success"))
    stdout = (data.get("stdout") or "")[:50000]
    stderr = (data.get("stderr") or "")[:10000]
    exit_code = int(data.get("exit_code") or 0)
    output_file = data.get("output_file") or ""  # 本地端创建的产物路径
    detail = (data.get("detail") or "")[:300]

    try:
        from app.database import update_mini_task as _umt
        result = {
            "status": "ok" if success else "failed",
            "local_execution": True,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "output_file": output_file,
            "elapsed": 0,
        }
        if output_file:
            result["message_file"] = f"已在本地创建：{output_file}"
        await _umt(
            task_id,
            status="done" if success else "failed",
            message="本地执行完成" if success else f"本地执行失败: {detail or (stderr or stdout)[-200:]}",
            result=json.dumps(result, ensure_ascii=False),
            error=None if success else (detail or (stderr or stdout)[-300:] or None),
        )
        logger.info("[local] 任务 %s 本地执行%s（退出码 %s）", task_id[:8], "成功" if success else "失败", exit_code)
    except Exception as e:
        logger.warning("[local] 任务 %s 回传处理失败: %s", task_id[:8], str(e)[:100])
        raise HTTPException(status_code=500, detail=f"回传处理失败: {str(e)[:100]}")
    return {"ok": True}