from __future__ import annotations
"""mini_generator 后台任务 API：提交自然语言任务 → 后台执行 → 查询状态/结果。"""
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.api.dependencies import get_current_user
from app.services import mini_tasks

router = APIRouter()


@router.post("/mini/tasks")
async def create_task(data: dict, user=Depends(get_current_user)):
    """提交一个自然语言自动化任务，立即返回 task_id（可带已上传图片路径）。"""
    requirement = (data.get("requirement") or "").strip()
    if not requirement:
        raise HTTPException(status_code=400, detail="requirement 不能为空")

    # 额度校验（登录/匿名用户默认 10 credits）
    from app.database import get_credits, decrement_credits
    credits = await get_credits(user["id"])
    if credits <= 0:
        raise HTTPException(status_code=402, detail="额度不足，无法提交任务")
    await decrement_credits(user["id"], 1)

    url = (data.get("url") or "").strip() or None
    image_paths = data.get("image_paths") or []
    if not isinstance(image_paths, list):
        image_paths = []
    data_paths = data.get("data_paths") or []
    if not isinstance(data_paths, list):
        data_paths = []
    record = mini_tasks.submit(requirement, url, user["id"], image_paths=image_paths, data_paths=data_paths)
    return {
        "task_id": record["id"],
        "status": record["status"],
        "message": record["message"],
        "credits_left": max(credits - 1, 0),
    }


@router.get("/mini/tasks")
async def list_all(limit: int = 20, user=Depends(get_current_user)):
    return {"tasks": mini_tasks.list_tasks(limit=limit, user_id=user["id"])}


@router.get("/mini/tasks/{task_id}")
async def task_status(task_id: str):
    status = mini_tasks.get_status(task_id)
    if status is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return status


@router.post("/mini/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    ok = mini_tasks.cancel_task(task_id)
    return {"cancelled": ok}


@router.post("/mini/tasks/{task_id}/iterate")
async def iterate_task(task_id: str, data: dict, user=Depends(get_current_user)):
    """对已完成任务提修改意见，同一任务原地迭代重跑。"""
    feedback = (data.get("feedback") or "").strip()
    if not feedback:
        raise HTTPException(status_code=400, detail="feedback 不能为空")
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
    if not _os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"文件不存在: {file_path}")

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
async def download_output(task_id: str):
    """下载任务产出的结果文件（report.html / output.xlsx 等）。"""
    status = mini_tasks.get_status(task_id)
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
