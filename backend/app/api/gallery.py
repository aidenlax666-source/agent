from __future__ import annotations
"""分享中心 API：扫描 web/ 目录，列出可分享的作品（游戏/报告/漫剧/视频/音乐），支持 zip 打包下载。"""
import io
import zipfile
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()

WEB_DIR = Path(__file__).parent.parent.parent.parent / "web"


def _classify(name: str) -> dict | None:
    lower = name.lower()
    if lower.endswith(".html"):
        base = lower[:-5]
        if base == "index":
            return {"name": "🎮 网页游戏", "type": "game", "desc": "贪吃蛇游戏"}
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
    if lower.endswith(".wav"):
        return {"name": "🎵 AI 音乐", "type": "music", "desc": f"{name}（{_size_human(Path(WEB_DIR, name))}）"}
    if lower.endswith((".png", ".jpg", ".jpeg")):
        return {"name": "🖼️ AI 图片", "type": "image", "desc": f"{name}（{_size_human(Path(WEB_DIR, name))}）"}
    return None


def _size_human(p: Path) -> str:
    size = p.stat().st_size if p.exists() else 0
    return f"{size/1024:.0f}KB" if size < 1024 * 1024 else f"{size/1024/1024:.1f}MB"


@router.get("/gallery")
async def gallery():
    """返回 web/ 目录下所有可分享作品（相对路径，前端拼站点 URL）。"""
    items: list[dict] = []
    if WEB_DIR.is_dir():
        for f in sorted(WEB_DIR.iterdir()):
            if not f.is_file():
                continue
            info = _classify(f.name)
            if info:
                items.append({
                    "path": f"/{f.name}",
                    "filename": f.name,
                    **info,
                })
    return {"items": items, "base_note": "路径为相对路径，扫码/分享时用完整站点 URL"}


@router.get("/gallery/download-zip")
async def download_zip():
    """打包 web/ 目录全部作品为 zip 下载。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if WEB_DIR.is_dir():
            for f in sorted(WEB_DIR.iterdir()):
                if f.is_file():
                    zf.write(f, f.name)
    buf.seek(0)
    return FileResponse(
        buf,
        media_type="application/zip",
        filename="works.zip",
        headers={"Content-Disposition": "attachment; filename=works.zip"},
    )
