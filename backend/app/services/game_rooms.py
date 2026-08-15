from __future__ import annotations
"""通用联机游戏房间服务：建房/加入/广播，不绑定具体游戏。

房间只负责：成员管理 + 消息转发。游戏规则完全由前端实现，
通过 WebSocket 把动作广播给房间内所有玩家。
"""
import time
import uuid
import logging

logger = logging.getLogger("app.services.game_rooms")

# room_id -> room
# room = {"id": str, "players": {player_id: name}, "created_at": float, "messages": int}
_rooms: dict[str, dict] = {}
_MAX_ROOMS = 200
_ROOM_TTL = 24 * 3600  # 房间最长存活 24h（防僵尸房间占内存）


def _cleanup_stale() -> None:
    """清理超 TTL 的僵尸房间（无人加入也不删的老房间）。"""
    now = time.time()
    stale = [rid for rid, room in _rooms.items() if now - room["created_at"] > _ROOM_TTL]
    for rid in stale:
        _rooms.pop(rid, None)


def create_room() -> str:
    """创建房间，返回 room_id。"""
    _cleanup_stale()
    # 清理超限旧房间
    if len(_rooms) > _MAX_ROOMS:
        for rid in list(_rooms.keys())[: len(_rooms) - _MAX_ROOMS]:
            _rooms.pop(rid, None)
    room_id = uuid.uuid4().hex[:8]
    _rooms[room_id] = {
        "id": room_id,
        "players": {},
        "created_at": time.time(),
    }
    return room_id


def get_room(room_id: str) -> dict | None:
    _cleanup_stale()
    room = _rooms.get(room_id)
    if not room:
        return None
    return {
        "id": room["id"],
        "players": [{"id": pid, "name": name} for pid, name in room["players"].items()],
    }


def add_player(room_id: str, name: str) -> tuple[str, str] | None:
    """加入房间，返回 (player_id, error)。"""
    room = _rooms.get(room_id)
    if not room:
        return None, "房间不存在"
    name = (name or "玩家").strip()[:20] or "玩家"
    # 重名加序号
    names = set(room["players"].values())
    if name in names:
        i = 2
        while f"{name}{i}" in names:
            i += 1
        name = f"{name}{i}"
    player_id = uuid.uuid4().hex[:8]
    room["players"][player_id] = name
    return player_id, name


def remove_player(room_id: str, player_id: str) -> None:
    room = _rooms.get(room_id)
    if room:
        room["players"].pop(player_id, None)
        if not room["players"]:
            _rooms.pop(room_id, None)  # 空房间自动销毁


def room_players(room_id: str) -> list[dict]:
    room = _rooms.get(room_id)
    if not room:
        return []
    return [{"id": pid, "name": name} for pid, name in room["players"].items()]
