from __future__ import annotations
"""豆包（火山引擎）TTS 语音合成：文本 → MP3。

使用豆包语音大模型 V3 HTTP SSE 单向流式接口（openspeech.bytedance.com）：
- 鉴权：X-Api-Key（火山引擎语音控制台创建的 API Key，与方舟 ark- 不同）
- 资源：X-Api-Resource-Id 头（seed-tts-2.0 默认 / seed-tts-1.0）
- 输出：MP3（默认）/ PCM / OGG
"""
import base64
import json
import logging
import os
import uuid

import httpx

from app.config import get_settings

logger = logging.getLogger("app.services.tts_client")

_TTS_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse"

# 常用中文音色（seed-tts-2.0）
DEFAULT_VOICE = "zh_female_shuangkuaisisi_uranus_bigtts"  # 爽快思思 2.0
VOICES = {
    "cancan": "zh_female_cancan_uranus_bigtts",          # 知性灿灿 2.0
    "xiaoyuan": "zh_female_tianmeixiaoyuan_uranus_bigtts",  # 甜美小源 2.0
    "xiaohe": "zh_female_xiaohe_uranus_bigtts",          # 晓荷 2.0
    "m191": "zh_male_m191_uranus_bigtts",                # 云舟 2.0（男）
    "taocheng": "zh_male_taocheng_uranus_bigtts",        # 晓田 2.0（男）
    "kefunv": "zh_female_kefunvsheng_uranus_bigtts",     # 暖阳女声 2.0（客服）
    "dacey": "en_female_dacey_uranus_bigtts",            # Dacey（英文）
}

# 1.0 音色（seed-tts-1.0）
VOICES_1_0 = {
    "cancan": "BV700_streaming",
    "qingcang": "BV701_streaming",  # 青苍（有声书）
    "general": "BV001_streaming",   # 通用女声
    "general_male": "BV002_streaming",  # 通用男声
}


def _resolve_voice(voice: str, resource_id: str) -> str:
    """音色别名 → 完整 voice_type（按代次匹配）。"""
    v = str(voice or "").strip().lower()
    if resource_id == "seed-tts-1.0":
        return VOICES_1_0.get(v, voice or "BV700_streaming")
    return VOICES.get(v, voice or DEFAULT_VOICE)


async def tts_speak(
    text: str,
    output_path: str,
    voice: str = DEFAULT_VOICE,
    emotion: str | None = None,
    speech_rate: int = 0,
    resource_id: str = "seed-tts-2.0",
) -> dict:
    """文本 → 语音 MP3，保存到 output_path。

    Args:
        text: 要朗读的文本（自动截断到 3000 字）
        output_path: 输出文件路径（.mp3）
        voice: 音色（VOICES/VOICES_1_0 里的别名或完整 voice_type）
        emotion: 情绪（happy/sad/angry/narrator/storytelling 等，可选）
        speech_rate: 语速 [-50, 100]，0 默认
        resource_id: 模型资源（seed-tts-2.0 / seed-tts-1.0）

    Returns:
        {"success": bool, "file_path": str|None, "error": str}
    """
    settings = get_settings()
    api_key = settings.doubao_tts_api_key
    if not api_key:
        return {
            "success": False,
            "file_path": None,
            "error": "未配置 DOUBAO_TTS_API_KEY（火山引擎语音控制台 → 语音技术 → API Key 管理）",
        }

    text = (text or "").strip()[:3000]  # 单次合成上限保护
    if not text:
        return {"success": False, "file_path": None, "error": "文本为空"}

    full_voice = _resolve_voice(voice, resource_id)

    audio_params: dict = {
        "format": "mp3",
        "sample_rate": 24000,
        "speech_rate": speech_rate,
        "loudness_rate": 0,
    }
    if emotion:
        audio_params["emotion"] = emotion

    body = {
        "user": {"uid": "ai-auto-agent"},
        "req_params": {
            "text": text,
            "speaker": full_voice,
            "audio_params": audio_params,
        },
    }

    req_id = uuid.uuid4().hex
    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": req_id,
        "X-Control-Require-Usage-Tokens-Return": "text_words",
        "Content-Type": "application/json",
    }

    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    chunks: list[bytes] = []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0), trust_env=False) as client:
            async with client.stream("POST", _TTS_URL, json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    err_body = (await resp.aread()).decode("utf-8", "replace")
                    return {"success": False, "file_path": None,
                            "error": f"豆包 TTS HTTP {resp.status_code}: {err_body[:200]}"}
                # SSE 流解析：data: {json}
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(("event:", ":")):
                        continue
                    if not line.startswith("data:"):
                        continue
                    try:
                        msg = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    code = msg.get("code", 0)
                    if code == 20000000:  # 结束帧（含 usage）
                        continue
                    if code != 0:  # 错误帧
                        return {"success": False, "file_path": None,
                                "error": f"豆包 TTS 错误({code}): {msg.get('message') or msg.get('hint') or ''}"}
                    audio_b64 = msg.get("data")
                    if audio_b64:
                        try:
                            chunks.append(base64.b64decode(audio_b64))
                        except Exception:
                            pass
    except httpx.TimeoutException:
        return {"success": False, "file_path": None, "error": "豆包 TTS 请求超时"}
    except Exception as e:
        logger.warning("豆包 TTS 异常: %s", repr(e)[:200])
        return {"success": False, "file_path": None, "error": f"豆包 TTS 失败: {str(e)[:150]}"}

    if not chunks:
        return {"success": False, "file_path": None, "error": "豆包 TTS 未返回音频数据"}

    with open(output_path, "wb") as f:
        f.write(b"".join(chunks))
    if os.path.getsize(output_path) == 0:
        return {"success": False, "file_path": None, "error": "豆包 TTS 输出为空文件"}
    return {"success": True, "file_path": output_path, "error": None}
