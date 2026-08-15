from __future__ import annotations
"""分享中心 API：按账号列出可分享的作品（游戏/报告/漫剧/视频/音乐/图片），支持 zip 打包下载。

产物与任务关联（task result 里的 url 字段），只显示当前账号自己生成的作品；
具体作品 URL 仍可公开访问，方便复制链接/扫码分享给他人。
"""
import io
import json
import os
import sqlite3
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, Response
from fastapi.responses import FileResponse

from app.api.dependencies import get_current_user

router = APIRouter()

WEB_DIR = Path(__file__).parent.parent.parent.parent / "web"


def _user_artifact_names(user_id: str) -> set[str]:
    """查当前用户所有任务的产物文件名（从 task result 的 url 字段提取）。"""
    names: set[str] = set()
    try:
        from app.database import DB_PATH
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT result FROM mini_tasks WHERE user_id=?", (user_id,)).fetchall()
        conn.close()
        for row in rows:
            res = row["result"]
            if not res:
                continue
            try:
                d = json.loads(res)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            for k in ("game_url", "content_url", "report_url", "video_url", "image_url", "music_url"):
                v = d.get(k)
                if v:
                    names.add(os.path.basename(str(v)))
            for v in d.get("image_urls") or []:
                names.add(os.path.basename(str(v)))
    except Exception:
        pass
    return names


def _classify(name: str) -> dict | None:
    lower = name.lower()
    if lower.endswith(".html"):
        base = lower[:-5]
        if base == "index":
            return {"name": "🏠 首页", "type": "game", "desc": "AI 生成的主页/作品"}
        if base == "manga":
            return {"name": "🦊 AI 漫剧", "type": "manga", "desc": "小狐狸和萤火虫"}
        if base == "music":
            return {"name": "🎵 音乐播放页", "type": "music", "desc": "夏日海边（AI 作曲）"}
        if base == "video":
            return {"name": "🎬 AI 视频", "type": "video", "desc": "夏日小猫（Seedance 生成）"}
        if base.startswith("report_"):
            return {"name": "📊 可视化报告", "type": "report", "desc": f"报告 {base[7:12]}"}
        return {"name": f"📄 {base}", "type": "html", "desc": "网页"}
    if lower.endswith(".mp4"):
        return {"name": "🎬 AI 视频", "type": "video", "desc": f"{name}（{_size_human(Path(WEB_DIR, name))}）"}
    if lower.endswith((".wav", ".mp3")):
        return {"name": "🎵 AI 音乐", "type": "music", "desc": f"{name}（{_size_human(Path(WEB_DIR, name))}）"}
    if lower.endswith((".png", ".jpg", ".jpeg")):
        return {"name": "🖼️ AI 图片", "type": "image", "desc": f"{name}（{_size_human(Path(WEB_DIR, name))}）"}
    return None


def _size_human(p: Path) -> str:
    size = p.stat().st_size if p.exists() else 0
    return f"{size/1024:.0f}KB" if size < 1024 * 1024 else f"{size/1024/1024:.1f}MB"


@router.get("/gallery")
async def gallery(user=Depends(get_current_user)):
    """返回当前账号自己生成的所有作品（相对路径，前端拼站点 URL）。"""
    own = _user_artifact_names(user["id"])
    items: list[dict] = []
    if WEB_DIR.is_dir():
        for f in sorted(WEB_DIR.iterdir()):
            if not f.is_file():
                continue
            info = _classify(f.name)
            if not info:
                continue
            if f.name not in own:
                continue  # 只显示自己的产物
            items.append({
                "path": f"/{f.name}",
                "filename": f.name,
                **info,
            })
    return {"items": items, "base_note": "路径为相对路径，扫码/分享时用完整站点 URL"}


@router.get("/gallery/download-zip")
async def download_zip(user=Depends(get_current_user)):
    """打包当前账号自己的全部作品为 zip 下载。"""
    own = _user_artifact_names(user["id"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if WEB_DIR.is_dir():
            for f in sorted(WEB_DIR.iterdir()):
                if f.is_file() and f.name in own:
                    zf.write(f, f.name)
    buf.seek(0)
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=works.zip"},
    )
