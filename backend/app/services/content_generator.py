from __future__ import annotations
"""通用内容生成器：模型根据用户需求现造可分享的 HTML 内容（游戏/漫剧/网页/工具页）。

不预置任何固定产物——需求说做什么就生成什么（贪吃蛇/扫雷/个人主页/漫剧...均由模型按需求实现）。
由 mini 任务自动调用（需求含"做一个XX/生成XX游戏/漫剧/网页"等，且非联机/非报告/非数据任务）。
"""
import logging
import os
import uuid

from app.services.llm_client import chat_completion

logger = logging.getLogger("app.services.content_generator")

_WEB_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "web"))

CONTENT_SYSTEM = """你是一位 AI 内容创作专家。根据用户需求生成**可立即使用的 HTML 单文件**内容（网页/小游戏/漫剧/工具页等）。

【用户需求】根据描述实现具体内容（类型、玩法、功能、主题）。

【通用要求】
1. 单文件 HTML：内嵌 CSS + JS，禁止外链任何 CDN/库/字体/图片
2. 现代精美视觉：渐变/圆角/阴影/合理配色，中文界面，响应式（手机可用）
3. 完整可用：有开始/操作/结束或完整的交互闭环，无半成品
4. 如果是游戏：有明确规则、操作方式、得分/胜负判定
5. 如果是漫剧/故事页：分镜/章节切换，叙事完整
6. 如果是工具页：功能真实可用（计算/转换/生成等）

【输出】只输出完整 HTML 代码，不要解释。"""


def _complete(html: str) -> tuple[bool, str]:
    # 只强制 HTML 结构；<script> 改为软检查（纯静态页面无 JS 也合法）
    checks = {"</html>": "html 结尾"}
    missing = [d for kw, d in checks.items() if kw not in html]
    if missing:
        return False, "、".join(missing)
    return True, "完整"


async def generate_content(requirement: str) -> dict:
    """按需求生成 HTML 内容，保存到 web/，返回 {success, file_path, url, error}。"""
    os.makedirs(_WEB_DIR, exist_ok=True)
    html = ""
    for attempt in range(4):
        try:
            html = await chat_completion(CONTENT_SYSTEM, f"【用户需求】{requirement}\n请生成内容HTML", temperature=0.4, max_tokens=10000)
            html = html.strip()
            if html.startswith("```html"):
                html = html[7:]
            elif html.startswith("```"):
                html = html[3:]
            if html.endswith("```"):
                html = html[:-3]
            html = html.strip()
            ok, reason = _complete(html)
            if ok:
                break
            logger.warning("内容生成第 %d 次不完整: %s", attempt + 1, reason)
            html = ""
        except Exception as e:
            logger.warning("内容生成第 %d 次失败: %s", attempt + 1, str(e)[:120])
            html = ""
    if not html:
        return {"success": False, "error": "内容生成失败"}

    content_id = uuid.uuid4().hex[:8]
    fname = f"content_{content_id}.html"
    path = os.path.join(_WEB_DIR, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return {
        "success": True,
        "file_path": path,
        "url": f"/{fname}",
        "content_id": content_id,
    }
