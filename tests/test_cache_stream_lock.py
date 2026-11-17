"""Redis 缓存 TTL 抖动 / 旁路缓存 / 队列 / 分布式锁测试。"""

from __future__ import annotations

import asyncio

import pytest

from app.cache.lock import LockTimeoutError, is_locked, redis_lock
from app.cache.stream_queue import get_stream_queue
from app.cache.ttl_cache import TTLCache


class TestTTLCache:
    def test_jitter_within_bounds(self):
        cache = TTLCache(jitter_ratio=0.2)
        samples = [cache.effective_ttl(600) for _ in range(300)]
        assert all(600 <= s <= 720 for s in samples)
        assert len(set(samples)) > 10  # 必须打散，而不是固定值

    def test_get_or_set_hits(self):
        cache = TTLCache(jitter_ratio=0.2)
        calls = {"n": 0}

        def producer():
            calls["n"] += 1
            return {"v": 1}

        k = "cache:test:1"
        assert cache.get_or_set(k, 60, producer) == {"v": 1}
        assert cache.get_or_set(k, 60, producer) == {"v": 1}
        assert calls["n"] == 1
        assert cache.stats.hits == 1 and cache.stats.misses == 1

    def test_get_or_set_async(self):
        async def run():
            cache = TTLCache(jitter_ratio=0.2)
            calls = {"n": 0}

            async def producer():
                calls["n"] += 1
                return [1, 2, 3]

            a = await cache.get_or_set_async("k", 60, producer)
            b = await cache.get_or_set_async("k", 60, producer)
            assert a == b == [1, 2, 3]
            assert calls["n"] == 1

        asyncio.run(run())


class TestStreamQueue:
    def test_enqueue_dedupe_and_consume(self):
        queue = get_stream_queue()
        task = {"sku_id": "SKU-X", "mode": "daily"}
        msg_id, dup = queue.enqueue(task, dedupe_key="SKU-X:2026100110")
        assert msg_id and not dup
        msg_id2, dup2 = queue.enqueue(task, dedupe_key="SKU-X:2026100110")
        assert msg_id2 is None and dup2

        batch = queue.consume(count=10)
        assert len(batch) == 1
        mid, payload = batch[0]
        assert payload["sku_id"] == "SKU-X"
        assert queue.pending_count() == 1
        assert queue.ack(mid) == 1
        assert queue.pending_count() == 0


class TestLock:
    def test_lock_timeout(self):
        async def run():
            async with redis_lock("lock:test:a", ttl_ms=5000):
                assert is_locked("lock:test:a")
                with pytest.raises(LockTimeoutError):
                    async with redis_lock("lock:test:a", wait_timeout=0.1):
                        pass
            assert not is_locked("lock:test:a")

        asyncio.run(run())

    def test_lock_release_after(self):
        async def run():
            async with redis_lock("lock:test:b", ttl_ms=500):
                pass
            await asyncio.sleep(0.6)
            assert not is_locked("lock:test:b")

        asyncio.run(run())
