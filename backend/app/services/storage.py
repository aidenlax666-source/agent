# -*- coding: utf-8 -*-
from __future__ import annotations

"""产物存储抽象层：本地/共享卷/S3 可切换。

产物（web/ 下的视频/图片/报告/游戏等）的存取统一走这里：
- 本地模式（默认，STORAGE_BACKEND 为空或 local）：直接读写 web/ 目录
  （单机或共享卷挂载，现状行为完全不变）
- S3 模式（STORAGE_BACKEND=s3 + S3_* 配置）：产物在对象存储，
  跨实例天然一致、不限磁盘、CDN 友好；流式/下载走 Range 请求代理

生产建议：单机用本地；多实例共享卷用 local（挂载 NFS/EFS）；
不想挂共享卷/要无限容量用 s3。
"""

import io
import os

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.paths import web_root


def _backend() -> str:
    from app.config import get_settings
    return (get_settings().storage_backend or "local").strip().lower()


# ============================================================
# 本地模式（默认）
# ============================================================

def _local_path(rel: str) -> str:
    """产物路径 → 绝对路径（防路径穿越）。

    rel 若是绝对路径（本地模式产物在 web/ 外，如沙箱临时输出）直接使用；
    否则 join 到 web/ 根下，且只允许 web/ 内（防路径穿越）。
    """
    if os.path.isabs(rel):
        return os.path.normpath(rel)
    root = os.path.normpath(web_root())
    p = os.path.normpath(os.path.join(root, rel.lstrip("/")))
    if not (p == root or p.startswith(root + os.sep)):
        raise HTTPException(status_code=400, detail="产物路径不合法")
    return p


def _local_exists(rel: str) -> bool:
    return os.path.isfile(_local_path(rel))


def _local_size(rel: str) -> int:
    return os.path.getsize(_local_path(rel))


def _local_read(rel: str) -> bytes:
    with open(_local_path(rel), "rb") as f:
        return f.read()


def _local_iter(rel: str, start: int, end: int, chunk_size: int = 256 * 1024):
    path = _local_path(rel)
    with open(path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            data = f.read(min(chunk_size, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


# ============================================================
# S3 模式（可选）
# ============================================================

def _s3_client():
    import boto3  # 可选依赖：STORAGE_BACKEND=s3 时才需要
    from app.config import get_settings
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint or None,
        aws_access_key_id=s.s3_access_key or None,
        aws_secret_access_key=s.s3_secret_key or None,
        region_name=s.s3_region or None,
    )


def _s3_bucket() -> str:
    from app.config import get_settings
    return get_settings().s3_bucket


def _s3_key(rel: str) -> str:
    return rel.lstrip("/")


def _s3_exists(rel: str) -> bool:
    try:
        _s3_client().head_object(Bucket=_s3_bucket(), Key=_s3_key(rel))
        return True
    except Exception:
        return False


def _s3_size(rel: str) -> int:
    try:
        r = _s3_client().head_object(Bucket=_s3_bucket(), Key=_s3_key(rel))
        return int(r.get("ContentLength", 0))
    except Exception:
        return 0


def _s3_read(rel: str) -> bytes:
    r = _s3_client().get_object(Bucket=_s3_bucket(), Key=_s3_key(rel))
    return r["Body"].read()


def _s3_iter(rel: str, start: int, end: int, chunk_size: int = 256 * 1024):
    # Range 请求：S3 原生支持，边下边播
    r = _s3_client().get_object(
        Bucket=_s3_bucket(), Key=_s3_key(rel),
        Range=f"bytes={start}-{end}",
    )
    body = r["Body"]
    while True:
        chunk = body.read(chunk_size)
        if not chunk:
            break
        yield chunk


# ============================================================
# 统一接口
# ============================================================

def artifact_exists(rel: str) -> bool:
    if _backend() == "s3":
        return _s3_exists(rel)
    return _local_exists(rel)


def artifact_size(rel: str) -> int:
    if _backend() == "s3":
        return _s3_size(rel)
    return _local_size(rel)


def artifact_read(rel: str) -> bytes:
    if _backend() == "s3":
        return _s3_read(rel)
    return _local_read(rel)


def artifact_download_response(rel: str, filename: str, media_type: str = "application/octet-stream",
                               extra_headers: dict | None = None) -> Response:
    """下载产物：强制下载 + nosniff（防存储型 XSS）。"""
    headers = {"X-Content-Type-Options": "nosniff",
               "Content-Disposition": f'attachment; filename="{filename}"'}
    if extra_headers:
        headers.update(extra_headers)
    if _backend() == "s3":
        return Response(content=artifact_read(rel), media_type=media_type, headers=headers)
    return FileResponse(_local_path(rel), media_type=media_type, headers=headers)


def artifact_stream_response(rel: str, media_type: str, range_header: str = "") -> Response:
    """流式输出产物（视频/音频）：支持 Range seek，S3 走 Range 请求、本地走文件切片。"""
    if not artifact_exists(rel):
        raise HTTPException(status_code=404, detail="没有可流式输出的文件")
    size = artifact_size(rel)
    start, end = 0, size - 1
    if range_header and range_header.startswith("bytes="):
        try:
            rng = range_header[6:].split("-")
            start = int(rng[0]) if rng[0] else 0
            if len(rng) > 1 and rng[1]:
                end = min(int(rng[1]), size - 1)
            if start > end or start >= size:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        except ValueError:
            pass
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Disposition": f'inline; filename="{os.path.basename(rel)}"',
    }

    def _iter():
        if _backend() == "s3":
            yield from _s3_iter(rel, start, end)
        else:
            yield from _local_iter(rel, start, end)

    return StreamingResponse(
        _iter(),
        media_type=media_type,
        status_code=206 if range_header else 200,
        headers=headers,
    )


def artifact_file_response(rel: str, media_type: str = "application/octet-stream",
                           filename: str | None = None) -> Response:
    """普通文件响应（不强制下载，用于可安全内联的类型）。"""
    headers = {"X-Content-Type-Options": "nosniff"}
    if _backend() == "s3":
        return Response(content=artifact_read(rel), media_type=media_type, headers=headers)
    return FileResponse(_local_path(rel), media_type=media_type,
                        filename=filename, headers=headers)
