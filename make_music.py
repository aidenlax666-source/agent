#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成音乐：LLM 作曲 + Python 标准库合成 WAV（零第三方依赖）。

用法：python make_music.py "主题词"
"""
import ast
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))
from app.services.llm_client import chat_completion

MUSIC_SYSTEM = """你是一位作曲家 + Python 音频合成专家。用 Python **标准库**（wave / math / struct，禁止 numpy/pygame/mido/任何第三方库）编写一个脚本，合成一首契合主题的音乐并保存为 WAV。

【作曲要求（按给定主题创作）】
1. 主题情绪要贴合（如"星空"→空灵慢速、"夏日海边"→轻快明亮、"森林"→悠扬自然）
2. 用 C 大调/A 小调等音阶设计旋律：主旋律 + 低音和声（可叠加两个音轨让声音更饱满）
3. 节奏：主旋律音符序列（音符名+八度+时值+休止），速度/时值体现情绪
4. 时长 30-60 秒

【合成技术（必须）】
1. 采样率 44100，16-bit 单声道（或双声道）
2. 正弦波合成，音符频率用公式 f = 440 * 2^((midi-69)/12)
3. 每个音符加**衰减包络**（attack/decay），音符间无爆音
4. 整体淡入淡出（开头 0.5s 渐入、结尾渐出）
5. 音量适中（峰值 ~0.5 避免削波）

【输出】
- 保存 melody.wav 到当前目录
- 打印 DURATION:秒数、NOTES:音符数

只输出完整 Python 代码，不要解释。"""


def check(code: str) -> tuple[bool, str]:
    try:
        ast.parse(code)
    except SyntaxError as e:
        return False, f"语法错误 {e.msg}@{e.lineno}"
    if "wave" not in code or "math" not in code:
        return False, "缺 wave/math"
    if "melody.wav" not in code:
        return False, "缺 melody.wav 输出"
    for bad in ("numpy", "pygame", "import mido", "scipy"):
        if bad in code:
            return False, f"用了禁用库 {bad}"
    return True, "OK"


async def main() -> None:
    theme = sys.argv[1] if len(sys.argv) > 1 else "夏日海边"
    print(f"正在作曲（主题：{theme}）...", flush=True)
    code = ""
    for attempt in range(4):
        try:
            code = await chat_completion(MUSIC_SYSTEM, f"【主题】{theme}\n请生成作曲脚本", temperature=0.4, max_tokens=6000)
            code = code.strip()
            if code.startswith("```python"):
                code = code[9:]
            elif code.startswith("```"):
                code = code[3:]
            if code.endswith("```"):
                code = code[:-3]
            code = code.strip()
            ok, reason = check(code)
            if ok:
                print(f"第 {attempt+1} 次生成成功（{len(code)} 字符）", flush=True)
                break
            print(f"第 {attempt+1} 次校验不过: {reason}，重试...", flush=True)
            code = ""
        except Exception as e:
            print(f"第 {attempt+1} 次调用失败: {str(e)[:120]}", flush=True)
            code = ""

    if not code:
        print("生成失败", flush=True)
        sys.exit(1)

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gen_music.py")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(code)
    print(f"作曲脚本已保存: {out_path}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
