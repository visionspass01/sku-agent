"""并发/限流/单飞/平台熔断（C1/C3/C5）。"""

from __future__ import annotations

import asyncio

import httpx
import pytest


# ---------------------------------------------------------------- C3 限流原子化
def test_check_and_hit_is_atomic_and_counts():
    from app.security.throttle import SlidingWindowCounter

    counter = SlidingWindowCounter()
    first = counter.check_and_hit("k", limit=1, window=60.0)
    second = counter.check_and_hit("k", limit=1, window=60.0)
    assert first["allowed"] is True and first["remaining"] == 0
    assert second["allowed"] is False and second["count"] == 1
    assert second["retry_after_s"] > 0


def test_counter_does_not_leak_empty_keys():
    """登录限流的 key 含账号名（可被随便填），空槽必须清掉，否则字典单调增长。"""
    from app.security.throttle import SlidingWindowCounter

    counter = SlidingWindowCounter(max_keys=5)
    for i in range(200):
        counter.check_and_hit(f"user-{i}", limit=3, window=0.01)
    assert counter.size() <= 5            # 超限时淘汰最旧
    # 窗口过了以后不再保留空槽
    import time as _time

    _time.sleep(0.02)
    for i in range(200, 203):
        counter.check_and_hit(f"user-{i}", limit=3, window=0.01)
    assert counter.size() <= 5


# ---------------------------------------------------------------- C1 闸门覆盖整轮
def test_chat_gate_covers_grounding(seeded, monkeypatch, _settings):
    """溯源重写也是一次模型调用：它必须在 chat 闸门里，否则总并发会超出上限。"""
    from app.agents import chat_agent
    from app.auth import AuthUser
    from app.memory.runtime import tool_context

    monkeypatch.setattr(_settings, "grounding_enabled", True)
    monkeypatch.setattr(_settings, "chat_max_concurrency", 4)
    chat_agent.reset_chat_semaphores()
    observed: list[int] = []

    class _Agent:
        async def ainvoke(self, payload):
            return {"messages": payload["messages"] + [
                __import__("langchain_core.messages", fromlist=["AIMessage"]).AIMessage(
                    content="可用库存 900 件")]}

    async def _fake_ground(reply, *, steps=None, tool_trace=None):  # noqa: ANN001
        observed.append(chat_agent.chat_limits()["inflight"])
        return {"reply": reply, "report": {"status": "passed", "issues": []}}

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda *a, **k: _Agent())
    monkeypatch.setattr("app.agents.grounding.ground_reply", _fake_ground)

    user = AuthUser(username="admin", role="admin", department=None)
    with tool_context(session_id="s-gate", username="admin", department=None):
        asyncio.run(chat_agent.run_chat(user, [], "ST-92-C1-M 库存"))

    assert observed == [1], "grounding 阶段闸门已经释放 → 这次模型调用不受并发上限约束"
    chat_agent.reset_chat_semaphores()


def test_health_limits_reports_both_gates(app_client):
    limits = app_client.get("/api/health").json()["limits"]
    assert {"max_concurrency", "inflight", "llm_max_concurrency", "llm_inflight",
            "total_inflight"} <= set(limits)
    assert limits["total_inflight"] == limits["inflight"] + limits["llm_inflight"]
    assert "两套闸门" in limits["note"]


# ---------------------------------------------------------------- B2 单轮预算
def test_retry_budget_stops_retrying(seeded, monkeypatch, _settings):
    """重试预算用尽：不再重试、也不再降级，直接明确失败（别把并发槽占十几分钟）。"""
    from app.agents import chat_agent
    from app.auth import AuthUser

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(_settings, "chat_max_retries", 3)
    monkeypatch.setattr(_settings, "chat_retry_budget_seconds", 0.0)
    monkeypatch.setattr(_settings, "chat_fallback_model", "deepseek-chat")
    calls = {"n": 0}

    class _Flaky:
        async def ainvoke(self, payload):  # noqa: ANN001
            calls["n"] += 1
            raise ConnectionError("connection reset")

    async def _no_sleep(_s):  # noqa: ANN001
        return None

    monkeypatch.setattr(chat_agent.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda *a, **k: _Flaky())

    user = AuthUser(username="admin", role="admin", department=None)
    with pytest.raises(chat_agent.TurnBudgetExceeded):
        asyncio.run(chat_agent.run_chat(user, [], "查库存"))
    assert calls["n"] == 1, "预算为 0 时不该再重试"


def test_retry_budget_also_blocks_fallback(seeded, monkeypatch, _settings):
    """预算也约束「降级」：慢主模型跑完再换备用模型，等于把预算翻倍。"""
    from app.agents import chat_agent
    from app.auth import AuthUser

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(_settings, "chat_max_retries", 0)
    monkeypatch.setattr(_settings, "chat_retry_budget_seconds", 0.0)
    monkeypatch.setattr(_settings, "chat_fallback_model", "deepseek-chat")
    calls = {"n": 0, "built": 0}

    class _Flaky:
        async def ainvoke(self, payload):  # noqa: ANN001
            calls["n"] += 1
            raise ConnectionError("connection reset")

    def _build(*a, **k):  # noqa: ANN002, ANN003
        calls["built"] += 1
        return _Flaky()

    monkeypatch.setattr(chat_agent, "build_chat_agent", _build)
    user = AuthUser(username="admin", role="admin", department=None)
    with pytest.raises(chat_agent.TurnBudgetExceeded):
        asyncio.run(chat_agent.run_chat(user, [], "查库存"))
    assert calls["n"] == 1
    assert calls["built"] == 1, "预算为 0 时不该再装配备用模型"


def test_turn_timeout_aborts_slow_turn(seeded, monkeypatch, _settings):
    """整轮硬超时：慢请求必须被中止，而不是一直占着连接与并发槽。"""
    from app.agents import chat_agent
    from app.auth import AuthUser

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(_settings, "chat_turn_timeout_seconds", 0.05)

    class _Slow:
        async def ainvoke(self, payload):  # noqa: ANN001
            await asyncio.sleep(1.0)
            return {"messages": payload["messages"]}

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda *a, **k: _Slow())
    user = AuthUser(username="admin", role="admin", department=None)
    with pytest.raises(chat_agent.TurnBudgetExceeded):
        asyncio.run(chat_agent.run_chat(user, [], "查库存"))


def test_domain_timeout_error_is_not_converted(seeded, monkeypatch, _settings):
    """业务代码自己抛的 TimeoutError（流中断）不能被误当成「整轮超时」。"""
    from app.agents import chat_agent
    from app.auth import AuthUser

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(_settings, "chat_turn_timeout_seconds", 30.0)
    monkeypatch.setattr(_settings, "chat_max_retries", 0)

    class _Dropped:
        async def ainvoke(self, payload):  # noqa: ANN001
            raise TimeoutError("stream dropped")

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda *a, **k: _Dropped())
    user = AuthUser(username="admin", role="admin", department=None)
    with pytest.raises(TimeoutError) as info:
        asyncio.run(chat_agent.run_chat(user, [], "查库存"))
    assert not isinstance(info.value, chat_agent.TurnBudgetExceeded)


# ---------------------------------------------------------------- C5 单飞
def test_single_flight_dedupes_concurrent_misses():
    from app.cache.ttl_cache import TTLCache

    async def main():
        cache = TTLCache(jitter_ratio=0.1)
        calls = {"n": 0}

        async def producer():
            calls["n"] += 1
            await asyncio.sleep(0.05)     # 模拟慢回源
            return {"v": 1}

        results = await asyncio.gather(
            *[cache.aget_or_set_async("hot-key", 60, producer) for _ in range(8)]
        )
        return cache, results, calls

    cache, results, calls = asyncio.run(main())
    assert all(r == {"v": 1} for r in results)
    assert calls["n"] == 1, "并发 miss 应该只回源一次（否则同时打 8 次中台）"


def test_single_flight_propagates_failure_to_waiters():
    from app.cache.ttl_cache import TTLCache

    async def main():
        cache = TTLCache(jitter_ratio=0.1)
        calls = {"n": 0}

        async def producer():
            calls["n"] += 1
            await asyncio.sleep(0.02)
            raise RuntimeError("upstream down")

        results = await asyncio.gather(
            *[cache.aget_or_set_async("bad-key", 60, producer) for _ in range(4)],
            return_exceptions=True,
        )
        return calls, results

    calls, results = asyncio.run(main())
    assert calls["n"] == 1
    assert all(isinstance(r, RuntimeError) for r in results)


def test_single_flight_owner_cancel_does_not_cancel_waiters():
    """回源者被取消（客户端断开 / 轮次超时）时，等待者不能连坐取消（复审 Q1）。

    `asyncio.shield` 只挡「等待者自己被取消」，挡不住「共享 future 带着 CancelledError
    完成」——不处理的话，A 关掉页面会把同一 SKU 上 B 的 SSE 流一起掐断（无 fatal 帧）。
    """
    from app.cache.ttl_cache import TTLCache

    async def main():
        cache = TTLCache(jitter_ratio=0.1)
        calls = {"n": 0}

        async def producer():
            calls["n"] += 1
            await asyncio.sleep(0.3)      # 慢回源，留出取消窗口
            return {"v": 1}

        owner = asyncio.create_task(cache.aget_or_set_async("slow-key", 60, producer))
        await asyncio.sleep(0.05)         # 让 owner 先成为回源者
        waiter = asyncio.create_task(cache.aget_or_set_async("slow-key", 60, producer))
        await asyncio.sleep(0.05)
        owner.cancel()                    # 模拟回源者的请求被取消
        cancelled_owner = False
        try:
            await owner
        except asyncio.CancelledError:
            cancelled_owner = True
        try:
            result = await waiter
        except asyncio.CancelledError:
            result = "WAITER_CANCELLED"
        return cancelled_owner, waiter.cancelled(), result, calls["n"], dict(cache._inflight)

    cancelled_owner, waiter_cancelled, result, producer_calls, inflight = asyncio.run(main())
    assert cancelled_owner is True, "回源者自己的取消要照常向上抛"
    assert waiter_cancelled is False, "等待者不能被回源者的取消连坐"
    assert result == {"v": 1}, f"等待者应该自己回源拿到结果，实际={result!r}"
    assert producer_calls == 2, "回源者被取消后，等待者应自己回源一次（不是 0 次也不是 3 次）"
    assert inflight == {}, "取消路径不能把单飞登记表漏成僵尸条目"


def test_single_flight_does_not_leak_across_loops():
    """两次 asyncio.run 之间不能复用上一轮循环的 future（否则会 ValueError）。"""
    from app.cache.ttl_cache import TTLCache

    cache = TTLCache(jitter_ratio=0.1)

    async def one():
        async def producer():
            return {"v": 9}

        return await cache.aget_or_set_async("loop-key", 60, producer)

    assert asyncio.run(one()) == {"v": 9}
    cache._inflight.clear()
    assert asyncio.run(one()) == {"v": 9}


# ---------------------------------------------------------------- C5 平台负缓存 + 熔断
@pytest.fixture()
def flaky_platform(monkeypatch, _settings):
    from app.tools import platform_tools

    state = {"mode": "down", "calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["mode"] == "down":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"sku_id": "ST-92-C1-M", "available": 1})

    client = platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler),
    )
    platform_tools.set_platform_client(client)
    yield client, state
    platform_tools.set_platform_client(None)


def test_link_failure_is_negative_cached(seeded, flaky_platform):
    """中台连不上时不要把每一轮都吃满 15s 超时：链路错误短 TTL 负缓存。"""
    from app.cache.ttl_cache import get_ttl_cache

    client, state = flaky_platform
    first = asyncio.run(client.get("/api/v1/skus/ST-92-C1-M/inventory"))
    second = asyncio.run(client.get("/api/v1/skus/ST-92-C1-M/inventory"))

    assert "error" in first and "error" in second
    assert first["negative_cached"] is False
    assert second["negative_cached"] is True
    assert state["calls"] == 1
    assert first["agent_cached"] is False and second["agent_cached"] is False
    assert get_ttl_cache().stats.writes == 1


def test_platform_breaker_opens_after_repeated_link_failures(seeded, flaky_platform):
    from app.tools.platform_tools import get_platform_client

    client, state = flaky_platform
    paths = [f"/api/v1/skus/SKU-{i}/inventory" for i in range(5)]
    responses = [asyncio.run(client.get(p)) for p in paths]

    assert client.breaker_state()["state"] == "open"
    assert any(r.get("breaker_open") for r in responses[1:]), \
        "熔断打开后应快速失败，而不是每次先等满超时"
    # 熔断打开后不再真的发请求
    calls_at_open = state["calls"]
    asyncio.run(client.get("/api/v1/skus/SKU-99/inventory"))
    assert state["calls"] == calls_at_open
    assert get_platform_client() is client


def test_breaker_recovers_after_window(seeded, flaky_platform):
    client, state = flaky_platform
    for i in range(4):
        asyncio.run(client.get(f"/api/v1/skus/SKU-{i}/inventory"))
    assert client.breaker_state()["state"] == "open"

    client._breaker.opened_at -= 61.0      # 模拟窗口结束
    state["mode"] = "up"
    ok = asyncio.run(client.get("/api/v1/skus/ST-92-C1-M/inventory"))
    assert "error" not in ok               # 半开探测成功 → 闭合
    assert client.breaker_state()["state"] == "closed"
