from __future__ import annotations
"""Shared dependencies - auth with SQLite."""

import hashlib
import os as _os

from fastapi import Depends, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

from app.config import get_settings
from app.database import get_user, get_user_by_email, create_user

settings = get_settings()
security = HTTPBearer(auto_error=False)


async def resolve_user(token: str | None, anon_id: str | None) -> dict:
    """从 JWT token 或匿名 id 解析用户，返回用户 dict。

    登录用户用 JWT 里的 sub 查；匿名用户用 anon_id 生成独立身份（email 唯一），
    避免所有匿名用户共用同一个 guest 身份导致项目数据互相可见。
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

    anon_id = (anon_id or "").strip()
    if not anon_id or len(anon_id) > 64:
        anon_id = "guest"
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
) -> dict:
    """FastAPI 依赖：返回当前用户（登录用户或独立匿名会话用户）。"""
    token = credentials.credentials if credentials else None
    return await resolve_user(token, x_anonymous_id)
