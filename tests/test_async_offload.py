"""异步路径不阻塞事件循环（C2）：同步 Redis 读写必须离开事件循环，且必须设读超时。

回归背景：`TTLCache._get/_set` 与 `StreamQueue.*` 用的是 redis-py **同步**客户端，
却在 async 工具/巡检里直接调用。本地 fakeredis 快到看不出问题，换真实 Redis 后
一次网络抖动就会把事件循环（SSE 心跳、其它会话）冻住；而客户端只设了
`socket_connect_timeout`、没设 `socket_timeout`，读操作默认**无限阻塞**。
"""

from __future__ import annotations

import asyncio

import pytest


def test_redis_client_gets_socket_timeout():
    """显式配置 REDIS_URL 时，连接参数必须带 socket_timeout（否则读会无限等）。"""
    from app.cache.redis_factory import make_client
    from app.config import Settings

    settings = Settings(
        redis_url="redis://127.0.0.1:6399/0",
        redis_socket_timeout=2.5,
        redis_socket_connect_timeout=1.5,
        redis_health_check_interval=15,
    )
    client = make_client(settings)
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == pytest.approx(2.5)
    assert kwargs["socket_connect_timeout"] == pytest.approx(1.5)
    assert kwargs["health_check_interval"] == 15
    client.close()


def test_ttl_cache_async_wrappers_offload_to_thread(monkeypatch):
    from app.cache.ttl_cache import TTLCache

    calls = {"to_thread": 0}
    real = asyncio.to_thread

    async def _counting(func, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["to_thread"] += 1
        return await real(func, *args, **kwargs)

    monkeypatch.setattr("app.cache.ttl_cache.asyncio.to_thread", _counting)
    cache = TTLCache(jitter_ratio=0.1)

    async def main():
        await cache.aset("k", {"v": 1}, 60)
        first = await cache.aget("k")
        second = await cache.aget("k")
        return first, second

    first, second = asyncio.run(main())
    assert first == second == {"v": 1}
    assert calls["to_thread"] == 3          # 1 次写 + 2 次读都在线程里
    assert cache.stats.writes == 1


def test_ttl_cache_async_get_or_set_matches_sync_semantics():
    from app.cache.ttl_cache import TTLCache

    async def main():
        cache = TTLCache(jitter_ratio=0.1)
        calls = {"n": 0}

        async def producer():
            calls["n"] += 1
            return {"v": 2}

        a = await cache.aget_or_set_async("k", 60, producer)
        b = await cache.aget_or_set_async("k", 60, producer)
        return cache, a, b, calls

    cache, a, b, calls = asyncio.run(main())
    assert a == b == {"v": 2}
    assert calls["n"] == 1                  # 第二次命中缓存
    assert cache.stats.hits == 1


def test_ttl_cache_should_cache_rejects_errors():
    from app.cache.ttl_cache import TTLCache

    async def main():
        cache = TTLCache(jitter_ratio=0.1)
        calls = {"n": 0}

        async def producer():
            calls["n"] += 1
            return {"error": "订单不存在"}

        await cache.aget_or_set_async("k", 60, producer, should_cache=lambda v: "error" not in v)
        await cache.aget_or_set_async("k", 60, producer, should_cache=lambda v: "error" not in v)
        return calls

    assert asyncio.run(main())["n"] == 2     # 错误结果不进缓存


def test_stream_queue_async_wrappers_match_sync():
    from app.cache.stream_queue import get_stream_queue

    async def main():
        queue = get_stream_queue()
        msg_id, dup = await queue.aenqueue({"sku_id": "SKU-A"}, dedupe_key="SKU-A:1")
        assert msg_id and not dup
        batch = await queue.aconsume(count=10)
        assert len(batch) == 1 and batch[0][1]["sku_id"] == "SKU-A"
        acked = await queue.aack(batch[0][0])
        stats = await queue.astats()
        return acked, stats

    acked, stats = asyncio.run(main())
    assert acked == 1
    assert stats["pending"] == 0
    assert stats["length"] >= 1


def test_stream_queue_async_ack_does_not_block_loop(monkeypatch):
    from app.cache.stream_queue import StreamQueue

    seen = {"n": 0}
    real = asyncio.to_thread

    async def _counting(func, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        seen["n"] += 1
        return await real(func, *args, **kwargs)

    monkeypatch.setattr("app.cache.stream_queue.asyncio.to_thread", _counting)

    async def main():
        queue = StreamQueue()
        msg_id, _ = await queue.aenqueue({"sku_id": "SKU-B"})
        await queue.aack(msg_id)
        await queue.astats()

    asyncio.run(main())
    assert seen["n"] == 3


# ---------------------------------------------------------------- 对话轮次的同步 DB 卸载
def test_begin_turn_offloads_sync_db_work(monkeypatch, seeded):
    """`_begin_turn` 的会话解析与用户消息落库都必须走线程池（评审 2.2-6）。

    回归背景：这两步是同步 SQLAlchemy 调用，却直接跑在事件循环里——单会话写库的时间会被
    摊给所有连接（SSE 心跳、其它会话的流）。`to_thread` 之后事件循环始终可调度。
    """
    from app.api import chat_routes
    from app.auth import AuthUser

    calls = {"n": 0}
    real = asyncio.to_thread

    async def _counting(func, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["n"] += 1
        return await real(func, *args, **kwargs)

    monkeypatch.setattr("app.api.chat_routes.asyncio.to_thread", _counting)
    user = AuthUser(username="admin", role="admin", department=None)

    async def main():
        # ① 新建会话路径：解析（线程）+ 落用户消息（线程）
        req = chat_routes.ChatRequest(message="卸载校验")
        turn = await chat_routes._begin_turn(user, req)
        await turn.guard.release()

        # ② 已有会话路径
        req2 = chat_routes.ChatRequest(message="第二轮", session_id=turn.session_id)
        turn2 = await chat_routes._begin_turn(user, req2)
        await turn2.guard.release()
        return turn.session_id

    asyncio.run(main())
    assert calls["n"] >= 4, f"对话轮次的同步 DB 调用没有离开事件循环（to_thread={calls['n']}）"


def test_persist_reply_runs_in_thread(monkeypatch, seeded):
    """答复落库同样要走线程池（它在流式路径里也会被调用）。"""
    from app.api import chat_routes
    from app.auth import AuthUser

    calls = {"n": 0}
    real = asyncio.to_thread

    async def _counting(func, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["n"] += 1
        return await real(func, *args, **kwargs)

    monkeypatch.setattr("app.api.chat_routes.asyncio.to_thread", _counting)
    user = AuthUser(username="admin", role="admin", department=None)

    async def main():
        turn = await chat_routes._begin_turn(user, chat_routes.ChatRequest(message="落库校验"))
        try:
            await asyncio.to_thread(
                chat_routes._persist_reply, user, turn.session_id,
                {"reply": "好的", "tool_trace": [], "run_detail": None},
            )
        finally:
            await turn.guard.release()

    asyncio.run(main())
    assert calls["n"] >= 3      # 解析 + 用户消息 + 答复

