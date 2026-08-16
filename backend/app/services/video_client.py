from __future__ import annotations
"""豆包（火山方舟）视频/图像生成客户端。

- 文生视频：doubao-seedance-*（异步任务：提交 → 轮询 → 拿到视频 URL）
- 文生图：doubao-seedream-*（异步任务，可顺带复用）
"""
import asyncio
import logging
import time

import httpx

from app.config import get_settings

logger = logging.getLogger("app.services.video_client")

_DEFAULT_MODEL = "doubao-seedance-2-0-260128"
_IMAGE_MODEL = "doubao-seedream-4-0-250828"


async def generate_image(
    prompt: str,
    model: str = _IMAGE_MODEL,
    size: str = "1024x1024",
) -> dict:
    """文生图（Seedream，同步返回）：POST /images/generations。

    Returns:
        {"success": bool, "image_urls": list[str], "status": str, "detail": str}
    """
    settings = get_settings()
    if not settings.doubao_api_key:
        return {"success": False, "image_urls": [], "status": "no_key", "detail": "未配置 DOUBAO_API_KEY"}

    body = {"model": model, "prompt": prompt, "size": size, "watermark": False}
    created = await _api("POST", "/images/generations", body, timeout=120)
    if not created:
        return {"success": False, "image_urls": [], "status": "submit_failed", "detail": "图片生成失败"}
    urls = []
    for item in created.get("data") or []:
        u = item.get("url") or item.get("b64_json")
        if u and u.startswith("http"):
            urls.append(u)
    return {
        "success": bool(urls),
        "image_urls": urls,
        "status": "succeeded" if urls else "empty",
        "detail": f"生成 {len(urls)} 张图片",
    }


async def _api(method: str, path: str, body: dict | None = None, timeout: int = 60):
    settings = get_settings()
    url = f"{settings.doubao_base_url}{path}"
    headers = {
        "Authorization": f"Bearer {settings.doubao_api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=30.0), trust_env=False, headers=headers) as client:
        resp = await client.request(method, url, json=body if body is not None else None)
        if resp.status_code >= 400:
            logger.warning("豆包 API %s %s -> %s: %s", method, path, resp.status_code, resp.text[:300])
            return None
        return resp.json()


async def generate_video(
    prompt: str,
    model: str = _DEFAULT_MODEL,
    poll_interval: int = 10,
    max_wait: int = 600,
    image_path: str | None = None,
) -> dict:
    """文生视频 / 图生视频（Seedance）：提交异步任务并轮询直到完成。

    image_path: 提供则做图生视频（图片作为首帧/参考）。

    Returns:
        {"success": bool, "video_url": str|None, "status": str, "detail": str}
    """
    settings = get_settings()
    if not settings.doubao_api_key:
        return {"success": False, "video_url": None, "status": "no_key", "detail": "未配置 DOUBAO_API_KEY"}

    content: list[dict] = []
    if image_path:
        import base64
        import mimetypes
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        mime = mimetypes.guess_type(image_path)[0] or "image/png"
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}, "role": "reference_image"})
    content.append({"type": "text", "text": prompt})

    body = {
        "model": model,
        "content": content,
        "ratio": "16:9",
        "duration": 5,
        "watermark": False,
    }
    try:
        created = await _api("POST", "/contents/generations/tasks", body, timeout=60)
    except Exception as e:
        return {"success": False, "video_url": None, "status": "submit_failed",
                "detail": f"任务提交失败: {str(e)[:120]}"}
    if not created:
        return {"success": False, "video_url": None, "status": "submit_failed", "detail": "任务提交失败"}
    task_id = created.get("id")
    if not task_id:
        return {"success": False, "video_url": None, "status": "submit_failed", "detail": str(created)[:200]}

    # 轮询任务状态（网络/解析异常不终止任务：记录并继续轮询，防一次抖动杀掉整个任务）
    waited = 0
    _poll_errors = 0
    while waited < max_wait:
        await asyncio.sleep(poll_interval)
        waited += poll_interval
        try:
            state = await _api("GET", f"/contents/generations/tasks/{task_id}", timeout=30)
        except Exception as e:
            _poll_errors += 1
            logger.warning("[video:%s] 轮询异常(%d): %s", task_id, _poll_errors, str(e)[:120])
            if _poll_errors >= 5:
                return {"success": False, "video_url": None, "status": "poll_error",
                        "detail": f"轮询任务状态连续失败: {str(e)[:120]}"}
            continue
        if not state:
            continue
        status = state.get("status", "")
        if status == "succeeded":
            content = state.get("content") or {}
            video_url = content.get("video_url") or content.get("url") or ""
            return {
                "success": bool(video_url),
                "video_url": video_url or None,
                "status": status,
                "detail": f"任务 {task_id} 完成，耗时约 {waited}s",
            }
        if status in ("failed", "cancelled"):
            return {"success": False, "video_url": None, "status": status, "detail": str(state)[:300]}
        logger.info("[video:%s] 生成中 %s (%ss)", task_id, status, waited)

    return {"success": False, "video_url": None, "status": "timeout", "detail": f"等待 {max_wait}s 超时"}


async def download_video(video_url: str, save_path: str) -> bool:
    """下载生成的视频到本地。"""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=30.0), trust_env=False) as client:
            resp = await client.get(video_url)
            if resp.status_code != 200:
                logger.warning("视频下载失败 %s", resp.status_code)
                return False
            with open(save_path, "wb") as f:
                f.write(resp.content)
            return True
    except Exception as e:
        logger.warning("视频下载异常: %s", str(e)[:200])
        return False
