from __future__ import annotations
"""Shared dependencies - auth with SQLite."""

import hashlib
import os as _os

from fastapi import Depends, Header, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

from app.config import get_settings
from app.database import get_user, get_user_by_email, create_user

settings = get_settings()
security = HTTPBearer(auto_error=False)


def _anon_identity(anon_id: str | None, request: Request | None) -> str:
    """解析匿名身份。

    安全：匿名身份必须绑定真实来源（IP），绝不直接信任客户端自报的 id——
    否则拿到/猜到他人 anon_id 即可冒用其匿名数据（任务/提醒/监控/积分，甚至
    触发读取其 browser_profile 中的第三方登录 Cookie）。实现：
    - 有自报 id 时：ip+id 一起哈希派生（id 仅作为同一来源下的子身份区分，
      脱离 IP 单独使用无效）；
    - 无自报 id：直接用 IP 派生稳定身份。
    """
    from app.api.mini import _client_ip
    peer = _client_ip(request) if request else "unknown"
    anon_id = (anon_id or "").strip()
    if anon_id and 0 < len(anon_id) <= 64:
        return hashlib.sha256(f"{peer}|{anon_id}".encode("utf-8")).hexdigest()[:32]
    return "ip_" + hashlib.sha256(peer.encode("utf-8")).hexdigest()[:16]


async def resolve_user(token: str | None, anon_id: str | None, request: Request | None = None) -> dict:
    """从 JWT token 或匿名 id 解析用户，返回用户 dict。

    登录用户用 JWT 里的 sub 查；匿名用户用 anon_id 生成独立身份（email 唯一），
    无 id 时按 IP 派生，避免所有匿名用户共用同一个 guest 身份导致项目数据互相可见。
    """
    if token:
        try:
            payload = jwt.decode(
                token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
            )
            uid = payload.get("sub")
            if uid:
                user = await get_user(uid)
                if user:
                    return user
        except (JWTError, ValueError):
            pass

    anon_id = _anon_identity(anon_id, request)
    email = f"anon_{anon_id}@auto.local"

    user = await get_user_by_email(email)
    if not user:
        salt = _os.urandom(16).hex()
        h = hashlib.sha256(("anon" + salt).encode()).hexdigest()
        user = await create_user(email, "匿名用户", f"{salt}${h}")
    return user


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    x_anonymous_id: str | None = Header(default=None, alias="X-Anonymous-Id"),
    request: Request = None,
) -> dict:
    """FastAPI 依赖：返回当前用户（登录用户或独立匿名会话用户）。"""
    token = credentials.credentials if credentials else None
    return await resolve_user(token, x_anonymous_id, request)
