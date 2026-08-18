from __future__ import annotations
"""Authentication endpoints with SQLite persistence."""

import time as _time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from jose import jwt
import bcrypt

from app.config import get_settings
from app.database import get_user_by_email, create_user
from app.api.dependencies import get_current_user

router = APIRouter()
settings = get_settings()

# 登录失败限流（进程内存中实现，无 Redis 依赖）：邮箱维度 + IP 维度
_failed_logins: dict[str, list[float]] = {}
_failed_logins_by_ip: dict[str, list[float]] = {}
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300      # IP 维度锁定
EMAIL_LOCKOUT_SECONDS = 60       # 邮箱维度锁定（短一些，防攻击者锁死他人账号）
_FAILED_LOGIN_MAX_KEYS = 10000   # 限流表键数上限（防伪造输入无限填充内存）

# 注册限速（防批量注册刷 10 积分）：按 IP
_register_attempts: dict[str, list[float]] = {}
REGISTER_MAX_PER_MINUTE = 5


def _trim_rate_dict(d: dict) -> None:
    if len(d) > _FAILED_LOGIN_MAX_KEYS:
        for k in list(d.keys())[: _FAILED_LOGIN_MAX_KEYS // 2]:
            d.pop(k, None)


def _hash_pwd(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_pwd(password: str, stored: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _check_ip_rate(ip: str) -> None:
    """IP 维度前置检查（登录撞库 / 注册刷号）。"""
    now = _time.time()
    _trim_rate_dict(_failed_logins_by_ip)
    ip_attempts = [t for t in _failed_logins_by_ip.get(ip, []) if now - t < LOGIN_LOCKOUT_SECONDS]
    _failed_logins_by_ip[ip] = ip_attempts
    if len(ip_attempts) >= MAX_LOGIN_ATTEMPTS * 3:
        raise HTTPException(429, "尝试次数过多，请稍后再试")


def _check_email_rate(email: str) -> None:
    """邮箱维度检查：仅在确实存在失败记录时锁（锁定窗口较短，缓解 DoS）。"""
    now = _time.time()
    _trim_rate_dict(_failed_logins)
    attempts = [t for t in _failed_logins.get(email, []) if now - t < EMAIL_LOCKOUT_SECONDS]
    _failed_logins[email] = attempts
    if len(attempts) >= MAX_LOGIN_ATTEMPTS:
        raise HTTPException(429, "尝试次数过多，请稍后再试")


def _record_login_failure(email: str, ip: str, email_exists: bool) -> None:
    # 邮箱维度只在账号真实存在时记录（避免伪造邮箱灌满限速表 / 只锁真实账号）
    if email_exists:
        _failed_logins.setdefault(email, []).append(_time.time())
    _failed_logins_by_ip.setdefault(ip, []).append(_time.time())


def _clear_login_failures(email: str, ip: str) -> None:
    _failed_logins.pop(email, None)
    _failed_logins_by_ip.pop(ip, None)


def _check_register_rate(ip: str) -> None:
    now = _time.time()
    _trim_rate_dict(_register_attempts)
    q = [t for t in _register_attempts.get(ip, []) if now - t < 60]
    _register_attempts[ip] = q
    if len(q) >= REGISTER_MAX_PER_MINUTE:
        raise HTTPException(429, "注册过于频繁，请稍后再试")


def make_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode({"sub": user_id, "exp": expire}, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


@router.post("/register")
async def register(data: dict, request: Request):
    import re as _re
    if not isinstance(data.get("email"), str) or not isinstance(data.get("password"), str):
        raise HTTPException(400, "邮箱和密码必须是文本")
    email = data["email"].strip()
    pwd = data["password"].strip()
    name = data.get("name")
    name = name.strip() if isinstance(name, str) and name.strip() else None

    # 前端同款校验（服务端权威）：邮箱格式 + 密码长度
    if not email or not pwd:
        raise HTTPException(400, "邮箱和密码不能为空")
    if not _re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(400, "邮箱格式不正确")
    if len(pwd) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if name and len(name) > 20:
        raise HTTPException(400, "昵称最多 20 个字")

    # 注册限速（防批量注册刷 10 积分）
    ip = request.client.host if request.client else "unknown"
    _check_register_rate(ip)

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
async def login(data: dict, request: Request):
    if not isinstance(data.get("email"), str) or not isinstance(data.get("password"), str):
        raise HTTPException(400, "Email and password must be text")
    email = data["email"].strip()
    pwd = data["password"].strip()
    ip = request.client.host if request.client else "unknown"

    # IP 维度前置检查（防分布式撞库）
    _check_ip_rate(ip)

    user = await get_user_by_email(email)
    if not user or not _verify_pwd(pwd, user["password_hash"]):
        # 邮箱维度失败计数只在账号真实存在时记录（防伪造邮箱锁死他人/灌表）
        _record_login_failure(email, ip, email_exists=bool(user))
        raise HTTPException(401, "Email or password incorrect")

    _clear_login_failures(email, ip)
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
