#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成一个可直接运行的可视化小游戏（Tkinter 贪吃蛇），保存到本地文件。"""
import ast
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))
from app.services.llm_client import chat_completion

GAME_SYSTEM = """你是一位资深游戏 UI/UX 设计师兼 Python 游戏开发专家。用 Tkinter（标准库，禁止 pygame 和第三方库）生成一个**视觉精美**的贪吃蛇游戏。

【视觉设计要求（非常重要，这是评判重点）】
1. 整体风格：深色霓虹主题。窗口背景 #0f0f23 或类似深蓝黑，游戏区域深色渐变底
2. 蛇：每节身体颜色渐变（头最亮 → 尾渐暗，如从 #00ffcc 渐变到 #0066aa），头部画眼睛，身体画圆角/圆形
3. 食物：红色发光圆球（多层圆叠加模拟光晕），可轻微呼吸脉动
4. 界面布局（分区域）：
   - 顶部：游戏标题"🐍 贪吃蛇"（大号微软雅黑粗体）+ 得分面板（"得分: 0"大字号，吃食物时得分飘字动画）
   - 中间：游戏画布（圆角边框，边框有霓虹描边）
   - 底部：操作提示（"方向键 / WASD 移动 · 空格 重启"）小字灰底
5. 开始画面：标题艺术字居中 + "按任意方向键开始" 呼吸闪烁提示
6. 结束画面：半透明遮罩 + "游戏结束" + "得分：X" + "按空格重新开始"，渐变文字
7. 动效：吃到食物时画布上飘出"+10"金色文字并淡出；蛇移动时头尾平滑过渡（节与节间距均匀）

【功能要求】
1. 完整可独立运行：import + 类 + if __name__ == "__main__"
2. 方向键 + WASD 控制，不能反向；得分 +10/食物
3. 撞墙/撞自己结束 → 结束画面，空格重新开始
4. 用 after() 驱动主循环（约 120ms/步），禁止 while True 阻塞
5. 全部中文界面

只输出 Python 代码，不要解释。"""


async def main() -> None:
    print("正在生成贪吃蛇游戏代码...", flush=True)
    code = ""
    for attempt in range(3):
        try:
            code = await chat_completion(GAME_SYSTEM, "请生成贪吃蛇游戏", temperature=0.3, max_tokens=4096)
            code = code.strip()
            if code.startswith("```python"):
                code = code[9:]
            elif code.startswith("```"):
                code = code[3:]
            if code.endswith("```"):
                code = code[:-3]
            code = code.strip()
            ast.parse(code)
            print(f"第 {attempt+1} 次生成成功（{len(code)} 字符）", flush=True)
            break
        except SyntaxError as e:
            print(f"第 {attempt+1} 次生成有语法错误: {e.msg}@{e.lineno}，重试...", flush=True)
            code = ""
        except Exception as e:
            print(f"第 {attempt+1} 次调用失败: {str(e)[:120]}", flush=True)
            code = ""

    if not code:
        print("生成失败", flush=True)
        sys.exit(1)

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snake_game.py")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(code)
    print(f"已保存: {out_path}", flush=True)
    print("=" * 40, flush=True)
    print(code[:500], flush=True)


if __name__ == "__main__":
    asyncio.run(main())
