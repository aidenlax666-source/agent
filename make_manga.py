#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 AI 漫剧：LLM 写剧本 → Pillow 绘制漫画分镜 → HTML 播放器。

用法：python make_manga.py "主题"
"""
import ast
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))
from app.services.llm_client import chat_completion

MANGA_SYSTEM = """你是一位 AI 漫剧导演 + Python 图像设计师。生成一个 Python 脚本，把给定主题制作成一部「AI 漫剧」（有声漫画形式）：剧本 + 漫画分镜画面 + HTML 播放器。

【剧本设计】
- 编一个 6-8 幕的完整小故事（温馨治愈或有趣风格），有起承转合
- 每幕包含：旁白（一句话）、角色对白（1-2 句）、场景（背景渐变配色 + 场景元素）、角色（名称+颜色+表情）

【画面绘制】（Pillow，800x600 每帧，漫画风）
1. 背景：垂直渐变（用两个颜色插值逐行画）
2. 场景元素程序化绘制：星星/月亮（夜空）、山（多边形剪影）、树（树干+树冠圆）、海浪（曲线）、萤火虫/光点（小圆发光）、云朵（椭圆叠加）等，按每幕场景关键词画
3. 角色：简化 Q 版（圆头+眼睛+嘴部表情+小身体），全身单色+描边，表情（开心/惊讶/难过/微笑）用眼嘴形状区分
4. 每帧漫画风：白色边框线、左上角幕标题（"第X幕"）、底部旁白字幕条（半透明黑底白字）、角色对白用对话气泡（椭圆+小尾巴）
5. 中文文字用字体：C:/Windows/Fonts/msyh.ttc（微软雅黑）或 simhei.ttf，找不到就 ImageFont.load_default()

【HTML 播放器 manga.html】
- 内嵌所有帧图（base64 或相对路径 frame_XX.png），深色背景，居中
- 上一幕/下一幕按钮 + 键盘左右键翻页 + 自动播放（每幕 4 秒）
- 每幕下方显示对白文字，顶部显示故事标题
- 优雅现代风格（渐变背景、圆角卡片）

【输出】
- 保存 frame_01.png ~ frame_NN.png 和 manga.html 到当前目录
- 打印 SUCCESS:SCENES:N

只输出完整 Python 代码，不要解释。"""


def check(code: str) -> tuple[bool, str]:
    try:
        ast.parse(code)
    except SyntaxError as e:
        return False, f"语法错误 {e.msg}@{e.lineno}"
    if "PIL" not in code and "Image" not in code:
        return False, "缺 Pillow"
    if "manga.html" not in code:
        return False, "缺 manga.html 输出"
    if "frame_" not in code:
        return False, "缺 frame 输出"
    if "ImageFont" not in code:
        return False, "缺字体处理"
    return True, "OK"


async def main() -> None:
    theme = sys.argv[1] if len(sys.argv) > 1 else "小狐狸和萤火虫的森林之夜"
    print(f"正在制作 AI 漫剧（主题：{theme}）...", flush=True)
    code = ""
    for attempt in range(4):
        try:
            code = await chat_completion(MANGA_SYSTEM, f"【主题】{theme}\n请生成漫剧脚本", temperature=0.4, max_tokens=10000)
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

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gen_manga.py")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(code)
    print(f"漫剧脚本已保存: {out_path}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
