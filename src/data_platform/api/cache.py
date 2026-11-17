"""数据服务层缓存：Redis / fakeredis + TTL 随机抖动 + 单飞（设计上预留，演示可跑）。

单飞（single-flight）为什么必须有：中台端点是同步端点（跑在线程池），多个 Agent 同时问同一个
SKU 时，缓存未命中的那几个请求会**一起回源**（同一份 SQL 重复算 3~5 次），既浪费又放大尾延迟。
这里用 per-key 线程锁把并发 miss 收敛成一次回源。
"""

from __future__ import annotations

import json
import random
import threading

import fakeredis

from data_platform.config import CACHE_JITTER, CACHE_TTL_SECONDS

_client = None
_stats = {"hits": 0, "misses": 0, "writes": 0}
_flight_lock = threading.Lock()
_flight: dict[str, threading.Lock] = {}
_FLIGHT_LIMIT = 1000


def get_client():
    global _client
    if _client is None:
        _client = fakeredis.FakeRedis(decode_responses=True)
    return _client


def set_client(client) -> None:
    global _client
    _client = client


def reset_client() -> None:
    global _client
    if _client is not None:
        try:
            _client.flushall()
        except Exception:
            pass
    _client = None
    _stats.update({"hits": 0, "misses": 0, "writes": 0})
    with _flight_lock:
        _flight.clear()


def effective_ttl(ttl: int | None = None) -> int:
    ttl = ttl or CACHE_TTL_SECONDS
    return max(1, int(ttl * (1 + random.uniform(0, CACHE_JITTER))))


def _key_lock(key: str) -> threading.Lock:
    with _flight_lock:
        lock = _flight.get(key)
        if lock is None:
            if len(_flight) > _FLIGHT_LIMIT:
                _flight.clear()
            lock = threading.Lock()
            _flight[key] = lock
        return lock


def get_or_set(key: str, producer, ttl: int | None = None) -> tuple[dict, bool]:
    """cache-aside：返回 (value, hit)。producer 返回 dict。同 key 并发只回源一次。"""
    client = get_client()
    raw = client.get(key)
    if raw is not None:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = None
        if value is not None:
            _stats["hits"] += 1
            return value, True

    with _key_lock(key):
        # 抢到锁后再看一次：可能刚被另一个线程回源写好了
        raw = client.get(key)
        if raw is not None:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = None
            if value is not None:
                _stats["hits"] += 1
                return value, True
        _stats["misses"] += 1
        value = producer()
        client.set(key, json.dumps(value, ensure_ascii=False, default=str), ex=effective_ttl(ttl))
        _stats["writes"] += 1
        return value, False


def stats() -> dict:
    """中台侧缓存统计：键数 + 命中/未命中/写入与命中率（供 Agent 看板展示第二层）。"""
    client = get_client()
    try:
        keys = int(client.dbsize())
    except Exception:
        keys = 0
    total = _stats["hits"] + _stats["misses"]
    return {
        "keys": keys,
        "hits": _stats["hits"],
        "misses": _stats["misses"],
        "writes": _stats["writes"],
        "hit_rate": round(_stats["hits"] / total, 4) if total else 0.0,
    }
