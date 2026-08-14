from __future__ import annotations
"""Login via Playwright persistent context - login once, reuse forever."""

import threading
import time
import json
from pathlib import Path
from urllib.parse import urlparse
from fastapi import APIRouter

router = APIRouter()

# Persistent browser profile directory - login state lives here
PROFILE_DIR = Path(__file__).parent.parent.parent / "browser_profile"
PROFILE_DIR.mkdir(exist_ok=True)

_login_status: dict = {"status": "idle", "message": ""}

# Active login session (single concurrent login for simplicity).
# The worker thread owns the Playwright browser; the API sets `save_requested`
# to tell it to persist state, and it sets `done` when finished.
_session_lock = threading.Lock()
_active_session: dict | None = None


def _save_state(ctx, domain: str) -> None:
    """Persist the browser context's login state to {domain}.json."""
    state = ctx.storage_state()
    (PROFILE_DIR / f"{domain}.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )


@router.post("/sessions/login")
async def start_login(data: dict):
    """Open a persistent browser window. User logs in once, profile is saved."""
    url = data.get("url", "")
    if not url:
        return {"error": "url required"}

    domain = urlparse(url).netloc.replace("www.", "")

    global _active_session
    with _session_lock:
        # 单窗口互斥：已有登录窗口在跑时拒绝新请求，避免刷出大量浏览器进程
        if _active_session is not None and not _active_session["done"].is_set():
            return {"error": "已有登录窗口打开，请先完成或等待"}
        _active_session = {
            "domain": domain,
            "save_requested": threading.Event(),
            "done": threading.Event(),
        }
    session = _active_session

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

                # Wait until the user requests a save, or closes the browser.
                while not session["save_requested"].is_set():
                    time.sleep(0.5)
                    try:
                        page.title()
                    except Exception:
                        break  # browser was closed manually

                # Persist login state (works whether save was requested or the
                # browser was closed manually).
                try:
                    _save_state(ctx, domain)
                    _login_status["status"] = "closed"
                    _login_status["message"] = f"登录状态已保存 ({domain})"
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

    threading.Thread(target=_worker, daemon=True).start()
    return {"status": "opening", "message": "Browser opening, please login"}


@router.get("/sessions/status")
async def login_status():
    """Check login window status."""
    return _login_status


@router.get("/sessions/check")
async def check_profile():
    """Check if any login state exists (user has logged in before)."""
    files = list(PROFILE_DIR.glob("*.json"))
    return {
        "has_profile": any(f.stat().st_size > 100 for f in files),
        "profile_dir": str(PROFILE_DIR),
    }


@router.post("/sessions/continue-after-login")
async def continue_after_login(data: dict):
    """User finished logging in - persist state.

    旧版用于恢复 projects 工作流；projects 流程已退役，登录态保存后直接完成。
    """
    if not _active_session:
        return {"status": "saved", "message": "没有活动的登录窗口"}

    session = _active_session
    session["save_requested"].set()
    session["done"].wait(timeout=10)
    return {"status": "saved", "message": "登录状态已保存"}
