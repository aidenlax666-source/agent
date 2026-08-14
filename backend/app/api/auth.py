from __future__ import annotations
"""Authentication endpoints with SQLite persistence."""

import time as _time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from jose import jwt
import bcrypt

from app.config import get_settings
from app.database import get_user_by_email, create_user
from app.api.dependencies import get_current_user

router = APIRouter()
settings = get_settings()

# 登录失败限流（进程内存中实现，无 Redis 依赖）
_failed_logins: dict[str, list[float]] = {}
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300


def _hash_pwd(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_pwd(password: str, stored: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _check_login_rate_limit(email: str) -> None:
    now = _time.time()
    attempts = [t for t in _failed_logins.get(email, []) if now - t < LOGIN_LOCKOUT_SECONDS]
    _failed_logins[email] = attempts
    if len(attempts) >= MAX_LOGIN_ATTEMPTS:
        raise HTTPException(429, "尝试次数过多，请稍后再试")


def _record_login_failure(email: str) -> None:
    _failed_logins.setdefault(email, []).append(_time.time())


def _clear_login_failures(email: str) -> None:
    _failed_logins.pop(email, None)


def make_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode({"sub": user_id, "exp": expire}, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


@router.post("/register")
async def register(data: dict):
    email = data.get("email", "").strip()
    pwd = data.get("password", "").strip()
    name = data.get("name", "").strip() or None

    if not email or not pwd:
        raise HTTPException(400, "Email and password required")
    if len(pwd) < 4:
        raise HTTPException(400, "Password too short")

    existing = await get_user_by_email(email)
    if existing:
        raise HTTPException(409, "Email already registered")

    user = await create_user(email, name, _hash_pwd(pwd))
    token = make_token(user["id"])

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user["id"], "email": user["email"], "name": user.get("name"),
                 "credits": user.get("credits", 10), "created_at": user["created_at"]}
    }


@router.post("/login")
async def login(data: dict):
    email = data.get("email", "").strip()
    pwd = data.get("password", "").strip()

    _check_login_rate_limit(email)

    user = await get_user_by_email(email)
    if not user or not _verify_pwd(pwd, user["password_hash"]):
        _record_login_failure(email)
        raise HTTPException(401, "Email or password incorrect")

    _clear_login_failures(email)
    token = make_token(user["id"])
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user["id"], "email": user["email"], "name": user.get("name"),
                 "credits": user.get("credits", 10), "created_at": user["created_at"]}
    }


@router.get("/me")
async def get_me(user=Depends(get_current_user)):
    return {"id": user["id"], "email": user["email"], "name": user.get("name"),
            "credits": user.get("credits", 10), "created_at": user.get("created_at", "")}
