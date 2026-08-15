from __future__ import annotations
"""mini_generator 后台任务 API：提交自然语言任务 → 后台执行 → 查询状态/结果。"""
import os
import time as _time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.api.dependencies import get_current_user
from app.services import mini_tasks

router = APIRouter()

# 上传文件根目录：只允许引用此目录内的文件（防任意文件读取/外泄）
UPLOAD_DIR = Path(__file__).parent.parent.parent / "uploads"

# 需求描述长度上限（防 LLM 成本 DoS）
MAX_REQUIREMENT_LEN = 2000

# 匿名用户提交限速：防止换 ID 无限刷积分/刷任务（内存表，按 IP）
_ANON_SUBMIT_RATE: dict[str, deque] = defaultdict(deque)
ANON_MAX_PER_MINUTE = 10
_ANON_RATE_MAX_KEYS = 10000  # 限速表键数上限（防伪造 XFF 无限填充内存）


def _client_ip(request: Request) -> str:
    """取客户端真实 IP：优先 X-Forwarded-For（cloudflared 等隧道/代理场景），否则直连 IP。"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


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


def _check_url(url: str) -> str | None:
    """URL 只允许 http/https（防 file:// 等本地文件入口）。"""
    url = (url or "").strip()
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
    requirement = (data.get("requirement") or "").strip()
    if not requirement:
        raise HTTPException(status_code=400, detail="requirement 不能为空")
    if len(requirement) > MAX_REQUIREMENT_LEN:
        raise HTTPException(status_code=400, detail=f"需求描述过长（最多 {MAX_REQUIREMENT_LEN} 字）")

    # 匿名用户按 IP 限速（登录用户不受限），防换 ID 刷积分
    if (user.get("email") or "").startswith("anon_"):
        _check_anon_rate(_client_ip(request))

    # 额度校验：原子扣减（防并发竞态），余额不足返回 402
    from app.database import get_credits, try_decrement_credits
    if not await try_decrement_credits(user["id"], 1):
        raise HTTPException(status_code=402, detail="额度不足，无法提交任务")
    credits = await get_credits(user["id"])

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
    record = mini_tasks.submit(requirement, url, user["id"], image_paths=image_paths, data_paths=data_paths)
    return {
        "task_id": record["id"],
        "status": record["status"],
        "message": record["message"],
        "credits_left": credits,
    }


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
async def iterate_task(task_id: str, data: dict, user=Depends(get_current_user)):
    """对已完成任务提修改意见，同一任务原地迭代重跑。"""
    feedback = (data.get("feedback") or "").strip()
    if not feedback:
        raise HTTPException(status_code=400, detail="feedback 不能为空")
    if len(feedback) > MAX_REQUIREMENT_LEN:
        raise HTTPException(status_code=400, detail=f"修改意见过长（最多 {MAX_REQUIREMENT_LEN} 字）")
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
    result = mini_tasks.schedule_task(
        task_id,
        data.get("schedule_type", "interval"),
        data.get("schedule_value", ""),
        bool(data.get("enabled", True)),
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
async def data_qa(data: dict, user=Depends(get_current_user)):
    """上传的 Excel/CSV 数据 → 自然语言问答分析。"""
    import json as _json
    import os as _os

    file_path = (data.get("file_path") or "").strip()
    question = (data.get("question") or "").strip()
    if not file_path or not question:
        raise HTTPException(status_code=400, detail="file_path 和 question 不能为空")
    if len(question) > MAX_REQUIREMENT_LEN:
        raise HTTPException(status_code=400, detail=f"问题过长（最多 {MAX_REQUIREMENT_LEN} 字）")
    # 只能分析上传目录内的文件（防任意文件读取）
    file_path = _ensure_upload_path(file_path)

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
    """下载任务产出的结果文件（仅本人可见）。"""
    status = mini_tasks.get_status(task_id, user_id=user["id"])
    if status is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    result = status.get("result") or {}
    path = result.get("output_file")
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="该任务没有可下载的结果文件")
    filename = os.path.basename(path)
    media = {
        ".html": "text/html; charset=utf-8",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".png": "image/png",
        ".pdf": "application/pdf",
        ".csv": "text/csv; charset=utf-8",
    }.get(os.path.splitext(filename)[1].lower(), "application/octet-stream")
    return FileResponse(path, media_type=media, filename=filename)
