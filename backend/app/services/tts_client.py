from __future__ import annotations
"""TTS 配音：文本 → 语音 WAV。

优先 edge-tts（微软神经语音，效果好）；不可用时回退 Windows SAPI（零安装，中文 Huihui）。
"""
import asyncio
import logging
import os
import subprocess
import sys

logger = logging.getLogger("app.services.tts_client")


def _edge_tts_available() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("edge_tts") is not None
    except Exception:
        return False


async def tts_speak(text: str, output_path: str, voice: str = "zh-CN-XiaoxiaoNeural") -> bool:
    """文本转语音，保存为 WAV/MP3。

    Args:
        text: 要朗读的文本
        output_path: 输出文件路径（.wav 或 .mp3）
        voice: edge-tts 声音（edge 可用时）

    Returns:
        成功返回 True。
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)

    # 优先 edge-tts（效果好）
    if _edge_tts_available():
        try:
            import edge_tts
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(output_path)
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return True
        except Exception as e:
            logger.warning("edge-tts 失败，回退 SAPI: %s", str(e)[:150])

    # 回退 Windows SAPI（win32com）
    if output_path.lower().endswith(".mp3"):
        output_path = output_path.rsplit(".", 1)[0] + ".wav"
    try:
        # 线程内 COM 有兼容问题，直接同步调用（阻塞短暂，可接受）
        return _sapi_speak(text, output_path)
    except Exception as e:
        logger.warning("SAPI TTS 失败: %s", str(e)[:150])
        return False


def _sapi_speak(text: str, output_wav: str) -> bool:
    import win32com.client
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception:
        pass
    try:
        sapi = win32com.client.Dispatch("SAPI.SpVoice")
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        # 显式指定 16kHz 16bit 单声道格式，避免默认格式不受支持
        try:
            stream.Format.Type = 10  # SPSF_16kHz16BitMono
        except Exception:
            pass
        stream.Open(output_wav, 3)  # SSFMCreateForWrite
        sapi.AudioOutputStream = stream
        sapi.Rate = 0
        sapi.Volume = 100
        sapi.Speak(text)
        stream.Close()
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
    return os.path.exists(output_wav) and os.path.getsize(output_wav) > 0
