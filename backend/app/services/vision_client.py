from __future__ import annotations
"""豆包（火山方舟）视觉客户端：图片识别/总结，供 DeepSeek 补充多模态能力。

DeepSeek 是纯文本模型，看不懂图片。当任务涉及图片（用户上传截图/要处理的图片）时，
先用本模块调豆包视觉模型识别图片内容并总结成文字，再把文字描述交给 DeepSeek。
"""
import base64
import logging
import mimetypes

import httpx

from app.config import get_settings

logger = logging.getLogger("app.services.vision_client")

_DEFAULT_PROMPT = "请仔细查看这张图片，用中文详细描述图片内容。如果是截图，请描述界面上的文字和元素。"


def _encode_image(image_path: str) -> str:
    with open(image_path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    mime = mimetypes.guess_type(image_path)[0] or "image/png"
    return f"data:{mime};base64,{data}"


async def describe_image(
    image_path: str,
    prompt: str = _DEFAULT_PROMPT,
    max_tokens: int = 1200,
) -> str:
    """识别一张图片，返回中文内容描述。

    Args:
        image_path: 本地图片路径
        prompt: 识别指令（默认总结图片内容）
        max_tokens: 描述长度上限

    Returns:
        图片内容的中文描述文本；失败时返回空字符串并记日志。
    """
    settings = get_settings()
    if not settings.doubao_api_key:
        logger.warning("未配置 DOUBAO_API_KEY，无法识别图片")
        return ""

    image_url = _encode_image(image_path)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": prompt},
            ],
        }
    ]
    body = {
        "model": settings.doubao_vision_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=30.0),
            trust_env=False,  # 忽略系统代理（同 DeepSeek 客户端），避免代理 TLS 问题
            headers={
                "Authorization": f"Bearer {settings.doubao_api_key}",
                "Content-Type": "application/json",
            },
        ) as client:
            resp = await client.post(
                f"{settings.doubao_base_url}/chat/completions", json=body
            )
            if resp.status_code != 200:
                logger.warning("豆包识别失败 HTTP %s: %s", resp.status_code, resp.text[:500])
                return ""
            data = resp.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            return (content or "").strip()
    except Exception as e:
        logger.warning("豆包识别异常: %s", repr(e)[:300])
        return ""


async def describe_images(image_paths: list[str], prompt: str = _DEFAULT_PROMPT) -> list[str]:
    """批量识别多张图片，返回与输入顺序一致的描述列表（失败项为空串）。"""
    results: list[str] = []
    for p in image_paths:
        desc = await describe_image(p, prompt)
        results.append(desc)
    return results
