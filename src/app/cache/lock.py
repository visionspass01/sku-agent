"""Redis 锁：**异步锁走 redis.asyncio**，同步短锁只给同步上下文用。

为什么锁要单独一套（评审 v3-①）：
会话锁在请求入口、`await` 之前拿；顺序锁（巡检）在协程里拿。如果它们用**同步** redis 客户端，
`socket_timeout`（默认 3s）一到就把整个事件循环冻住——所有 SSE 心跳与其它会话一起停摆。
所以：
- `redis_lock(...)`：真异步（`await client.set/get/delete` + `asyncio.sleep` 轮询），
  失败仍区分 `LockTimeoutError`（别人持有锁 → 业务冲突 409）与 `LockBackendError`（后端不可用 → fail-closed）；
- `async_short_lock(...)`：异步短锁（异步 drain 用），不调用 `time.sleep`；
- `short_lock(...)`：同步短锁（`_write_document` / `rerender_topic` / 脚本用），
  **只能在同步上下文或工作线程里调用**（在事件循环里调用会阻塞循环，见各调用点注释）。

释放策略不变：持有者 token 比对后删除；释放失败靠 TTL 兜底，绝不覆盖业务异常。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager

from app.cache.redis_factory import get_async_redis, get_redis
from app.config import get_settings

logger = logging.getLogger("sku-agent.lock")


class LockTimeoutError(RuntimeError):
    pass


class LockBackendError(RuntimeError):
    """锁后端（Redis）不可用。

    为什么单独一类：`LockTimeoutError` 表示「别人持有锁」（业务语义是 409 冲突），
    而这里表示「锁服务本身挂了」——两者混在一起时，Redis 抖动会被误报成"你在并发对话"，
    监控上也看不出真实的故障面。调用方据此决定 fail-open 还是 fail-closed。
    """


# ---------------------------------------------------------------- 异步锁（请求入口 / 协程内）
@asynccontextmanager
async def redis_lock(
    key: str,
    ttl_ms: int = 120_000,
    wait_timeout: float = 0.0,
    poll_interval: float = 0.05,
):
    """异步分布式锁：`SET NX PX` + token 比对释放，轮询用 `asyncio.sleep`。"""
    try:
        client = get_async_redis()
        token = uuid.uuid4().hex
        deadline = time.monotonic() + wait_timeout
        acquired = bool(await client.set(key, token, nx=True, px=ttl_ms))
        while not acquired:
            if wait_timeout <= 0 or time.monotonic() >= deadline:
                raise LockTimeoutError(f"获取锁超时: {key}")
            await asyncio.sleep(poll_interval)
            acquired = bool(await client.set(key, token, nx=True, px=ttl_ms))
    except LockTimeoutError:
        raise
    except Exception as exc:  # noqa: BLE001 —— Redis 连不上/命令失败都属于"锁后端不可用"
        raise LockBackendError(f"锁后端不可用：{type(exc).__name__}: {exc}") from exc
    try:
        yield
    finally:
        try:
            current = await client.get(key)
            if isinstance(current, bytes):
                current = current.decode()
            if current == token:
                await client.delete(key)
        except Exception as exc:  # noqa: BLE001 —— 释放失败靠 TTL 兜底，不能覆盖业务异常
            logger.warning("释放 Redis 锁失败（靠 TTL 过期兜底）：%s", exc)


@asynccontextmanager
async def async_short_lock(key: str, ttl_ms: int = 5_000, wait_timeout: float = 0.2):
    """异步短锁：`async with async_short_lock(k) as got:`，`got=False` 表示没抢到（**不要覆盖**）。

    进程内用 `asyncio.Lock`；配了 `REDIS_URL`（多副本）时再叠一层异步 `SET NX PX`。
    与同步版语义一致，只是等待用 `asyncio.sleep`——不会像 `time.sleep` 那样卡住事件循环。
    """
    local = _async_local_lock(key)
    try:
        await asyncio.wait_for(local.acquire(), timeout=max(0.0, wait_timeout))
    except (asyncio.TimeoutError, TimeoutError):
        yield False
        return
    token: str | None = None
    client = None
    try:
        if get_settings().redis_url:
            client = get_async_redis()
            token = uuid.uuid4().hex
            deadline = time.monotonic() + max(0.0, wait_timeout)
            while True:
                try:
                    if await client.set(f"lock:{key}", token, nx=True, px=ttl_ms):
                        break
                except Exception as exc:  # noqa: BLE001 —— Redis 故障时退化为进程内锁
                    logger.warning("获取 Redis 短锁失败（退化为进程内锁）：%s", exc)
                    token = None
                    break
                if time.monotonic() >= deadline:
                    token = None
                    yield False
                    return
                await asyncio.sleep(0.02)
        yield True
    finally:
        if token is not None and client is not None:
            try:
                current = await client.get(f"lock:{key}")
                if isinstance(current, bytes):
                    current = current.decode()
                if current == token:
                    await client.delete(f"lock:{key}")
            except Exception:  # noqa: BLE001 —— 释放失败靠 TTL 兜底
                pass
        if local.locked():
            local.release()


_ASYNC_LOCAL_LOCKS: dict[str, asyncio.Lock] = {}
_ASYNC_LOCAL_GUARD = threading.Lock()
_ASYNC_LOCK_LIMIT = 1000


def _async_local_lock(key: str) -> asyncio.Lock:
    with _ASYNC_LOCAL_GUARD:
        lock = _ASYNC_LOCAL_LOCKS.get(key)
        if lock is None:
            if len(_ASYNC_LOCAL_LOCKS) > _ASYNC_LOCK_LIMIT:
                _ASYNC_LOCAL_LOCKS.clear()
            lock = asyncio.Lock()
            _ASYNC_LOCAL_LOCKS[key] = lock
        return lock


def is_locked(key: str) -> bool:
    try:
        return bool(get_redis().exists(key))
    except Exception as exc:  # noqa: BLE001 —— 探活失败按"锁后端不可用"处理
        raise LockBackendError(f"锁后端不可用：{type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------- 同步短锁（档案 CAS 写用）
# 为什么单独一个同步版本：`org_memory.save_analysis` 是同步函数（可能跑在线程池里），
# 用不上异步版本；而且这里要的是**毫秒级短锁**——只包住「读哈希 → 校验 → 原子替换」，
# 长耗时的合并 / 模型调用必须在锁外，否则并发写会互相排队（Agent 轮次最怕这种无谓等待）。
# 注意：**不要在事件循环里直接调用**（内部是同步 redis + time.sleep）；
# 需要异步场景请用 `async_short_lock`，或把整个同步函数丢进 `asyncio.to_thread`。
_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_GUARD = threading.Lock()
_LOCAL_LOCK_LIMIT = 1000


def _local_lock(key: str) -> threading.Lock:
    with _LOCAL_GUARD:
        lock = _LOCAL_LOCKS.get(key)
        if lock is None:
            if len(_LOCAL_LOCKS) > _LOCAL_LOCK_LIMIT:   # 兜底：topic 数量异常增长时整体重建
                _LOCAL_LOCKS.clear()
            lock = threading.Lock()
            _LOCAL_LOCKS[key] = lock
        return lock


@contextmanager
def short_lock(key: str, ttl_ms: int = 5_000, wait_timeout: float = 0.2):
    """同步短锁：`with short_lock(k) as got:`，`got=False` 表示没抢到（**不要覆盖**）。

    进程内用 `threading.Lock`；配了 `REDIS_URL`（多副本）时再叠一层 `SET NX PX`。
    """
    local = _local_lock(key)
    if not local.acquire(timeout=max(0.0, wait_timeout)):
        yield False
        return
    token: str | None = None
    try:
        if get_settings().redis_url:
            client = get_redis()
            token = uuid.uuid4().hex
            deadline = time.monotonic() + max(0.0, wait_timeout)
            while True:
                try:
                    if client.set(f"lock:{key}", token, nx=True, px=ttl_ms):
                        break
                except Exception as exc:  # noqa: BLE001 —— Redis 故障时退化为进程内锁
                    logger.warning("获取 Redis 短锁失败（退化为进程内锁）：%s", exc)
                    token = None
                    break
                if time.monotonic() >= deadline:
                    token = None
                    yield False
                    return
                time.sleep(0.02)
        yield True
    finally:
        if token is not None:
            try:
                current = get_redis().get(f"lock:{key}")
                if isinstance(current, bytes):
                    current = current.decode()
                if current == token:
                    get_redis().delete(f"lock:{key}")
            except Exception:  # noqa: BLE001 —— 释放失败靠 TTL 兜底
                pass
        local.release()
