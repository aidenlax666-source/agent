from __future__ import annotations
"""联机游戏生成器：按用户需求生成联机游戏前端（接房间协议），保存到 web/ 可分享。

由 mini 任务自动调用（需求含"联机/多人/一起玩"等关键词时），也支持手动调用。
"""
import asyncio
import logging
import os
import uuid

from app.services.llm_client import chat_completion

logger = logging.getLogger("app.services.game_generator")

_WEB_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "web"))

GAME_SYSTEM = """你是一位联机游戏前端专家。生成一个**多人联机小游戏**的 HTML 单文件（内嵌 CSS + JS，无外部依赖），通过 WebSocket 实现多人同玩。

【用户需求】根据给定的游戏描述实现具体玩法（规则、操作、胜负条件）。

【房间系统（必须实现）】
1. 页面顶部有两个模式：🏠 创建房间 / 🔗 加入房间（输入房间号）
2. 创建房间：fetch POST 到 `${API_BASE}/api/game/rooms`（API_BASE 用 window.location.origin），拿 {room_id}，自动连 ws
3. 加入房间：输入房间号后连 ws：`${WS_BASE}/api/ws/game/{roomId}`（WS_BASE = API_BASE 的 http→ws）
4. 分享链接：建房后页面显示可复制的完整链接（当前 URL + ?room=房间号）；加载时若有 ?room= 自动进房

【WebSocket 协议（必须遵守）】
- 连接后首条发: {"type":"join","name":玩家名}
- 收 {"type":"welcome","playerId":...,"players":[...]} → 进游戏，记自己 playerId；players[0] 是房主
- 收 {"type":"players","players":[...]} → 更新在线玩家列表
- 发动作: {"type":"action","data":{...}}
- 收 {"type":"action","from":...,"fromName":...,"data":{...}} → 渲染对方动作/事件（from 是自己则忽略）
- 聊天: 发 {"type":"chat","text":"..."}，收 {"type":"chat","from":"...","text":"..."}
- 断线: 提示"连接断开，刷新重进"

【回合/胜负流程（按游戏实现，必须完整可玩）】
- 明确谁是当前操作者（如画手/当前回合玩家），操作完成后要有明确的"确认/完成/提交"按钮进入下一阶段
- 胜负/得分要有明确判定和显示，支持重新开始/下一轮

【视觉要求】现代精美（渐变背景、圆角、按钮），单文件零外链，中文界面。
【输出】只输出完整 HTML 代码。"""


def _complete(html: str) -> tuple[bool, str]:
    checks = {
        "</html>": "html 结尾",
        "WebSocket": "WebSocket",
        "/api/game/rooms": "建房 API",
        "/api/ws/game/": "房间 WS",
        "welcome": "welcome 处理",
        "players": "玩家列表",
        "action": "动作同步",
    }
    missing = [d for kw, d in checks.items() if kw not in html]
    return (not missing), "、".join(missing) if missing else "完整"


async def generate_multiplayer_game(game_desc: str) -> dict:
    """生成联机游戏页面，保存到 web/，返回 {success, file_path, url, error}。"""
    os.makedirs(_WEB_DIR, exist_ok=True)
    html = ""
    for attempt in range(4):
        try:
            html = await chat_completion(GAME_SYSTEM, f"【游戏描述】{game_desc}\n请生成联机游戏HTML", temperature=0.4, max_tokens=10000)
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
            logger.warning("联机游戏生成第 %d 次不完整: %s", attempt + 1, reason)
            html = ""
        except Exception as e:
            logger.warning("联机游戏生成第 %d 次失败: %s", attempt + 1, str(e)[:120])
            html = ""
    if not html:
        return {"success": False, "error": "联机游戏生成失败"}

    game_id = uuid.uuid4().hex[:8]
    fname = f"game_{game_id}.html"
    path = os.path.join(_WEB_DIR, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return {
        "success": True,
        "file_path": path,
        "url": f"/{fname}",
        "game_id": game_id,
    }
