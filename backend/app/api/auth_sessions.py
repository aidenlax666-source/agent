from __future__ import annotations
"""Login via Playwright persistent context - login once, reuse for a short window.

登录态按账号隔离：每个用户（含匿名会话）的登录 cookie 存到独立目录
browser_profile/{user_id}/，任务沙箱只加载该用户自己的登录态。

**短时存储**：第三方网站登录态（Cookie）会过期，这里只做短时保存（默认 2 小时），
过期自动失效并清理——够"登录→抓取"用一次即可，不长期保留登录凭证。
"""

import re
import threading
import time
import json
from pathlib import Path
from urllib.parse import urlparse
from fastapi import APIRouter, Depends

from app.api.dependencies import get_current_user

router = APIRouter()

# Persistent browser profile directory - login state lives here
PROFILE_DIR = Path(__file__).parent.parent.parent / "browser_profile"
PROFILE_DIR.mkdir(exist_ok=True)

# 登录态有效期（秒）：第三方 Cookie 会过期，短时保存即可（默认 2 小时）
LOGIN_TTL_SECONDS = 2 * 3600

_login_status: dict = {"status": "idle", "message": ""}

# 每个用户一个登录窗口槽位（不再全局单槽：避免一人开窗阻塞全站）
_session_lock = threading.Lock()
_active_sessions: dict[str, dict] = {}


def _safe_domain(url: str) -> str:
    """提取域名并做文件名安全化（去掉端口/非法字符，防止 Windows 文件名非法）。"""
    host = (urlparse(url).hostname or "unknown").lower()
    host = re.sub(r'[\\/:*?"<>|]', "_", host)
    return host.replace("www.", "") or "unknown"


def _save_state(ctx, domain: str, profile_dir: str) -> None:
    """把登录态存为短时文件：JSON 里带 _saved_at 时间戳（过期清理用）。"""
    state = ctx.storage_state()
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    record = {"_saved_at": time.time(), "state": state}
    (Path(profile_dir) / f"{domain}.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )


def _state_valid_at(profile_dir: str, domain: str, now: float | None = None) -> bool:
    """判断某域名登录态文件是否仍在有效期内。"""
    now = now or time.time()
    p = Path(profile_dir) / f"{domain}.json"
    if not p.is_file():
        return False
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        saved = rec.get("_saved_at") or 0
        return now - saved < LOGIN_TTL_SECONDS
    except Exception:
        return False


def _cleanup_expired(user_id: str) -> None:
    """清理该用户已过期的登录态文件（短时存储，过期即删）。"""
    user_dir = PROFILE_DIR / str(user_id)
    if not user_dir.is_dir():
        return
    now = time.time()
    for f in user_dir.glob("*.json"):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            saved = rec.get("_saved_at") or 0
            if now - saved >= LOGIN_TTL_SECONDS:
                f.unlink(missing_ok=True)
        except Exception:
            f.unlink(missing_ok=True)  # 损坏文件直接清掉


@router.post("/sessions/login")
async def start_login(data: dict, user=Depends(get_current_user)):
    """Open a persistent browser window. User logs in once, profile is saved (per account)."""
    url = data.get("url", "")
    if not url:
        return {"error": "url required"}
    # 只允许 http/https（防 file:/data: 等本地文件/命令 scheme 被浏览器加载）
    low = url.lower()
    if not low.startswith(("http://", "https://")):
        return {"error": "仅支持 http/https 协议的登录地址"}
    # SSRF 防护：禁止访问内网/回环/云元数据地址（服务器浏览器不能当内网代理）
    from app.sandbox.security import is_lan_url
    if is_lan_url(url):
        return {"error": "禁止访问内网/回环/元数据地址"}

    domain = _safe_domain(url)
    user_profile = str(PROFILE_DIR / str(user["id"]))
    Path(user_profile).mkdir(parents=True, exist_ok=True)

    uid = str(user["id"])
    with _session_lock:
        # 每个用户独立窗口槽：该用户已有窗口在跑时拒绝新请求
        existing = _active_sessions.get(uid)
        if existing is not None and not existing["done"].is_set():
            return {"error": "已有登录窗口打开，请先完成或等待"}
        session = {
            "domain": domain,
            "user_id": uid,
            "profile_dir": user_profile,
            "save_requested": threading.Event(),
            "done": threading.Event(),
        }
        _active_sessions[uid] = session

    _login_status["status"] = "opening"
    _login_status["message"] = f"Opening {url}..."

    def _worker():
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="msedge", headless=False)
                ctx = browser.new_context(viewport={"width": 1280, "height": 900})
                page = ctx.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    print(f"goto failed: {e}")
                _login_status["status"] = "waiting"
                _login_status["message"] = "请在浏览器中登录，登录完成后点「我已完成登录」"

                # Wait until the user requests a save, closes the browser, or times out.
                # 超时（3 分钟）强制结束：避免"有人打开登录窗口不关"长期占用槽位。
                wait_started = time.time()
                while not session["save_requested"].is_set():
                    time.sleep(0.5)
                    if time.time() - wait_started > 180:
                        _login_status["status"] = "timeout"
                        _login_status["message"] = "登录窗口超时，已自动结束"
                        break
                    try:
                        page.title()
                    except Exception:
                        break  # browser was closed manually

                # Persist login state (works whether save was requested or the
                # browser was closed manually).
                try:
                    _save_state(ctx, session["domain"], session["profile_dir"])
                    _login_status["status"] = "closed"
                    _login_status["message"] = f"登录状态已保存 ({session['domain']})"
                except Exception as e:
                    _login_status["status"] = "error"
                    _login_status["message"] = f"保存登录状态失败: {e}"

                try:
                    browser.close()
                except Exception:
                    pass
        except Exception as e:
            _login_status["status"] = "error"
            _login_status["message"] = str(e)
        finally:
            session["done"].set()
            with _session_lock:
                if _active_sessions.get(uid) is session:
                    _active_sessions.pop(uid, None)

    threading.Thread(target=_worker, daemon=True).start()
    return {"status": "opening", "message": "Browser opening, please login"}


@router.get("/sessions/status")
async def login_status(user=Depends(get_current_user)):
    """Check login window status（仅返回当前用户自己的窗口状态，不再暴露全局/他人状态）。"""
    uid = str(user["id"])
    session = _active_sessions.get(uid)
    if session is None or session["done"].is_set():
        return {"status": "idle", "message": "", "domain": ""}
    return {
        "status": _login_status.get("status", "opening"),
        "message": _login_status.get("message", ""),
        "domain": session.get("domain", ""),
    }


@router.get("/sessions/check")
async def check_profile(user=Depends(get_current_user)):
    """Check if the current account has a VALID (non-expired) login state.（不暴露服务器路径）"""
    user_dir = PROFILE_DIR / str(user["id"])
    _cleanup_expired(str(user["id"]))  # 先清过期，只算有效的
    files = list(user_dir.glob("*.json")) if user_dir.is_dir() else []
    has_valid = False
    expires_at = 0.0
    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            saved = rec.get("_saved_at") or 0
            if saved > expires_at:
                expires_at = saved + LOGIN_TTL_SECONDS
            has_valid = True
        except Exception:
            continue
    return {
        "has_profile": has_valid,
        "expires_at": expires_at,
        "ttl_seconds": LOGIN_TTL_SECONDS,
    }


@router.post("/sessions/continue-after-login")
async def continue_after_login(data: dict, user=Depends(get_current_user)):
    """User finished logging in - persist state.

    旧版用于恢复 projects 工作流；projects 流程已退役，登录态保存后直接完成。
    """
    uid = str(user["id"])
    session = _active_sessions.get(uid)
    if not session:
        return {"status": "saved", "message": "没有活动的登录窗口"}
    session["save_requested"].set()
    session["done"].wait(timeout=10)
    return {"status": "saved", "message": "登录状态已保存"}
