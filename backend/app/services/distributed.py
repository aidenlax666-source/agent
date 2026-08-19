from __future__ import annotations
"""分布式协调层（云架构）：Redis 提供跨实例共享的锁/去重/调度。

设计：**可选依赖**——配置 REDIS_URL 时启用 Redis（多实例部署），
留空时自动回退进程内内存实现（单机模式，保持原有行为不变）。

这层封装了云架构需要的三个原语：
- 锁（SETNX + 过期）：任务执行锁，防止多实例重复执行同一任务
- 去重（带 TTL 的集合）：提醒/监控触发去重，跨实例生效
- 原子递增：调度计数等
"""

import logging
import os
import time
import uuid

from app.config import get_settings

logger = logging.getLogger("app.services.distributed")

_redis = None
_redis_error: str | None = None

# 本实例（worker）唯一标识：用于沙箱容器打标签 + 孤儿清理（谁启动的谁负责回收）
_worker_id: str = uuid.uuid4().hex[:12]


def get_worker_id() -> str:
    """返回本进程唯一 worker id（沙箱容器/孤儿清理用）。"""
    return _worker_id


def _get_redis():
    """惰性连接 Redis；失败记录错误并返回 None（回退单机模式）。"""
    global _redis, _redis_error
    if _redis is not None:
        return _redis
    if _redis_error:
        return None
    url = get_settings().redis_url.strip()
    if not url:
        return None
    try:
        import redis as _redis_mod
        _redis = _redis_mod.Redis.from_url(url, decode_responses=True, socket_timeout=3)
        _redis.ping()
        logger.info("[distributed] Redis 已连接（云架构多实例模式）")
        return _redis
    except Exception as e:
        _redis_error = f"{type(e).__name__}: {str(e)[:100]}"
        logger.warning("[distributed] Redis 不可用，回退单机内存模式: %s", _redis_error)
        return None


def redis_enabled() -> bool:
    return _get_redis() is not None


# ============================================================
# 1. 分布式锁（任务执行 / 调度抢占）
# ============================================================

# 单机模式用的内存锁表：{key: expiry_ts}
_local_locks: dict[str, float] = {}


def acquire_lock(key: str, ttl_seconds: int = 300) -> bool:
    """尝试获取锁。成功返回 True（调用方必须 release）。"""
    r = _get_redis()
    if r is not None:
        try:
            return bool(r.set(f"aiagent:lock:{key}", "1", nx=True, ex=ttl_seconds))
        except Exception:
            return True  # Redis 抖动时放行（避免任务卡死），靠 TTL 自愈
    # 单机模式：进程内锁
    now = time.time()
    expiry = _local_locks.get(key, 0)
    if expiry > now:
        return False  # 已锁
    _local_locks[key] = now + ttl_seconds
    return True


def release_lock(key: str) -> None:
    r = _get_redis()
    if r is not None:
        try:
            r.delete(f"aiagent:lock:{key}")
        except Exception:
            pass
        return
    _local_locks.pop(key, None)


def renew_lock(key: str, ttl_seconds: int = 300) -> None:
    """长任务续锁（防止锁在任务执行期间过期被别的实例抢走）。"""
    r = _get_redis()
    if r is not None:
        try:
            r.expire(f"aiagent:lock:{key}", ttl_seconds)
        except Exception:
            pass


# ============================================================
# 2. 分布式去重（提醒/监控触发：跨实例只发一次）
# ============================================================

_local_set: set[tuple[str, float]] = set()  # (key, expiry_ts)


def dedup_mark(key: str, ttl_seconds: int) -> bool:
    """标记 key 在 ttl 内已触发。返回 True 表示"这是第一次"（应触发）。"""
    r = _get_redis()
    if r is not None:
        try:
            return bool(r.set(f"aiagent:dedup:{key}", "1", nx=True, ex=ttl_seconds))
        except Exception:
            pass
    # 单机模式
    now = time.time()
    # 清理过期
    expired = [k for k, exp in list(_local_set) if exp <= now]
    for k in expired:
        _local_set.discard(k)
    if any(k == key for k, _ in _local_set):
        return False
    _local_set.add((key, now + ttl_seconds))
    return True


def dedup_clear(key: str) -> None:
    global _local_set
    r = _get_redis()
    if r is not None:
        try:
            r.delete(f"aiagent:dedup:{key}")
        except Exception:
            pass
        return
    _local_set = {x for x in _local_set if x[0] != key}


# ============================================================
# 3. 分布式调度（原子抢占）
# ============================================================

def scheduler_claim(task_id: str, expected_next: float, new_next: float) -> bool:
    """定时任务抢占：仅当 next_run_at 仍是期望值时更新（多实例不重复执行）。

    Redis 用 WATCH/MULTI 或 Lua；简化用 lock + 数据库原子更新兜底。
    这里返回 True 表示"可以执行"（锁获取成功）。
    """
    return acquire_lock(f"sched:{task_id}", ttl_seconds=120)


def scheduler_release(task_id: str) -> None:
    release_lock(f"sched:{task_id}")


# ============================================================
# 3.5 调度器 leader 选举（多实例只一个实例跑调度循环）
# ============================================================

LEADER_KEY = "aiagent:leader:scheduler"
LEADER_TTL = 60  # leader 租约 TTL（秒）；失联后自动过期，其他实例接管


def try_become_leader(ttl_seconds: int = LEADER_TTL) -> bool:
    """尝试成为调度 leader（SETNX）。成功返回 True（本实例负责跑调度循环）。

    单机模式（无 Redis）返回 True：本来就只有本实例，直接当 leader。
    """
    r = _get_redis()
    if r is None:
        return True
    try:
        return bool(r.set(LEADER_KEY, get_worker_id(), nx=True, ex=ttl_seconds))
    except Exception:
        return True  # Redis 抖动时放行（单机行为兜底）


def renew_leadership(ttl_seconds: int = LEADER_TTL) -> bool:
    """续期 leader 租约；返回是否仍是 leader（true=续期成功或单机模式）。"""
    r = _get_redis()
    if r is None:
        return True
    try:
        # 只有自己持有才续期（防误续别人的租约）
        if r.get(LEADER_KEY) == get_worker_id():
            r.expire(LEADER_KEY, ttl_seconds)
            return True
    except Exception:
        return True
    return False


# ============================================================
# 4. 分布式任务队列（真正的云架构：任务进 Redis 队列，worker 消费）
# ============================================================

TASK_QUEUE_KEY = "aiagent:queue:tasks"
# 高优先级队列：高优任务（如用户主动提交、紧急迭代）先于普通任务消费
TASK_QUEUE_KEY_HIGH = "aiagent:queue:tasks:high"
# 任务执行状态记录（BRPOP 后标记，防 worker 崩溃任务静默丢失）
TASK_LEASE_KEY = "aiagent:task-lease"


def enqueue_task(task_id: str, priority: str = "normal") -> bool:
    """任务入队（LPUSH）。priority: normal | high（高优先消费）。
    返回是否成功；无 Redis 返回 False（调用方走单机模式）。"""
    r = _get_redis()
    if r is None:
        return False
    key = TASK_QUEUE_KEY_HIGH if priority == "high" else TASK_QUEUE_KEY
    try:
        r.lpush(key, task_id)
        return True
    except Exception as e:
        logger.warning("[distributed] 任务入队失败: %s", str(e)[:100])
        return False


def dequeue_task(timeout: float = 5.0) -> str | None:
    """从队列取一个任务（BRPOP，阻塞 timeout 秒）。无任务返回 None。

    优先级：先阻塞等高优队列（timeout 的 1/4），再等普通队列——
    高优任务随时插队，普通任务不饿死（高优空时照样消费普通）。
    """
    r = _get_redis()
    if r is None:
        return None
    try:
        # 先短等高优队列
        item = r.brpop(TASK_QUEUE_KEY_HIGH, timeout=min(timeout, 2.0))
        if item:
            return item[1]
        # 再等普通队列（剩余时间）
        remaining = max(timeout - min(timeout, 2.0), 0.1)
        item = r.brpop(TASK_QUEUE_KEY, timeout=remaining)
        if item:
            return item[1]
    except Exception as e:
        logger.warning("[distributed] 取任务失败: %s", str(e)[:100])
    return None


def task_in_queue(task_id: str) -> bool:
    """任务是否仍在队列中等待（未被 worker 取出）。

    崩溃恢复用：BRPOP 取出后任务即不在队列；若租约也过期 → 失联，需重新入队。
    无 Redis（单机模式）返回 False（本实例进程内调度，无队列概念）。
    """
    r = _get_redis()
    if r is None:
        return False
    try:
        for key in (TASK_QUEUE_KEY, TASK_QUEUE_KEY_HIGH):
            items = r.lrange(key, 0, -1)
            if task_id in items:
                return True
        return False
    except Exception:
        return False  # Redis 抖动时保守判断"不在队列"，由租约/重试次数兜底


def claim_task_lease(task_id: str, ttl_seconds: int = 1800) -> bool:
    """领取任务执行租约（防多 worker 同时执行同一任务 + worker 崩溃后任务被找回）。"""
    return acquire_lock(f"task-run:{task_id}", ttl_seconds=ttl_seconds)


def release_task_lease(task_id: str) -> None:
    release_lock(f"task-run:{task_id}")


def task_lease_alive(task_id: str) -> bool:
    """任务执行租约是否仍存在（worker 是否还在跑该任务）。

    孤儿沙箱清理用：容器打的 task 标签对应的租约已过期/被删 =
    任务已结束或 worker 崩溃 → 容器可以安全回收。
    """
    r = _get_redis()
    if r is not None:
        try:
            return bool(r.exists(f"aiagent:lock:task-run:{task_id}"))
        except Exception:
            return True  # Redis 抖动时保守不清（宁可残留不可误杀）
    return any(k == f"task-run:{task_id}" for k, _exp in _local_locks.items())


# ============================================================
# 5. 全局沙箱并发信号量（跨实例精确限制）
# ============================================================

# 槽位 key 前缀：aiagent:sandbox:slot:{i}，value = 占用者 worker_id
_SANDBOX_SLOT_PREFIX = "aiagent:sandbox:slot"


def acquire_sandbox_slot(limit: int, ttl_seconds: int = 1800) -> int | None:
    """占用一个全局沙箱槽位（SETNX + TTL，崩溃自动释放）。

    返回槽位号（0-based）；全部被占返回 None（调用方排队重试）。
    无 Redis（单机模式）返回 0：全局限制不生效，由本地信号量兜底。
    """
    r = _get_redis()
    if r is None:
        return 0
    owner = get_worker_id()
    try:
        for i in range(limit):
            if r.set(f"{_SANDBOX_SLOT_PREFIX}:{i}", owner, nx=True, ex=ttl_seconds):
                return i
    except Exception as e:
        logger.warning("[distributed] 沙箱槽位占用失败: %s", str(e)[:100])
        return 0  # Redis 抖动时放行（本地信号量仍限制本实例并发）
    return None


def renew_sandbox_slot(slot: int, ttl_seconds: int = 1800) -> None:
    """续期自己占用的槽位（长任务防 TTL 到期被别的实例抢走）。"""
    r = _get_redis()
    if r is None:
        return
    try:
        r.expire(f"{_SANDBOX_SLOT_PREFIX}:{slot}", ttl_seconds)
    except Exception:
        pass


def release_sandbox_slot(slot: int) -> None:
    """释放自己占用的槽位。"""
    r = _get_redis()
    if r is None:
        return
    try:
        r.delete(f"{_SANDBOX_SLOT_PREFIX}:{slot}")
    except Exception:
        pass


# ============================================================
# 7. 可观测性：worker 心跳 + 队列深度（运维/监控用）
# ============================================================

WORKER_HEARTBEAT_PREFIX = "aiagent:worker"
WORKER_HEARTBEAT_TTL = 120  # 心跳 TTL（秒）；超过视为 worker 失联


def worker_heartbeat(worker_id: str | None = None) -> None:
    """注册/刷新本 worker 心跳（带 TTL，失联自动消失）。无 Redis 时无操作。"""
    r = _get_redis()
    if r is None:
        return
    wid = worker_id or get_worker_id()
    try:
        r.set(f"{WORKER_HEARTBEAT_PREFIX}:{wid}", str(time.time()), ex=WORKER_HEARTBEAT_TTL)
    except Exception:
        pass


def list_workers() -> list[dict]:
    """列出活跃 worker（心跳未过期）。返回 [{id, last_heartbeat}]。"""
    r = _get_redis()
    if r is None:
        return []
    try:
        keys = r.keys(f"{WORKER_HEARTBEAT_PREFIX}:*")
        workers = []
        for k in keys or []:
            try:
                raw = r.get(k)
                if raw is None:
                    continue  # 心跳已过期（TTL 自清理）
                workers.append({"id": k.rsplit(":", 1)[-1], "last_heartbeat": float(raw)})
            except Exception:
                continue
        return workers
    except Exception:
        return []


def queue_depth() -> dict:
    """当前队列深度（普通/高优）。无 Redis 返回空。"""
    r = _get_redis()
    if r is None:
        return {}
    try:
        return {
            "normal": int(r.llen(TASK_QUEUE_KEY) or 0),
            "high": int(r.llen(TASK_QUEUE_KEY_HIGH) or 0),
        }
    except Exception:
        return {}


# ============================================================
# 6. 全局限流（跨实例共享计数器，防每实例各自计数被绕过）
# ============================================================

def rate_limit(key: str, limit: int, window_seconds: int = 60) -> bool:
    """滑动窗口限流：返回 True 表示**放行**，False 表示超限。

    Redis：INCR + EXPIRE（首次设置 TTL），key 计数跨实例共享。
    单机模式：进程内计数（与现状一致，多实例不共享但单实例正确）。
    """
    r = _get_redis()
    if r is not None:
        try:
            k = f"aiagent:ratelimit:{key}"
            n = r.incr(k)
            if n == 1:
                r.expire(k, window_seconds)
            return n <= limit
        except Exception as e:
            logger.warning("[distributed] 限流计数失败（放行）: %s", str(e)[:100])
            return True  # Redis 抖动时放行，避免误伤
    # 单机模式：进程内计数
    now = time.time()
    if key not in _rate_counts or now - _rate_counts[key][1] > window_seconds:
        _rate_counts[key] = [1, now]
        return 1 <= limit
    _rate_counts[key][0] += 1
    return _rate_counts[key][0] <= limit


_rate_counts: dict[str, list] = {}  # key -> [count, window_start_ts]（单机模式）
