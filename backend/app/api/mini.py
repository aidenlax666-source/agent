from __future__ import annotations
"""mini_generator 后台任务 API：提交自然语言任务 → 后台执行 → 查询状态/结果。"""
import os
import time as _time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.api.dependencies import get_current_user
from app.services import mini_tasks

router = APIRouter()

# 开发任务 API：zip 上限 50MB
MAX_DEV_ZIP_SIZE = 50 * 1024 * 1024
# 解压后总大小/成员数/单文件大小上限（防 zip 炸弹打爆磁盘/内存）
MAX_DEV_EXTRACT_TOTAL = 200 * 1024 * 1024
MAX_DEV_EXTRACT_FILES = 5000
MAX_DEV_EXTRACT_FILE = 20 * 1024 * 1024


@router.post("/dev/tasks")
async def dev_task(requirement: str = Form(...), file: UploadFile = File(...),
                   request: Request = None, user=Depends(get_current_user)):
    """开发任务 API（供 CLI/外部调用）：上传项目 zip + 需求 → AI 改码 → 返回 diff + 修改后文件 zip。

    返回: {dev_diff, dev_files, dev_summary, dev_modified_zip(base64), dev_diff_url}
    """
    import base64 as _b64
    import io as _io
    import tempfile
    import uuid as _uuid
    import zipfile as _zip

    requirement = (requirement or "").strip()
    if not requirement:
        raise HTTPException(status_code=400, detail="requirement 不能为空")
    if len(requirement) > MAX_REQUIREMENT_LEN:
        raise HTTPException(status_code=400, detail=f"requirement 过长（最大 {MAX_REQUIREMENT_LEN} 字）")
    content = await file.read(MAX_DEV_ZIP_SIZE + 1)
    if len(content) > MAX_DEV_ZIP_SIZE:
        raise HTTPException(status_code=413, detail="项目 zip 过大（最大 50MB）")

    # 安全解压：防 zip-slip 路径穿越
    tmp = _unzip_dev_project(content)
    await _charge_dev_credit(user, request)

    try:
        task_id = _uuid.uuid4().hex[:12]
        result = await mini_tasks._run_dev_task(task_id, requirement, code_dir=tmp)
        if result.get("status") != "ok":
            raise HTTPException(status_code=422, detail=result.get("error") or "开发任务失败")
        return {
            "task_id": task_id,
            "dev_summary": result.get("dev_summary"),
            "dev_files": result.get("dev_files"),
            "dev_diff": result.get("dev_diff"),
            "dev_diff_url": result.get("dev_diff_url"),
            "dev_modified_zip": result.get("dev_modified_zip"),
            "elapsed": result.get("elapsed"),
        }
    finally:
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)


def _unzip_dev_project(content: bytes) -> str:
    """安全解压用户上传的项目 zip（防 zip-slip 路径穿越 + 防 zip 炸弹），返回解压目录。

    限制：解压后总字节 ≤ MAX_DEV_EXTRACT_TOTAL、成员数 ≤ MAX_DEV_EXTRACT_FILES、
    单文件 ≤ MAX_DEV_EXTRACT_FILE。任何失败都会清理已创建的临时目录。
    """
    import io as _io
    import shutil as _shutil
    import tempfile
    import zipfile as _zip

    tmp = tempfile.mkdtemp(prefix="dev_api_")
    try:
        with _zip.ZipFile(_io.BytesIO(content)) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_DEV_EXTRACT_FILES:
                raise HTTPException(status_code=400, detail=f"项目 zip 文件数过多（最大 {MAX_DEV_EXTRACT_FILES} 个）")
            total = 0
            for member in infos:
                target = os.path.normpath(os.path.join(tmp, member.filename))
                if not target.startswith(tmp + os.sep) and target != tmp:
                    raise HTTPException(status_code=400, detail="项目 zip 包含非法路径")
                if member.file_size > MAX_DEV_EXTRACT_FILE:
                    raise HTTPException(status_code=400, detail=f"项目 zip 内单文件过大（最大 {MAX_DEV_EXTRACT_FILE // (1024 * 1024)}MB）")
                total += member.file_size
                if total > MAX_DEV_EXTRACT_TOTAL:
                    raise HTTPException(status_code=400, detail=f"项目 zip 解压后总大小超限（最大 {MAX_DEV_EXTRACT_TOTAL // (1024 * 1024)}MB）")
            zf.extractall(tmp)
    except HTTPException:
        _shutil.rmtree(tmp, ignore_errors=True)
        raise
    except Exception as e:
        _shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"zip 解压失败: {str(e)[:150]}")
    return tmp


async def _dev_zip_from_form(file: UploadFile) -> bytes:
    content = await file.read(MAX_DEV_ZIP_SIZE + 1)
    if len(content) > MAX_DEV_ZIP_SIZE:
        raise HTTPException(status_code=413, detail="项目 zip 过大（最大 50MB）")
    return content


async def _charge_dev_credit(user: dict, request: Request) -> int:
    """dev/qa 接口：匿名用户按 IP 限速 + 原子扣 1 积分（防无限刷 LLM 成本）。"""
    if (user.get("email") or "").startswith("anon_"):
        _check_anon_rate(_client_ip(request))
    from app.database import get_credits, try_decrement_credits
    if not await try_decrement_credits(user["id"], 1):
        raise HTTPException(status_code=402, detail="额度不足，无法执行该操作")
    return await get_credits(user["id"])


@router.post("/dev/plan")
async def dev_plan(requirement: str = Form(...), file: UploadFile = File(...),
                   feedback: str = Form(""), request: Request = None, user=Depends(get_current_user)):
    """交互式改码第一步：上传项目 zip + 需求 → AI 先出修改方案（不改代码）。

    返回: {status, plan, files(改动清单), questions(需用户确认的问题)}
    用户确认/提意见后调 /api/dev/apply 落地改动。
    """
    import uuid as _uuid

    requirement = (requirement or "").strip()
    if not requirement:
        raise HTTPException(status_code=400, detail="requirement 不能为空")
    if len(requirement) > MAX_REQUIREMENT_LEN:
        raise HTTPException(status_code=400, detail=f"requirement 过长（最大 {MAX_REQUIREMENT_LEN} 字）")
    tmp = _unzip_dev_project(await _dev_zip_from_form(file))
    await _charge_dev_credit(user, request)
    try:
        result = await mini_tasks._plan_dev_task(requirement, code_dir=tmp, feedback=(feedback or "").strip() or None)
        if result.get("status") != "ok":
            raise HTTPException(status_code=422, detail=result.get("error") or "方案生成失败")
        return {
            "plan_id": _uuid.uuid4().hex[:12],
            "plan": result.get("plan"),
            "files": result.get("files"),
            "questions": result.get("questions"),
        }
    finally:
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)


@router.post("/dev/apply")
async def dev_apply(requirement: str = Form(...), plan: str = Form(...),
                    file: UploadFile = File(...), feedback: str = Form(""),
                    request: Request = None, user=Depends(get_current_user)):
    """交互式改码第二步：按用户已确认的方案落地改动。

    返回: {task_id, dev_summary, dev_files, dev_diff, dev_diff_url, dev_modified_zip, elapsed}
    """
    import uuid as _uuid

    requirement = (requirement or "").strip()
    plan = (plan or "").strip()
    if not requirement:
        raise HTTPException(status_code=400, detail="requirement 不能为空")
    if not plan:
        raise HTTPException(status_code=400, detail="plan 不能为空（请先调用 /api/dev/plan）")
    if len(requirement) > MAX_REQUIREMENT_LEN:
        raise HTTPException(status_code=400, detail=f"requirement 过长（最大 {MAX_REQUIREMENT_LEN} 字）")
    tmp = _unzip_dev_project(await _dev_zip_from_form(file))
    await _charge_dev_credit(user, request)
    try:
        task_id = _uuid.uuid4().hex[:12]
        result = await mini_tasks._run_dev_task(task_id, requirement, code_dir=tmp,
                                                plan=plan, feedback=(feedback or "").strip() or None)
        if result.get("status") != "ok":
            raise HTTPException(status_code=422, detail=result.get("error") or "开发任务失败")
        return {
            "task_id": task_id,
            "dev_summary": result.get("dev_summary"),
            "dev_files": result.get("dev_files"),
            "dev_diff": result.get("dev_diff"),
            "dev_diff_url": result.get("dev_diff_url"),
            "dev_modified_zip": result.get("dev_modified_zip"),
            "elapsed": result.get("elapsed"),
        }
    finally:
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)

# 上传文件根目录：只允许引用此目录内的文件（防任意文件读取/外泄）
UPLOAD_DIR = Path(__file__).parent.parent.parent / "uploads"

# 需求描述长度上限（防 LLM 成本 DoS；20000 字足以容纳脚本/长需求）
MAX_REQUIREMENT_LEN = 20000

# 匿名用户提交限速：防止换 ID 无限刷积分/刷任务（内存表，按 IP）
_ANON_SUBMIT_RATE: dict[str, deque] = defaultdict(deque)
ANON_MAX_PER_MINUTE = 10
_ANON_RATE_MAX_KEYS = 10000  # 限速表键数上限（防伪造 XFF 无限填充内存）


def _client_ip(request: Request) -> str:
    """取客户端真实 IP：仅当直连来源是可信代理（本机回环/白名单）时才信任 XFF，否则用直连 IP。

    防伪造 X-Forwarded-For 绕过匿名限速：客户端可随意改 XFF 头，但只要
    直连 peer 不在可信白名单里，就按直连 IP 限速。
    """
    from app.config import get_settings

    peer = request.client.host if request.client else "unknown"
    xff = request.headers.get("x-forwarded-for")
    if not xff:
        return peer
    trusted = {s.strip().lower() for s in (get_settings().trusted_proxy_ips or "").split(",") if s.strip()}
    if peer.lower() not in trusted:
        return peer
    first = xff.split(",")[0].strip()
    return first if first else peer


def _check_anon_rate(ip: str) -> None:
    now = _time.time()
    if len(_ANON_SUBMIT_RATE) > _ANON_RATE_MAX_KEYS:
        # 键数超限：清空最旧一半（按插入顺序）
        for k in list(_ANON_SUBMIT_RATE.keys())[: _ANON_RATE_MAX_KEYS // 2]:
            _ANON_SUBMIT_RATE.pop(k, None)
    q = _ANON_SUBMIT_RATE[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= ANON_MAX_PER_MINUTE:
        raise HTTPException(status_code=429, detail="操作太频繁，请稍后再试")
    q.append(now)


def _check_url(url) -> str | None:
    """URL 只允许 http/https（防 file:// 等本地文件入口）。"""
    if url is None:
        return None
    if not isinstance(url, str):
        raise HTTPException(status_code=400, detail="url 必须是文本")
    url = url.strip()
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="url 仅支持 http/https 协议")
    return url


def _ensure_upload_path(path: str) -> str:
    """校验路径位于 uploads 目录内，防止读取服务器任意文件。"""
    if not path:
        return path
    try:
        resolved = Path(path).resolve()
        root = UPLOAD_DIR.resolve()
        if not (str(resolved).startswith(str(root) + os.sep) or resolved == root):
            raise HTTPException(status_code=400, detail="文件路径必须在上传目录内")
        if not resolved.is_file():
            raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
        return str(resolved)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="文件路径不合法")


@router.post("/mini/tasks")
async def create_task(data: dict, request: Request, user=Depends(get_current_user)):
    """提交一个自然语言自动化任务，立即返回 task_id（可带已上传图片路径）。"""
    if not isinstance(data.get("requirement"), str):
        raise HTTPException(status_code=400, detail="requirement 必须是文本")
    requirement = data.get("requirement").strip()
    if not requirement:
        raise HTTPException(status_code=400, detail="requirement 不能为空")
    if len(requirement) > MAX_REQUIREMENT_LEN:
        raise HTTPException(status_code=400, detail=f"需求描述过长（最多 {MAX_REQUIREMENT_LEN} 字）")

    # 匿名用户按 IP 限速（登录用户不受限），防换 ID 刷积分
    if (user.get("email") or "").startswith("anon_"):
        _check_anon_rate(_client_ip(request))

    # 先完成全部入参校验（url/图片/数据路径），通过后再扣积分，避免校验失败白扣分
    url = _check_url(data.get("url"))
    image_paths = data.get("image_paths") or []
    if not isinstance(image_paths, list):
        image_paths = []
    # 图片路径必须是上传目录内的文件（防任意文件被读取/外泄到豆包 API）
    image_paths = [_ensure_upload_path(p) for p in image_paths]
    data_paths = data.get("data_paths") or []
    if not isinstance(data_paths, list):
        data_paths = []
    # 数据文件必须是上传目录内的文件（防任意文件读取）
    data_paths = [_ensure_upload_path(p) for p in data_paths]

    # 额度校验：原子扣减（防并发竞态），余额不足返回 402
    from app.database import (get_credits, try_decrement_credits, add_reminder as _db_add_reminder,
                              add_monitor as _db_add_monitor, update_mini_task as _db_update_task,
                              add_credits as _db_add_credits)
    if not await try_decrement_credits(user["id"], 1):
        raise HTTPException(status_code=402, detail="额度不足，无法提交任务")
    credits = await get_credits(user["id"])

    # 自动化意图解析（提醒 / 监控 / 循环执行，纯正则零成本）
    auto = mini_tasks.parse_automation(requirement)

    # ---- 定时提醒：创建提醒项 + 落一条"设置型"历史记录（不跑任务引擎）----
    if auto.get("kind") == "reminder" and auto.get("reminders"):
        # 单请求提醒条数上限（防正则批量灌入）
        reminders = auto["reminders"][:20]
        try:
            record = mini_tasks.submit(requirement, url, user["id"], image_paths=image_paths,
                                       data_paths=data_paths, skip_run=True)
            for it in reminders:
                await _db_add_reminder(user["id"], it["time"], it["text"], source_task=record["id"])
            import json as _json
            await _db_update_task(record["id"], status="done", message="已设置定时提醒",
                                  result=_json.dumps({"status": "ok", "kind": "reminder",
                                                      "reminders": reminders}, ensure_ascii=False))
        except Exception as e:
            await _db_add_credits(user["id"], 1)  # 创建失败补回积分（原子性）
            raise HTTPException(status_code=500, detail=f"创建定时提醒失败: {str(e)[:120]}")
        return {
            "task_id": record["id"], "status": "done", "message": f"已设置 {len(reminders)} 条定时提醒",
            "credits_left": credits, "automation": "reminder", "reminders": reminders,
        }

    # ---- 监控任务：创建监控项 + 落"设置型"历史记录 ----
    if auto.get("kind") == "monitor" and auto.get("monitor"):
        try:
            record = mini_tasks.submit(requirement, url, user["id"], image_paths=image_paths,
                                       data_paths=data_paths, skip_run=True)
            m = auto["monitor"]
            mid = await _db_add_monitor(user["id"], m["type"], m["keywords"], m["condition"],
                                        m["action_requirement"], m["check_interval"], source_task=record["id"])
            import json as _json
            await _db_update_task(record["id"], status="done", message="已设置监控任务",
                                  result=_json.dumps({"status": "ok", "kind": "monitor",
                                                      "monitor_id": mid, "monitor": m}, ensure_ascii=False))
        except Exception as e:
            await _db_add_credits(user["id"], 1)  # 创建失败补回积分
            raise HTTPException(status_code=500, detail=f"创建监控任务失败: {str(e)[:120]}")
        return {
            "task_id": record["id"], "status": "done", "message": "已设置监控任务",
            "credits_left": credits, "automation": "monitor", "monitor": {**m, "id": mid},
        }

    # ---- 普通任务（可带显式 schedule 或自然语言解析出的循环执行）----
    schedule = auto.get("schedule") or data.get("schedule")
    sched_dict = schedule if isinstance(schedule, dict) and schedule.get("type") in ("interval", "daily") else None
    record = mini_tasks.submit(requirement, url, user["id"], image_paths=image_paths,
                               data_paths=data_paths, schedule=sched_dict)
    automation_note = ""
    if sched_dict:
        automation_note = f"{sched_dict['type']}:{sched_dict.get('value')}"
    return {
        "task_id": record["id"],
        "status": record["status"],
        "message": record["message"],
        "credits_left": credits,
        "automation": "schedule" if automation_note else "task",
        "schedule": automation_note or None,
    }


# ============================================================
# 站内通知（定时提醒 / 监控触发）
# ============================================================

@router.get("/notifications")
async def get_notifications(limit: int = 20, user=Depends(get_current_user)):
    from app.database import list_notifications, unread_notification_count
    items = await list_notifications(user["id"], min(max(limit, 1), 50))
    unread = await unread_notification_count(user["id"])
    return {"items": items, "unread": unread}


@router.post("/notifications/read")
async def mark_notifications_read(data: dict, user=Depends(get_current_user)):
    from app.database import mark_notifications_read as _db_mark
    ids = data.get("ids")
    await _db_mark(user["id"], ids if isinstance(ids, list) else None)
    return {"ok": True}


# ============================================================
# 定时提醒 / 监控任务管理
# ============================================================

@router.get("/automations")
async def list_automations(user=Depends(get_current_user)):
    from app.database import list_reminders, list_monitors
    reminders = await list_reminders(user["id"])
    monitors = await list_monitors(user["id"])
    return {"reminders": reminders, "monitors": monitors}


@router.post("/reminders")
async def create_reminder(data: dict, user=Depends(get_current_user)):
    """直接创建定时提醒：{time: "HH:MM", text: "内容"}"""
    import re as _re
    from app.database import add_reminder as _db_add_rem
    t = str(data.get("time") or "").strip()
    text = str(data.get("text") or "").strip()
    if not _re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
        raise HTTPException(status_code=400, detail="time 需要 HH:MM 格式（如 08:30）")
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")
    await _db_add_rem(user["id"], t, text)
    return {"ok": True, "time": t, "text": text}


@router.post("/monitors")
async def create_monitor(data: dict, user=Depends(get_current_user)):
    """直接创建监控任务：{type: window|screen, keywords?, condition?, action_requirement?, check_interval?}"""
    from app.database import add_monitor as _db_add_mon
    mtype = str(data.get("type") or "").strip()
    if mtype not in ("window", "screen"):
        raise HTTPException(status_code=400, detail="type 只能是 window（软件/窗口）或 screen（屏幕变化）")
    keywords = str(data.get("keywords") or "").strip()
    condition = str(data.get("condition") or "").strip()
    action = str(data.get("action_requirement") or "").strip()
    try:
        interval = int(data.get("check_interval") or 60)
        interval = max(5, min(interval, 3600))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="check_interval 需要 5~3600 的整数（秒）")
    if mtype == "window" and not keywords:
        raise HTTPException(status_code=400, detail="window 监控需要 keywords（窗口标题关键词）")
    mid = await _db_add_mon(user["id"], mtype, keywords, condition, action, interval)
    return {"ok": True, "id": mid, "type": mtype, "keywords": keywords,
            "condition": condition, "action_requirement": action, "check_interval": interval}


@router.delete("/reminders/{rid}")
async def delete_reminder(rid: str, user=Depends(get_current_user)):
    from app.database import delete_reminder as _db_del_rem
    # 幂等：任务不存在也返回 ok（前端 stale 列表重复点击不会 404 报错）
    await _db_del_rem(user["id"], rid)
    return {"ok": True}


@router.post("/reminders/{rid}/toggle")
async def toggle_reminder(rid: str, user=Depends(get_current_user)):
    from app.database import toggle_reminder as _db_toggle
    enabled = await _db_toggle(user["id"], rid)
    if enabled is None:
        # 幂等：不存在返回当前无效状态，前端据此刷新列表
        return {"ok": True, "enabled": False, "missing": True}
    return {"ok": True, "enabled": enabled}


@router.delete("/monitors/{mid}")
async def delete_monitor(mid: str, user=Depends(get_current_user)):
    from app.database import delete_monitor as _db_del_mon
    # 幂等：不存在也返回 ok
    await _db_del_mon(user["id"], mid)
    return {"ok": True}


@router.post("/monitors/{mid}/toggle")
async def toggle_monitor(mid: str, user=Depends(get_current_user)):
    from app.database import toggle_monitor as _db_toggle
    enabled = await _db_toggle(user["id"], mid)
    if enabled is None:
        return {"ok": True, "enabled": False, "missing": True}
    return {"ok": True, "enabled": enabled}


@router.get("/mini/tasks")
async def list_all(limit: int = 20, user=Depends(get_current_user)):
    return {"tasks": mini_tasks.list_tasks(limit=limit, user_id=user["id"])}


@router.get("/mini/tasks/{task_id}")
async def task_status(task_id: str, user=Depends(get_current_user)):
    """查询任务状态（仅本人可见，账号数据独立）。"""
    status = mini_tasks.get_status(task_id, user_id=user["id"])
    if status is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return status


@router.post("/mini/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, user=Depends(get_current_user)):
    ok = mini_tasks.cancel_task(task_id, user["id"])
    return {"cancelled": ok}


@router.post("/mini/tasks/{task_id}/iterate")
async def iterate_task(task_id: str, data: dict, request: Request, user=Depends(get_current_user)):
    """对已完成任务提修改意见，同一任务原地迭代重跑。"""
    feedback = (data.get("feedback") or "").strip()
    if not feedback:
        raise HTTPException(status_code=400, detail="feedback 不能为空")
    if len(feedback) > MAX_REQUIREMENT_LEN:
        raise HTTPException(status_code=400, detail=f"修改意见过长（最多 {MAX_REQUIREMENT_LEN} 字）")
    # 迭代会重跑完整 LLM 管线：匿名限速 + 扣 1 积分（防无限免费重跑）
    await _charge_dev_credit(user, request)
    result = mini_tasks.iterate(task_id, feedback, user["id"])
    if result is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if result.get("error"):
        raise HTTPException(status_code=409, detail=result["error"])
    return {"task_id": task_id, "status": result.get("status"), "message": result.get("message", "迭代修改已提交")}


@router.post("/mini/tasks/{task_id}/confirm")
async def confirm_task(task_id: str, user=Depends(get_current_user)):
    """确认任务结果（满意，结果即最终版）。"""
    result = mini_tasks.confirm_task(task_id, user["id"])
    if result is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return result


@router.post("/mini/tasks/{task_id}/schedule")
async def schedule_task(task_id: str, data: dict, user=Depends(get_current_user)):
    """设置任务定时执行：{schedule_type: interval|daily, schedule_value: 分钟数|HH:MM, enabled: bool}"""
    raw_enabled = data.get("enabled", True)
    if isinstance(raw_enabled, bool):
        enabled = raw_enabled
    else:
        enabled = str(raw_enabled).strip().lower() in ("1", "true", "yes", "on")  # "false"→False
    result = mini_tasks.schedule_task(
        task_id,
        data.get("schedule_type", "interval"),
        data.get("schedule_value", ""),
        enabled,
        user["id"],
    )
    if result is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


QA_SYSTEM_PROMPT = """你是一位数据分析师。根据用户上传的数据文件的摘要信息，回答用户的自然语言问题。

【回答要求】
1. 基于给出的数据摘要（列名、行数、示例数据、统计信息）作答
2. 如果问题需要具体计算（求和/平均/最大等），基于摘要中能获取的数据估算，并说明"基于示例数据"
3. 中文回答，简洁清晰，必要时给出关键数字
4. 摘要信息不足时，明确说明还缺什么数据

只输出回答内容，不要解释过程。"""


@router.post("/mini/qa")
async def data_qa(data: dict, request: Request, user=Depends(get_current_user)):
    """上传的 Excel/CSV 数据 → 自然语言问答分析。"""
    import json as _json
    import os as _os

    file_path = data.get("file_path")
    question = data.get("question")
    if not isinstance(file_path, str) or not isinstance(question, str):
        raise HTTPException(status_code=400, detail="file_path 和 question 必须是文本")
    file_path = file_path.strip()
    question = question.strip()
    if not file_path or not question:
        raise HTTPException(status_code=400, detail="file_path 和 question 不能为空")
    if len(question) > MAX_REQUIREMENT_LEN:
        raise HTTPException(status_code=400, detail=f"问题过长（最多 {MAX_REQUIREMENT_LEN} 字）")
    # 只能分析上传目录内的文件（防任意文件读取）
    file_path = _ensure_upload_path(file_path)
    # 匿名限速 + 扣 1 积分（防无限刷 LLM 成本）
    await _charge_dev_credit(user, request)

    # 1. 读取数据摘要（线程池避免阻塞）
    import asyncio as _a
    import pandas as pd

    def _load_summary():
        try:
            if file_path.lower().endswith((".xlsx", ".xls")):
                df = pd.read_excel(file_path)
            elif file_path.lower().endswith(".csv"):
                df = pd.read_csv(file_path)
            else:
                raise ValueError("仅支持 Excel/CSV")
        except Exception as e:
            return {"error": f"读取失败: {str(e)[:200]}"}
        summary = {
            "rows": int(len(df)),
            "columns": list(df.columns),
            "head": df.head(8).fillna("").to_dict(orient="records"),
            "describe": df.describe(include="all").fillna("").to_dict() if len(df) > 0 else {},
        }
        return summary

    loop = _a.get_running_loop()
    summary = await loop.run_in_executor(None, _load_summary)
    if summary.get("error"):
        raise HTTPException(status_code=400, detail=summary["error"])

    # 2. LLM 回答
    from app.services.llm_client import chat_completion
    user_prompt = (
        f"【数据摘要】\n{_json.dumps(summary, ensure_ascii=False, default=str)[:4000]}\n\n"
        f"【用户问题】{question}"
    )
    try:
        answer = await chat_completion(QA_SYSTEM_PROMPT, user_prompt, temperature=0.3, max_tokens=800)
    except Exception as e:
        answer = f"分析失败: {str(e)[:200]}"

    return {"answer": answer, "summary": {"rows": summary["rows"], "columns": summary["columns"]}}


@router.get("/mini/tasks/{task_id}/download")
async def download_output(task_id: str, user=Depends(get_current_user)):
    """下载任务产出的结果文件（仅本人可见）。

    安全：产物 HTML 是 LLM/网页抓取内容拼接的不可信数据，一律强制下载 +
    nosniff，禁止在 API 源内联渲染（防存储型 XSS 偷取同源 localStorage JWT）。
    """
    status = mini_tasks.get_status(task_id, user_id=user["id"])
    if status is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    result = status.get("result") or {}
    path = result.get("output_file")
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="该任务没有可下载的结果文件")
    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    if ext in (".html", ".htm", ".svg", ".xml"):
        # 不可信标记语言：强制下载 + 非 HTML 媒体类型（防内联执行）
        return FileResponse(path, media_type="application/octet-stream",
                            headers={"X-Content-Type-Options": "nosniff",
                                     "Content-Disposition": f'attachment; filename="{filename}"'})
    media = {
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".png": "image/png",
        ".pdf": "application/pdf",
        ".csv": "text/csv; charset=utf-8",
        ".mp4": "video/mp4",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".dxf": "application/dxf",
    }.get(ext, "application/octet-stream")
    return FileResponse(path, media_type=media, filename=filename,
                        headers={"X-Content-Type-Options": "nosniff"})
