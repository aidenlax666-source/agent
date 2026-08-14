import sys
import os
import asyncio

# UTF-8 mode for Windows console (fixes emoji/Chinese encoding crashes)
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import init_db
from app.api import auth, auth_sessions, upload, mini, gallery
from app.services.mini_tasks import mini_scheduler_loop

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # 启动 mini 定时任务调度器
    asyncio.create_task(mini_scheduler_loop())
    yield


app = FastAPI(
    title="AI Automation Generator",
    description="AI-driven browser automation - describe tasks in natural language",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS - 只允许配置的前端来源 + 本地 localhost 任意端口（开发）；用 JWT header 鉴权，不启用 credentials
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_origin_regex=r"http://localhost:\d+",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])
app.include_router(auth_sessions.router, prefix="/api", tags=["Auth Sessions"])
app.include_router(upload.router, prefix="/api", tags=["Upload"])
app.include_router(mini.router, prefix="/api", tags=["Mini Generator"])
app.include_router(gallery.router, prefix="/api", tags=["Gallery"])


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "version": "1.0.0"}
