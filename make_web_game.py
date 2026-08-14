#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成精美可交互的网页版贪吃蛇（单 HTML 文件，可分享），保存 index.html。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))
from app.services.llm_client import chat_completion

WEB_SYSTEM = """你是一位顶尖的网页设计师 + 前端开发专家。生成一个**视觉精美、可交互、适合分享**的网页版贪吃蛇游戏，单 HTML 文件（内嵌 CSS + JS），不需要任何外部依赖。

【技术约束】
1. 单文件 index.html：<!DOCTYPE html> + <style> + <canvas> + <script>，禁止外链 CDN/字体/库（纯原生 JS）
2. 全屏自适应：游戏区域居中，响应式（桌面键盘 + 手机触屏都能玩）

【视觉设计（核心，必须精美）】
1. 深色霓虹主题：页面背景深蓝黑渐变，游戏画布圆角 + 霓虹描边
2. 蛇：每节颜色渐变（头亮青 → 尾深蓝），头部画眼睛，身体圆润
3. 食物：红色发光圆球 + 呼吸脉动动画
4. 顶部：游戏名"🐍 贪吃蛇"渐变艺术字 + 得分/最高分面板（最高分存 localStorage）
5. 底部：操作提示（"方向键/WASD/滑动 控制 · 空格 重开"）
6. 开始画面：标题 + "点击或按任意键开始"呼吸闪烁
7. 结束画面：半透明遮罩 + "游戏结束" + 得分 + "重新开始"按钮
8. 动效：吃到食物 +10 金色飘字；蛇移动平滑（requestAnimationFrame 插值）

【交互与玩法】
1. 方向键/WASD 控制，不能反向；手机支持滑动（touch 事件）
2. 得分 +10/食物，最高分 localStorage 持久化
3. 撞墙/撞自己结束 → 结束画面，点击/空格重开
4. 游戏循环用 requestAnimationFrame + 时间步进（约 120ms/步）

只输出完整 HTML 代码，不要解释。"""


def is_complete(html: str) -> tuple[bool, str]:
    """校验 HTML 是否完整：闭合标签 + 核心机制齐全（不锁函数名，防误判）。"""
    checks = {
        "</script>": "</script> 闭合",
        "</html>": "</html> 结尾",
        "requestAnimationFrame": "游戏主循环 RAF",
        "keydown": "键盘事件",
        "touchstart": "触屏事件",
        "getContext": "canvas 绘制",
    }
    missing = [desc for kw, desc in checks.items() if kw not in html]
    return (not missing), "、".join(missing) if missing else "完整"


async def main() -> None:
    print("正在生成网页版贪吃蛇...", flush=True)
    html = ""
    for attempt in range(4):
        try:
            html = await chat_completion(WEB_SYSTEM, "请生成网页版贪吃蛇", temperature=0.3, max_tokens=10000)
            html = html.strip()
            if html.startswith("```html"):
                html = html[7:]
            elif html.startswith("```"):
                html = html[3:]
            if html.endswith("```"):
                html = html[:-3]
            html = html.strip()
            ok, reason = is_complete(html)
            head = html.lstrip()[:200]
            if (head.startswith("<!DOCTYPE") or head.startswith("<html")) and ok:
                print(f"第 {attempt+1} 次生成成功（{len(html)} 字符，完整性校验通过）", flush=True)
                break
            print(f"第 {attempt+1} 次不完整: {reason or '开头异常'}（{len(html)} 字符），重试...", flush=True)
            html = ""
        except Exception as e:
            print(f"第 {attempt+1} 次调用失败: {str(e)[:120]}", flush=True)
            html = ""

    if not html:
        print("生成失败", flush=True)
        sys.exit(1)

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"已保存: {out_path}（{len(html)} 字符）", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
