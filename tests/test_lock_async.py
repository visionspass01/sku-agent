"""异步锁 / 同步短锁的行为与「不阻塞事件循环」测试（评审 v3-①）。

背景：会话锁与巡检顺序锁原来用的是**同步** redis 客户端。`socket_timeout` 默认 3s，
Redis 抖一下就把事件循环冻住 3 秒——SSE 心跳、其它会话的 token 流全部停摆。
所以这两把锁必须走 `redis.asyncio`。这里锁定三件事：

1. 语义不变：抢不到锁 → `LockTimeoutError`（409 业务冲突）；Redis 连不上 → `LockBackendError`（fail-closed）；
2. 真异步：等锁期间事件循环还能跑别的协程（用 `asyncio.sleep` 而不是 `time.sleep`）；
3. 释放靠持有者 token 比对，TTL 兜底，不覆盖业务异常。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.cache.lock import (
    LockBackendError,
    LockTimeoutError,
    async_short_lock,
    is_locked,
    redis_lock,
    short_lock,
)


class _BrokenClient:
    """所有命令都抛错的假客户端：模拟 Redis 不可用。"""

    async def set(self, *a, **kw):  # noqa: D401, ANN002, ANN003
        raise ConnectionError("redis down")

    async def get(self, *a, **kw):  # noqa: ANN002, ANN003
        raise ConnectionError("redis down")

    async def delete(self, *a, **kw):  # noqa: ANN002, ANN003
        raise ConnectionError("redis down")


class _BrokenSyncClient:
    def exists(self, *a, **kw):  # noqa: ANN002, ANN003
        raise ConnectionError("redis down")


class TestAsyncLock:
    def test_acquire_and_release(self):
        async def run():
            async with redis_lock("lock:async:a", ttl_ms=5000):
                assert is_locked("lock:async:a")
            assert not is_locked("lock:async:a")

        asyncio.run(run())

    def test_timeout_raises_business_error(self):
        async def run():
            async with redis_lock("lock:async:b", ttl_ms=5000):
                with pytest.raises(LockTimeoutError):
                    async with redis_lock("lock:async:b", wait_timeout=0.05):
                        pass
            # 外层释放后可以立即重新拿到
            async with redis_lock("lock:async:b", ttl_ms=5000):
                pass

        asyncio.run(run())

    def test_waiter_does_not_block_event_loop(self):
        """持锁 150ms 期间，事件循环必须还能跑别的协程（否则就是同步锁）。"""

        async def run():
            ticks = 0

            async def ticker():
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.01)
                    ticks += 1

            async with redis_lock("lock:async:c", ttl_ms=5000):
                task = asyncio.create_task(ticker())
                started = time.monotonic()
                with pytest.raises(LockTimeoutError):
                    # 等锁 0.15s：真异步时 ticker 至少能跑十几次
                    async with redis_lock("lock:async:c", wait_timeout=0.15):
                        pass
                elapsed = time.monotonic() - started
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert elapsed >= 0.1
            assert ticks >= 5, f"事件循环被阻塞：等待期间只跑了 {ticks} 次"

        asyncio.run(run())

    def test_release_does_not_kill_someone_elses_lock(self):
        """token 比对：A 的 finally 不能删掉 B 后来抢到的同名锁（TTL 过期场景）。"""

        async def run():
            key = "lock:async:token"
            cm = redis_lock(key, ttl_ms=50)   # 故意 TTL 极短
            await cm.__aenter__()
            await asyncio.sleep(0.12)          # 锁已过期，B 成功拿到
            async with redis_lock(key, ttl_ms=5000):
                assert is_locked(key)
                await cm.__aexit__(None, None, None)   # A 释放：不能删 B 的锁
                assert is_locked(key), "释放时覆盖了别人的锁"

        asyncio.run(run())

    def test_backend_error_is_not_timeout(self):
        """Redis 挂了要报 LockBackendError，否则会被当成"你在并发对话"返回 409。"""
        from app.cache.redis_factory import reset_redis, set_async_redis

        async def run():
            set_async_redis(_BrokenClient())
            with pytest.raises(LockBackendError):
                async with redis_lock("lock:async:down", ttl_ms=1000):
                    pass

        try:
            asyncio.run(run())
        finally:
            reset_redis()

    def test_business_exception_not_masked(self):
        """锁内的业务异常必须原样抛出，不能被释放逻辑覆盖。"""

        async def run():
            with pytest.raises(ValueError):
                async with redis_lock("lock:async:err", ttl_ms=5000):
                    raise ValueError("业务失败")
            assert not is_locked("lock:async:err")

        asyncio.run(run())

    def test_aclose_async_redis(self):
        """shutdown 时关掉异步客户端，且不抛错（fakeredis 也支持 aclose）。"""
        from app.cache.redis_factory import aclose_async_redis, get_async_redis, reset_redis

        async def run():
            client = get_async_redis()
            assert client is not None
            await aclose_async_redis()

        try:
            asyncio.run(run())
        finally:
            reset_redis()


class TestSyncShortLock:
    def test_sync_short_lock_still_works_in_thread(self):
        """同步短锁只在同步上下文/工作线程用：这里就在线程里验证它没被异步化改坏。"""

        def run():
            with short_lock("short:sync:a", ttl_ms=2000) as got:
                assert got
            assert not is_locked("lock:short:sync:a")

        import threading

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive()

    def test_sync_probe_sees_backend_failure(self):
        from app.cache.redis_factory import reset_redis, set_redis

        try:
            set_redis(_BrokenSyncClient())
            with pytest.raises(LockBackendError):
                is_locked("lock:anything")
        finally:
            reset_redis()


class TestAsyncShortLock:
    def test_async_short_lock_waits_without_blocking(self):
        async def run():
            async with async_short_lock("short:async:a", ttl_ms=2000, wait_timeout=0.5) as got:
                assert got
            async with async_short_lock("short:async:a", ttl_ms=2000, wait_timeout=0.5) as got:
                assert got

        asyncio.run(run())

    def test_async_short_lock_contention_returns_false(self):
        """同一个 key 并发：第二个拿不到就返回 False（调用方不要覆盖先写的那份）。"""

        async def run():
            async with async_short_lock("short:async:b", ttl_ms=2000, wait_timeout=0.05) as first:
                assert first
                async with async_short_lock("short:async:b", ttl_ms=2000,
                                            wait_timeout=0.05) as second:
                    assert second is False

        asyncio.run(run())


@pytest.fixture()
def unreachable_redis(_settings, monkeypatch):
    """配了 REDIS_URL 但连不上：锁要退化为进程内锁，而不是抛错/卡住。"""
    monkeypatch.setattr(_settings, "redis_url", "redis://127.0.0.1:6399/0")
    monkeypatch.setattr(_settings, "redis_socket_timeout", 0.2)
    monkeypatch.setattr(_settings, "redis_socket_connect_timeout", 0.2)
    from app.cache.redis_factory import reset_redis

    reset_redis()
    yield _settings
    monkeypatch.setattr(_settings, "redis_url", "")
    reset_redis()


class TestDegradedBackend:
    def test_async_short_lock_degrades_to_process_local(self, unreachable_redis):
        async def run():
            async with async_short_lock("short:down:a", ttl_ms=1000, wait_timeout=0.2) as got:
                assert got, "Redis 不可用时短锁应退化为进程内锁，而不是判成「抢不到」"

        asyncio.run(run())

    def test_sync_short_lock_degrades_to_process_local(self, unreachable_redis):
        import threading

        result = {}

        def run():
            with short_lock("short:down:b", ttl_ms=1000, wait_timeout=0.2) as got:
                result["got"] = got

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=15)
        assert not thread.is_alive()
        assert result.get("got") is True
