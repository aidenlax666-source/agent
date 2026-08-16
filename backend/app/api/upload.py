from __future__ import annotations
"""文件上传接口 - 用户上传本地文件，保存到服务器，返回可处理的路径。"""

import uuid
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends

from app.api.dependencies import get_current_user
from app.database import log_audit

router = APIRouter()

UPLOAD_DIR = Path(__file__).parent.parent.parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

MAX_UPLOAD_SIZE = 200 * 1024 * 1024  # 200 MB（视频较大）
ALLOWED_EXTENSIONS = {
    ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt",
    ".csv", ".txt", ".json", ".pdf", ".png", ".jpg", ".jpeg",
    # 视频/音频/字幕（视频剪辑技能）
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v",
    ".mp3", ".wav", ".m4a", ".aac", ".flac",
    ".srt", ".ass",
}


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    """上传文件，保存到 uploads 目录，返回文件路径。

    前端拿到路径后，拼进需求，AI 生成的脚本读取这个路径的文件。
    """
    # 文件名清洗：只取 basename，去除可能的路径成分
    original_name = Path(file.filename or "file").name
    ext = Path(original_name).suffix.lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"不支持的文件类型: {ext or '(无扩展名)'}")

    # 读取并限制大小
    content = await file.read(MAX_UPLOAD_SIZE + 1)
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(413, "文件过大，最大支持 20MB")

    # 生成唯一文件名，避免覆盖
    unique_name = f"{uuid.uuid4().hex[:12]}_{original_name}"
    save_path = UPLOAD_DIR / unique_name

    try:
        save_path.write_bytes(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传失败: {str(e)}")

    await log_audit(user["id"], "upload", original_name)

    return {
        "filename": original_name,
        "path": str(save_path),  # 完整路径，供脚本读取
        "size": len(content),
    }
