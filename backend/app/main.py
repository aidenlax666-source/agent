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
from app.api import auth, auth_sessions, upload, mini, gallery, game, local_exec
from app.services.mini_tasks import mini_scheduler_loop

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # 启动 mini 定时任务调度器
    asyncio.create_task(mini_scheduler_loop())
    # 孤儿沙箱清理：worker 重启后回收本实例遗留的容器 + 租约已过期的容器
    # （云架构多实例：崩溃实例的容器由其他实例按任务租约回收，启动时清一次兜底）
    from app.sandbox.docker_executor import cleanup_orphan_containers
    try:
        await asyncio.to_thread(cleanup_orphan_containers)
    except Exception:
        pass
    # 分布式任务 worker + 崩溃恢复 reaper：有 Redis 时启动（云架构多实例）
    from app.services import distributed
    from app.services.mini_tasks import distributed_worker_loop, distributed_reaper_loop, system_monitor_loop
    if distributed.redis_enabled():
        asyncio.create_task(distributed_worker_loop())
        asyncio.create_task(distributed_reaper_loop())
    # 系统告警（仅 leader 实例跑；alert_enabled=False 时内部直接返回）
    asyncio.create_task(system_monitor_loop())
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
app.include_router(local_exec.router, prefix="/api", tags=["Local Execution"])
app.include_router(gallery.router, prefix="/api", tags=["Gallery"])
app.include_router(game.router, prefix="/api", tags=["Multiplayer Game"])


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "version": "1.0.0"}


# 安全说明：不再挂载 web/ 静态目录 —— 产物页面（LLM 生成的 HTML）与 API 必须不同源，
# 否则恶意产物页面可带登录态窃取同源 API 数据（同源 XSS）。
# 产物由独立静态服务（如 python -m http.server 8001 --directory web）提供，
# 前端通过 NEXT_PUBLIC_ASSETS_URL / settings.public_assets_base 访问。
