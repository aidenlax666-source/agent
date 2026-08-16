from __future__ import annotations
"""文件上传接口 - 用户上传本地文件，保存到服务器，返回可处理的路径。"""

import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Request

from app.api.dependencies import get_current_user
from app.database import log_audit

router = APIRouter()

UPLOAD_DIR = Path(__file__).parent.parent.parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

MAX_UPLOAD_SIZE = 200 * 1024 * 1024  # 200 MB（视频较大）
# 匿名用户上传限速（防无限刷磁盘）：按 IP 每 30 秒最多 5 次
_anon_upload: dict[str, deque] = defaultdict(deque)
_ANON_UPLOAD_MAX = 5
_ANON_UPLOAD_WINDOW = 30


def _check_upload_rate(request: Request) -> None:
    peer = request.client.host if request.client else "unknown"
    xff = request.headers.get("x-forwarded-for")
    # 与 mini.py 一致：仅可信代理（本机回环）时信任 XFF
    if xff and peer in ("127.0.0.1", "::1"):
        peer = xff.split(",")[0].strip() or peer
    now = time.time()
    q = _anon_upload[peer]
    while q and now - q[0] > _ANON_UPLOAD_WINDOW:
        q.popleft()
    if len(q) >= _ANON_UPLOAD_MAX:
        raise HTTPException(429, "上传过于频繁，请稍后再试")
    q.append(now)
    if len(_anon_upload) > 10000:
        for k in list(_anon_upload)[:5000]:
            _anon_upload.pop(k, None)


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
    request: Request = None,
    user=Depends(get_current_user),
):
    """上传文件，保存到 uploads 目录，返回文件路径。

    前端拿到路径后，拼进需求，AI 生成的脚本读取这个路径的文件。
    """
    # 匿名用户按 IP 限速（防无限刷磁盘）
    if (user.get("email") or "").startswith("anon_"):
        _check_upload_rate(request)
    # 文件名清洗：只取 basename，去除可能的路径成分
    original_name = Path(file.filename or "file").name
    ext = Path(original_name).suffix.lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"不支持的文件类型: {ext or '(无扩展名)'}")

    # 读取并限制大小
    content = await file.read(MAX_UPLOAD_SIZE + 1)
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(413, "文件过大，最大支持 200MB")

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
