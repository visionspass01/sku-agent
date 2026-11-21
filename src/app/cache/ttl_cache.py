"""TTL 缓存：带随机偏移的旁路缓存（Redis 缓存雪崩优化）。

关键设计：
- TTL 随机偏移 TTL_eff = TTL * (1 + uniform(0, jitter_ratio))，避免大量 key 同一秒过期；
- 命中/未命中计数用于看板展示；
- 未命中时同步回源后写缓存，回源函数可抛错，不吞异常。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.cache.redis_factory import get_redis
from app.config import Settings, get_settings

logger = logging.getLogger("sku-agent.cache")

# 缓存是旁路：Redis 挂掉时**必须当 miss 继续走回源**，不能让对话/检索直接 500。
_REDIS_ERRORS: tuple[type[BaseException], ...] = ()
try:  # redis-py 的异常类型；fakeredis 也复用同一套
    from redis.exceptions import RedisError as _RedisError

    _REDIS_ERRORS = (_RedisError,)
except Exception:  # noqa: BLE001
    _REDIS_ERRORS = ()


class _OwnerCancelledError(Exception):
    """「回源者被取消」的专用信号：只发给等待者，永远不发给回源者自己（复审 Q1）。

    为什么不直接往共享 future 里塞 `CancelledError`：`asyncio.shield` 只能挡住
    「等待者自己被取消」，挡不住「共享 future 带着取消完成」。于是回源者的客户端断开
    （关页 / 轮次超时）时，同一 key 的其它等待者也会收到 `CancelledError`——
    表现就是「别人的请求把我也断了」，而且 SSE 里连一条 `fatal` 都没有。
    """


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    writes: int = 0
    ttl_samples: list[int] = field(default_factory=list)

    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else 0.0

    def snapshot(self) -> dict:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "hit_rate": self.hit_rate(),
        }


class TTLCache:
    def __init__(
        self,
        client=None,
        jitter_ratio: float | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self.client = client or get_redis()
        self.jitter_ratio = (
            jitter_ratio if jitter_ratio is not None else self._settings.cache_jitter_ratio
        )
        self.stats = CacheStats()
        self.degraded = False
        self.degraded_reason = ""
        # 单飞（single-flight）：同 key 的并发 miss 只回源一次。
        # TTL 抖动只解决「大量 key 同时过期」，解决不了「同一个 key 被并发打穿」
        # （例如 3 个会话同时问同一个 SKU，会同时打 3 次中台）。
        self._inflight: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Future]] = {}

    def _single_flight(self, key: str) -> tuple[asyncio.Future, bool]:
        """返回 (该 key 的共享 future, 自己是否是回源者)。跨事件循环不复用。"""
        loop = asyncio.get_running_loop()
        entry = self._inflight.get(key)
        if entry is not None:
            entry_loop, fut = entry
            if entry_loop is loop and not fut.done():
                return fut, False
        fut: asyncio.Future = loop.create_future()
        self._inflight[key] = (loop, fut)
        return fut, True

    def effective_ttl(self, ttl: int) -> int:
        """TTL 随机偏移：在 [ttl, ttl*(1+jitter)] 间取值。"""
        eff = int(ttl * (1 + random.uniform(0, self.jitter_ratio)))
        eff = max(1, eff)
        self.stats.ttl_samples.append(eff)
        if len(self.stats.ttl_samples) > 500:
            self.stats.ttl_samples = self.stats.ttl_samples[-500:]
        return eff

    def _get(self, key: str):
        try:
            raw = self.client.get(key)
        except Exception as exc:  # noqa: BLE001 —— Redis 不可用时当 miss（旁路缓存不能拖垮主流程）
            self._degrade(exc)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def _set(self, key: str, value, ttl: int) -> None:
        eff = self.effective_ttl(ttl)
        try:
            self.client.set(key, json.dumps(value, ensure_ascii=False), ex=eff)
        except Exception as exc:  # noqa: BLE001
            self._degrade(exc)
            return
        self.stats.writes += 1

    def _degrade(self, exc: BaseException) -> None:
        """记录一次 Redis 故障（只记第一次，避免日志刷屏）。"""
        if not self.degraded:
            self.degraded = True
            self.degraded_reason = f"{type(exc).__name__}: {exc}"
            logger.warning("Redis/缓存不可用，已降级为「不缓存」：%s", self.degraded_reason)

    def snapshot(self) -> dict:
        """看板/health 用的完整快照：命中统计 + 是否降级。"""
        return {**self.stats.snapshot(), "degraded": bool(self.degraded),
                "degraded_reason": self.degraded_reason or ""}

    def get(self, key: str):
        val = self._get(key)
        if val is None:
            self.stats.misses += 1
        else:
            self.stats.hits += 1
        return val

    def set(self, key: str, value, ttl: int) -> None:
        self._set(key, value, ttl)

    # ------------------------------------------------------------ 异步包装（不阻塞事件循环）
    # 为什么要这些：redis-py 的同步客户端在读操作上是**阻塞**调用，而缓存读写全都发生在
    # async 路径里（工具取数、MCP 调用）。本地 fakeredis 快到看不出问题，一旦换成真实 Redis
    # 或网络抖动，就会把事件循环（SSE 心跳、其它会话）一起卡住——这正是我们给 SQLAlchemy
    # 加 `to_thread` 的原因，缓存/队列不能例外。
    async def aget(self, key: str):
        return await asyncio.to_thread(self.get, key)

    async def aset(self, key: str, value, ttl: int) -> None:
        await asyncio.to_thread(self.set, key, value, ttl)

    async def aget_or_set_async(
        self,
        key: str,
        ttl: int,
        producer: Callable[[], Awaitable],
        should_cache: Callable[[object], bool] | None = None,
    ):
        """异步读取 + （未命中时）异步回源 + 异步写缓存，全程不占事件循环。

        - `should_cache`：错误结果不进缓存（例：MCP 工具返回 {"error": ...}）；
        - 单飞：同 key 的并发 miss 只回源一次，其余请求等同一份结果；
        - 回源者被取消时**不连坐**等待者：等待者收到 `_OwnerCancelledError` 后自己重走一遍
          （重走会重新读缓存，多半就轮到自己回源了）。
        """
        for _attempt in range(2):
            try:
                return await self._aget_or_set_once(key, ttl, producer, should_cache)
            except _OwnerCancelledError:
                continue
        # 连续两次都撞上「回源者被取消」：自己直接回源，绝不把取消传染给调用方
        return await producer()

    async def _aget_or_set_once(
        self,
        key: str,
        ttl: int,
        producer: Callable[[], Awaitable],
        should_cache: Callable[[object], bool] | None = None,
    ):
        val = await self.aget(key)
        if val is not None:
            return val
        fut, owner = self._single_flight(key)
        if not owner:
            return await asyncio.shield(fut)
        try:
            val = await producer()
            if val is not None and (should_cache is None or should_cache(val)):
                await self.aset(key, val, ttl)
            if not fut.done():
                fut.set_result(val)
            return val
        except asyncio.CancelledError:
            # 回源者被取消：给等待者一个「可重试」的信号，而不是 CancelledError（Q1）。
            # 自己的取消照常向上抛（这一轮确实该结束）。
            self._fail_waiters(fut, _OwnerCancelledError())
            raise
        except BaseException as exc:
            # 业务失败仍然共享给等待者（单飞的语义就是「同一份结果」），只标记已取回，
            # 避免无人等待时的 "Future exception was never retrieved" 告警。
            self._fail_waiters(fut, exc)
            raise
        finally:
            self._inflight.pop(key, None)

    @staticmethod
    def _fail_waiters(fut: asyncio.Future, exc: BaseException) -> None:
        if fut.done():
            return
        fut.set_exception(exc)
        fut.exception()       # 标记「已取回」，避免无等待者时的告警

    async def get_or_set_async(
        self,
        key: str,
        ttl: int,
        producer: Callable[[], Awaitable],
        should_cache: Callable[[object], bool] | None = None,
    ):
        """`aget_or_set_async` 的别名（历史调用点用这个名字）。"""
        return await self.aget_or_set_async(key, ttl, producer, should_cache=should_cache)

    def get_or_set(self, key: str, ttl: int, producer: Callable[[], object],
                   should_cache: Callable[[object], bool] | None = None):
        val = self._get(key)
        if val is not None:
            self.stats.hits += 1
            return val
        self.stats.misses += 1
        val = producer()
        if val is not None and (should_cache is None or should_cache(val)):
            self._set(key, val, ttl)
        return val


_cache: TTLCache | None = None


def get_ttl_cache() -> TTLCache:
    global _cache
    if _cache is None:
        _cache = TTLCache()
    return _cache


def reset_ttl_cache() -> None:
    global _cache
    _cache = None
