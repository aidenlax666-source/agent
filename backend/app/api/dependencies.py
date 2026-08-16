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
    """解析匿名身份：优先客户端自报 id（校验长度），否则按直连 IP 派生，
    避免所有无头客户端共享同一个 guest 身份导致数据互相可见。"""
    anon_id = (anon_id or "").strip()
    if anon_id and 0 < len(anon_id) <= 64:
        return anon_id
    # 无自报 id：用 IP（经 _client_ip 同款逻辑：仅可信代理时信任 XFF）派生稳定身份
    peer = request.client.host if request and request.client else "unknown"
    xff = request.headers.get("x-forwarded-for") if request else None
    if xff:
        from app.api.mini import _client_ip
        peer = _client_ip(request)
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
