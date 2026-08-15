from __future__ import annotations
"""豆包（火山引擎）TTS 语音合成：文本 → MP3。

使用豆包语音大模型 V3 HTTP SSE 单向流式接口（openspeech.bytedance.com）：
- 鉴权：X-Api-Key（火山引擎语音控制台创建的 API Key，与方舟 ark- 不同）
- 资源：seed-tts-2.0（默认，2.0 音色） / seed-tts-1.0（1.0 音色）
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
        text: 要朗读的文本（<= 若干千字，按需截断）
        output_path: 输出文件路径（.mp3）
        voice: 音色（VOICES 里的别名或完整 voice_type）
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

    # 音色别名 → 完整 voice_type
    full_voice = VOICES.get(str(voice).lower(), voice)
    if resource_id == "seed-tts-1.0" and not full_voice.startswith("BV"):
        full_voice = "BV700_streaming"  # 1.0 默认音色

    text = (text or "").strip()[:3000]  # 单次合成上限保护
    if not text:
        return {"success": False, "file_path": None, "error": "文本为空"}

    body = {
        "user": {"uid": "ai-auto-agent", "audiotoken": ""},
        "request": {
            "reqid": uuid.uuid4().hex,
            "text": text,
            "operation": "submit",
            "resource_id": resource_id,
            "model_name": resource_id,
            "audio": {
                "voice_type": full_voice,
                "encoding": "mp3",
                "speed_ratio": 1.0 + speech_rate / 100.0,
            },
        },
    }
    if emotion:
        body["request"]["audio"]["emotion"] = emotion

    headers = {
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
    }

    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    chunks: list[bytes] = []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0), trust_env=False) as client:
            async with client.stream("POST", _TTS_URL, json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    return {"success": False, "file_path": None,
                            "error": f"豆包 TTS HTTP {resp.status_code}: {(await resp.aread()).decode('utf-8', 'replace')[:200]}"}
                # SSE 流解析：audio_binary 事件的 data 是 {"data":"<base64>","content_type":"audio/mpeg",...}
                async for line in resp.aiter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        try:
                            msg = json.loads(payload)
                        except Exception:
                            continue
                        # 错误事件
                        if msg.get("code") not in (None, 3000) or msg.get("error"):
                            return {"success": False, "file_path": None,
                                    "error": f"豆包 TTS 错误: {msg.get('message') or msg.get('error') or str(msg)[:200]}"}
                        data = msg.get("data") or msg.get("audio") or msg.get("content") or ""
                        if isinstance(data, str):
                            try:
                                chunks.append(base64.b64decode(data))
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
