# 云服务器部署（Cloud Deployment）

本文说明如何把系统从本地单机模式升级为云服务器多实例架构。

## 架构对比

| | 单机模式（默认） | 云架构（配置 REDIS_URL） |
|---|---|---|
| 任务锁 | 进程内 dict | Redis `SETNX` + 过期 |
| 提醒/监控去重 | 进程内 dict | Redis 带 TTL key（跨实例只触发一次） |
| 调度抢占 | SQLite 原子更新 | SQLite 原子 + Redis 锁双保险 |
| 运行实例 | 1 个 uvicorn | 多 worker / 多台服务器 |
| 适用 | 本地个人使用 | 云服务器 / 团队使用 |

核心原则：**不配 REDIS_URL 就是原来的单机行为**，配了自动启用分布式协调——平滑升级，旧功能不受影响。

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

### 4. 部署拓扑

```
客户端 ──▶ Nginx/LB（HTTPS）
              ├──▶ API 实例 1（uvicorn :8000）
              ├──▶ API 实例 2（uvicorn :8000）  ← Redis 协调去重/锁
              └──▶ 静态资源（assets :8001，web/ 产物）
                     └── Redis（锁/去重）
                     └── SQLite/PostgreSQL（数据）
```

## 数据库迁移（可选，高并发推荐）

SQLite 适合单机；多实例高并发建议迁移 PostgreSQL：

1. 用 SQLAlchemy 重写 `database.py` 的连接层（当前是原生 sqlite3）
2. 或加一个兼容层：`DATABASE_URL` 支持 `postgresql://`，其余 SQL 基本兼容（SQLite/PostgreSQL 语法差异小）

> 当前 `database.py` 是原生 sqlite3，迁移 PostgreSQL 需要改连接层（较大改动，
> 列入后续工作；单机/低并发 SQLite + WAL 完全够用）。

## 已知取舍（诚实声明）

- **Redis 非必需**：不配即单机模式（向后兼容）
- **进程内 `_TASKS` 缓存**：任务状态最终落 SQLite，多实例读 DB 一致；内存表只是缓存
- **沙箱并发**：`SANDBOX_MAX_CONCURRENCY`（默认 3）是全局资源保护，多实例各自限制，
  总并发 = 实例数 × 3（如需精确全局限制可用 Redis 信号量——后续可加）
