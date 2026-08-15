from __future__ import annotations
"""联机游戏 API：建房 + WebSocket 房间广播（通用，不绑定具体游戏）。

协议（前端游戏页面遵循）：
- 连接: ws://HOST/api/ws/game/{room_id}
- 客户端→服务端: {"type":"join","name":"玩家名"}  （加入，首条必须）
- 服务端→所有:   {"type":"players","players":[{id,name},...]} （成员变化广播）
- 客户端→服务端: {"type":"action","data":{...}}  （游戏动作）
- 服务端→他人:   {"type":"action","from":"playerId","fromName":"名字","data":{...}}
- 客户端→服务端: {"type":"chat","text":"..."}     （聊天）
- 服务端→所有:   {"type":"chat","from":"名字","text":"..."}
- 服务端→加入者: {"type":"welcome","playerId":"xxx","roomId":"xxx","players":[...]}
"""
import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services import game_rooms

logger = logging.getLogger("app.api.game")

router = APIRouter()

# 房间级连接集合：room_id -> set(WebSocket)，广播时遍历同房间所有连接
_room_conns: dict[str, set] = {}


@router.post("/game/rooms")
async def create_room():
    """创建房间，返回 room_id（前端用它拼分享链接）。"""
    room_id = game_rooms.create_room()
    return {"room_id": room_id}


@router.get("/game/rooms/{room_id}")
async def room_info(room_id: str):
    room = game_rooms.get_room(room_id)
    if room is None:
        return {"error": "房间不存在"}
    return room


@router.websocket("/ws/game/{room_id}")
async def game_ws(websocket: WebSocket, room_id: str):
    """房间 WebSocket：加入/广播。"""
    await websocket.accept()
    room = game_rooms.get_room(room_id)
    if room is None:
        await websocket.send_json({"type": "error", "message": "房间不存在"})
        await websocket.close()
        return

    player_id = None
    player_name = "玩家"
    conns = _room_conns.setdefault(room_id, set())
    conns.add(websocket)

    async def broadcast(message: dict, exclude: WebSocket | None = None):
        conns = _room_conns.get(room_id, set())
        for conn in list(conns):
            if conn is exclude:
                continue
            try:
                await conn.send_json(message)
            except Exception:
                conns.discard(conn)

    try:
        # 等 join 消息
        for _ in range(5):  # 最多等 10 秒
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=2)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if msg.get("type") == "join":
                name = msg.get("name") or "玩家"
                pid, resolved = game_rooms.add_player(room_id, name)
                if pid is None:
                    await websocket.send_json({"type": "error", "message": resolved})
                    await websocket.close()
                    return
                player_id, player_name = pid, resolved
                players = game_rooms.room_players(room_id)
                await websocket.send_json({
                    "type": "welcome", "playerId": player_id,
                    "roomId": room_id, "players": players,
                })
                await broadcast({"type": "players", "players": players})
                break

        if player_id is None:
            await websocket.close()
            return

        # 转发游戏动作/聊天
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")
            if mtype == "action":
                await broadcast({
                    "type": "action",
                    "from": player_id,
                    "fromName": player_name,
                    "data": msg.get("data", {}),
                }, exclude=websocket)
            elif mtype == "chat":
                await broadcast({
                    "type": "chat",
                    "from": player_name,
                    "text": str(msg.get("text", ""))[:200],
                })
            elif mtype == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("[game:%s] ws error: %s", room_id, str(e)[:100])
    finally:
        if player_id:
            conns.discard(websocket)
            game_rooms.remove_player(room_id, player_id)
            players = game_rooms.room_players(room_id)
            await broadcast({"type": "players", "players": players})
            if not _room_conns.get(room_id):
                _room_conns.pop(room_id, None)
