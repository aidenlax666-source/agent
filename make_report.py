#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成脚本：从网站抓取数据 → 统计分析 → 生成精美 HTML 可视化报告。"""
import ast
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))
from app.services.llm_client import chat_completion

REPORT_SYSTEM = """你是一位 Python 脚本专家 + 数据可视化设计师。生成一个 Python 脚本，完成「从网站抓数据 → 统计分析 → 生成可视化报告」全流程。

【数据抓取】
- 从 https://quotes.toscrape.com 抓取前 3 页名言（翻页：/page/1/ 到 /page/3/），每页 10 条，提取「名言文本」「作者」
- 网络注意：requests 在本环境可能报 SSL/代理错误，优先用 urllib.request 或 requests 加 proxies={"http": None, "https": None}；抓取失败要有降级

【统计分析】（用 pandas）
- 统计总名言数、去重后作者数
- 每位作者的名言条数（作者 → 条数），找出条数最多的作者

【可视化报告 report.html】（重点，必须精美）
- 单文件 HTML，纯 HTML/CSS/SVG 实现图表，禁止外链任何 CDN/库/字体
- 现代浅色主题（或优雅深色），圆角卡片、阴影、合理留白
- 页面结构：
  1. 标题：「📊 名言网站数据报告」（大号渐变标题）
  2. 统计摘要卡片（3-4 张）：总名言数、作者总数、最多名言作者、平均每人条数
  3. 条形图：作者名言条数 Top10（纯 SVG 横向条形图，带数值标签，配色渐变）
  4. 数据表格：作者 / 条数（含序号，斑马纹行）
  5. 页脚：抓取时间、数据来源
- 中文界面

【输出】
- 脚本保存 report.html 到当前目录
- 打印 SUCCESS:DATA_ROWS:N（N=作者数）和 PREVIEW_DATA:JSON（作者条数前5）

只输出完整 Python 代码，不要解释。"""


def check(code: str) -> tuple[bool, str]:
    try:
        ast.parse(code)
    except SyntaxError as e:
        return False, f"语法错误 {e.msg}@{e.lineno}"
    need = ["urllib", "requests", "pandas", "report.html", "to_html" if "to_html" in code else "open("]
    if "requests" not in code and "urllib" not in code:
        return False, "缺少网络库"
    if "pandas" not in code:
        return False, "缺少 pandas"
    if "report.html" not in code:
        return False, "缺少 report.html 输出"
    return True, "OK"


async def main() -> None:
    print("正在生成报告脚本...", flush=True)
    code = ""
    for attempt in range(4):
        try:
            code = await chat_completion(REPORT_SYSTEM, "请生成报告脚本", temperature=0.3, max_tokens=8000)
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

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gen_report.py")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(code)
    print(f"脚本已保存: {out_path}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
