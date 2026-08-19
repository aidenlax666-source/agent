# 云服务器部署（Cloud Deployment）

本文说明如何把系统从本地单机模式升级为云服务器多实例架构。

## 架构对比

| | 单机模式（默认） | 云架构（配置 REDIS_URL） |
|---|---|---|
| 任务锁 | 进程内 dict | Redis `SETNX` + 过期 |
| 提醒/监控去重 | 进程内 dict | Redis 带 TTL key（跨实例只触发一次） |
| 调度抢占 | SQLite 原子更新 | SQLite 原子 + Redis 锁双保险 |
| **任务执行** | 进程内 asyncio（本实例跑） | **Redis 队列**：提交入队 → worker 消费（多实例自动负载均衡） |
| **任务恢复** | 进程内取消即停 | **reaper 自动重入队** + 重试上限死信（无需手动） |
| **沙箱目录** | 各实例本地（browser_profile/web） | 共享卷（`SANDBOX_PROFILE_ROOT`/`ASSET_WEB_ROOT`） |
| **沙箱并发** | 每实例各自限 | 全局精确限制（`SANDBOX_GLOBAL_CONCURRENCY`，Redis 信号量） |
| **孤儿沙箱** | 进程内取消即清理 | 容器标签 + 启动回收（任务租约判断） |
| **匿名限流** | 进程内计数 | Redis 计数器跨实例共享（`rate_limit`） |
| **队列优先级** | 无 | 高优/普通双队列（`priority=high` 插队） |
| **数据层** | SQLite（默认） | PostgreSQL 可选（`DATABASE_URL`，自动适配） |
| 运行实例 | 1 个 uvicorn | 多 worker / 多台服务器 |
| 适用 | 本地个人使用 | 云服务器 / 团队使用 |

核心原则：**不配 REDIS_URL 就是原来的单机行为**，配了自动启用分布式协调——平滑升级，旧功能不受影响。

## 分布式任务队列（云架构核心）

配置 REDIS_URL 后，任务提交走 **Redis 队列**（不再是进程内 asyncio）：

```
任务提交（submit）
   │  ① SQLite 落库（任务持久化）
   │  ② LPUSH → Redis 队列（aiagent:queue:tasks）
   ▼
Worker（每实例一个，BRPOP 竞争消费）
   │  ③ 领取执行租约（防多 worker 执行同一任务）
   │  ④ 从 SQLite 重建任务上下文
   │  ⑤ 执行 _run_task（内存/沙箱/LLM 全流程）
   │  ⑥ 期间每 30s 心跳续租约（防长任务被抢）
   ▼
状态写回 SQLite（任务结果/进度，前端可查）
```

**特性**：
- **任务不丢**：提交即落 SQLite + 入队——worker 崩溃任务仍在队列/库中
- **自动负载均衡**：多个 worker 从同一队列 BRPOP 竞争，谁空闲谁拿（天然分发）
- **多实例不重复执行**：BRPOP 互斥 + 租约双保险
- **崩溃自动恢复**：reaper 循环扫描失联任务（租约过期 + 不在队列）→ 自动重新
  入队，重试超限（3 次）标记死信失败——不再需要手动重跑

## 本地单机（现状，无需改动）

```bash
# .env 不配 REDIS_URL（或留空）
# 直接启动即可，全部走进程内协调
```

## 云服务器部署

### 1. 基础设施

```bash
# Docker 一键（含 redis）
docker-compose up -d redis backend frontend assets
```

### 2. 配置 .env

```env
REDIS_URL=redis://redis:6379/0   # docker-compose 内部服务名
# 或外部 redis: REDIS_URL=redis://your-redis-host:6379/0
JWT_SECRET_KEY=<随机长字符串>      # 多实例必须共享同一密钥
PUBLIC_API_BASE=https://api.your-domain.com
CORS_ORIGINS=https://your-domain.com
TRUSTED_PROXY_IPS=<负载均衡 IP>
SANDBOX_HEADFUL=false             # 服务器无显示器，关有头浏览器
```

### 3. 多实例启动（横向扩容）

```bash
# 单机多 worker（同一台服务器）
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4

# 多台服务器：每台都跑上述命令，Redis 保证任务不重复执行
```

> ⚠️ 注意：SQLite 仍为单文件。多实例共享 SQLite 在云环境建议挂载到共享存储
> （或迁移 PostgreSQL——见下方"数据库迁移"）。

## CI/CD（GitHub Actions）

仓库已内置 `.github/workflows/ci.yml`，三条流水线：

| 触发 | 流水线 | 作用 |
|---|---|---|
| PR / push 到 main | `backend-test` | Python 3.11 装依赖 → pytest 全量（FakeRedis 模拟云模式，无需真实 Redis） |
| PR / push 到 main | `frontend-build` | Next.js 生产构建（类型检查 + 编译） |
| push 到 main（前两者通过后） | `build-images` | 构建 backend/frontend 镜像 → 推送到 GHCR（`ghcr.io/<repo>/backend:latest`） |

服务器部署时可 `docker pull ghcr.io/<repo>/backend:latest` 拉取，或接入
Watchtower/ArgoCD 实现自动更新。

## HTTPS（Nginx + Let's Encrypt）

`deploy/` 目录内置完整 HTTPS 三件套：Nginx 反代配置（HTTP→HTTPS 跳转、
TLS 加固、安全响应头、前端/API/产物三路转发、静态长缓存、Range 流式）、
certbot 首次签发脚本、部署说明。快速开始：

```bash
sudo bash deploy/init-certbot.sh your-domain.com your@email.com   # 签发+自动续期
sudo cp deploy/nginx-https.conf /etc/nginx/sites-available/aiagent
sudo ln -sf /etc/nginx/sites-available/aiagent /etc/nginx/sites-enabled/aiagent
sudo nginx -t && sudo systemctl reload nginx
```

详见 `deploy/README.md`。启用后后端 `.env` 需同步：
`TRUSTED_PROXY_IPS=127.0.0.1,::1`（信任本机 Nginx 的 XFF，防伪造 IP 绕限速）。

### 4. 部署拓扑

```
客户端 ──▶ Nginx/LB（HTTPS）
              ├──▶ API 实例 1（uvicorn :8000）
              ├──▶ API 实例 2（uvicorn :8000）  ← Redis 协调去重/锁/队列
              └──▶ 静态资源（assets :8001，web/ 产物）
                     └── Redis（锁/去重/队列/沙箱信号量）
                     └── 共享存储（登录态 + 产物，见下）
                     └── SQLite/PostgreSQL（数据）
```

### 5. 沙箱设计（多实例关键）

多实例下沙箱不能再用"每实例本地磁盘"，否则跨实例互相看不见。三项配置：

| 配置 | 默认（单机） | 云架构（多实例） | 作用 |
|---|---|---|---|
| `SANDBOX_PROFILE_ROOT` | `backend/browser_profile` | 共享卷路径（NFS/EFS/对象存储同步目录） | 登录态跨实例可见：用户在实例 A 登录，任务被实例 B 消费也能读到 |
| `ASSET_WEB_ROOT` | 仓库根 `web/` | 共享卷路径 | 产物跨实例可见：产物写在 B 的磁盘，用户从 A 下载也能读到 |
| `SANDBOX_GLOBAL_CONCURRENCY` | 0（每实例各自限 `SANDBOX_MAX_CONCURRENCY`） | 如 6 | 全局并发精确限制：总并发 = 该值，不再 = 实例数 × 单实例并发 |

```env
# 云架构 .env 追加
SANDBOX_PROFILE_ROOT=/mnt/shared/profiles   # 共享卷挂载点
ASSET_WEB_ROOT=/mnt/shared/web              # 共享卷挂载点
SANDBOX_GLOBAL_CONCURRENCY=6                # 全集群最多 6 个沙箱并发
```

**全局并发实现**：Redis 槽位信号量（`aiagent:sandbox:slot:{0..N-1}`，SETNX + TTL）。
worker 崩溃后槽位 TTL 自动过期释放，不永久占位；执行期间无需续期
（槽位 TTL = 单次执行超时上限 1800s）。

**孤儿沙箱回收**：每个沙箱容器打标签（`aiagent.worker` / `aiagent.task`）。
实例启动时清理：
- 本实例之前启动的容器（重启后必是孤儿）
- 其他实例的容器：仅当带任务标签且该任务**执行租约已不存在**（= 任务结束或 worker 崩溃）才回收
- 宁可保守不可误杀（Redis 抖动时不清理）

**临时目录不共享**：脚本/输出中转仍在各实例本地 `backend/tmp`
（产物最终落到共享 `web/`）——共享卷 IO 慢且放大临时垃圾，本地反而更安全。

## 性能与成本（可选优化）

| 优化 | 说明 | 配置 |
|---|---|---|
| **PG 连接池** | pg 模式连接复用（ThreadedConnectionPool，避免每次操作新建连接/握手） | 自动启用，无需配置 |
| **任务完整日志** | DB 只存截断错误；完整 stdout/脚本落 `web/logs/{task_id}.log`（可下载排查，不用重跑任务） | 自动启用 |
| **沙箱预构建镜像** | 默认镜像每次任务首次运行要装 Playwright，慢；预构建好秒级就绪 | 见下 |

**沙箱预构建镜像**（worker 秒级就绪）：

```bash
cd backend
docker build -f Dockerfile.sandbox -t python:3.11-slim-aiagent .
# .env 配置
SANDBOX_IMAGE=python:3.11-slim-aiagent
```

**任务完整日志下载**：任务详情返回 `log_file` 字段（`logs/{task_id}.log`），
前端可加"查看执行日志"入口；日志与产物同目录，走产物静态服务。

## 数据库迁移（可选，高并发推荐）

SQLite 适合单机；多实例高并发建议迁移 PostgreSQL。**已内置兼容层**：
`DATABASE_URL=postgresql://user:pass@host:5432/dbname` 即自动走 PostgreSQL，
业务 SQL 不用改（占位符 `?` 自动转 `%s`，`PRAGMA` 跳过，列检查走
information_schema）。不配 DATABASE_URL 则保持 SQLite（默认）。

```env
# 云架构 .env 追加
DATABASE_URL=postgresql://aiagent:aiagent@postgres:5432/aiagent  # docker-compose 服务名
```

```bash
# Docker 一键（含 redis + postgres）
docker-compose up -d redis postgres backend frontend assets
```

> ⚠️ SQLite 模式多实例共享需挂载共享存储；迁移 PostgreSQL 后多实例直接共享
> （数据层无单文件限制）。实现：`backend/app/db_adapter.py`（psycopg2 兼容层）+ 
> `_get_conn()` 按配置分发。

## 混合架构（本地执行模式）

**问题**：云端沙箱在服务器，写不到用户本地磁盘。需求"在 D:/我的文档 创建 报告.txt"
这类**本地文件操作**任务无法在云端完成。

**方案**：混合架构——云端 main 分布式处理普通任务；本地文件操作类任务**自动识别**
并派发给**用户本地的 exe 端**（`local_worker.exe`）在用户自己电脑上执行。

```
用户提交："在 D:/我的文档 创建 报告.txt"
   │  submit 自动识别（含"本地/创建文件/路径"等意图）
   ▼
本地队列 aiagent:queue:local:{user_id}（Redis，按用户隔离）
   │  用户本地的 local_worker.exe 轮询 /api/local/tasks/poll 领取
   ▼
云端懒生成"本地操作脚本"（Python 标准库，LLM）→ 返回给 exe
   ▼
exe 本地执行（内置 AST 扫描 + 子进程隔离 + 超时清理）
   │  产物直接写在用户磁盘
   ▼
POST /api/local/tasks/report 回传 → 云端标记 done/failed，任务详情可见
```

### 自动识别规则（无需用户开关）

需求命中以下任意意图即自动走本地执行：
- 明确词：本地 / 我的电脑 / 本地文件 / 在本地 / 保存到本地 / 我的文档
- 文件操作：创建文件 / 创建文件夹 / 新建文件 / 写文件 / 批量重命名 / 整理文件夹
- 路径：`D:/` `D:\` `C:/` `C:\`
- 组合："创建/生成/新建" + "文件/.txt/.csv/.xlsx"

### 本地端使用（用户侧）

```bash
# 1. 打包 exe（Windows，开发者在仓库根目录执行一次）
powershell -ExecutionPolicy Bypass -File build_local_worker.ps1
# 产物：dist/local_worker.exe（约 10-15MB）

# 2. 用户拿到 exe 后运行（填云端地址 + 自己网页登录的 token）
local_worker.exe --server https://your-domain.com --token <JWT>
# 之后保持运行：自动轮询领取自己的本地任务并执行
```

**安全**：exe 内置 AST 扫描（拦 eval/exec/命令注入/网络外联/危险删除），
脚本在独立临时目录执行、超时/进程树清理；token 绑定账号，只领自己的任务。

**云端 API**（JWT 认证，只操作自己的队列）：
- `POST /api/local/tasks/poll`：领取一个本地任务（BRPOP，短阻塞）
- `POST /api/local/tasks/report`：回传结果（stdout/stderr/退出码/产物路径）

## 已知取舍（诚实声明）

- **Redis 非必需**：不配即单机模式（向后兼容）
- **进程内 `_TASKS` 缓存**：任务状态最终落 SQLite，多实例读 DB 一致；内存表只是缓存
- **SQLite 单文件**：多实例共享需挂载共享存储或迁移 PostgreSQL（见上）
- **子进程回退模式无容器孤儿回收**：`cleanup_orphan_containers` 只对 Docker 容器生效；
  宿主子进程回退（Windows 本地开发）下 worker 崩溃的残留进程由进程树超时/取消逻辑兜底，
  云生产环境必须启用 Docker（SECURITY.md 已有此要求）
