"""MCP 连接池 + 熔断测试（不需要真实子进程：用合成会话注入延迟/故障）。"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest


class _State:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self.delay = 0.05
        self.fail = False

    def snapshot(self) -> dict:
        return {"active": self.active, "max_active": self.max_active, "calls": self.calls}


def _make_factory(state: _State):
    @asynccontextmanager
    async def factory():
        session = _Session(state)
        yield session

    return factory


class _Session:
    def __init__(self, state: _State) -> None:
        self._state = state

    async def call_tool(self, tool, arguments=None):  # noqa: ANN001
        state = self._state
        state.calls += 1
        state.active += 1
        state.max_active = max(state.max_active, state.active)
        try:
            await asyncio.sleep(state.delay)
            if state.fail:
                raise RuntimeError("boom")
            return SimpleNamespace(isError=False, content=[
                SimpleNamespace(text=json.dumps({"tool": tool, "args": arguments or {}})),
            ])
        finally:
            state.active -= 1


def test_pool_runs_same_server_calls_in_parallel():
    from app.mcp_client.client import _PooledClient

    state = _State()

    async def main():
        client = _PooledClient("erp", _make_factory(state), pool_size=2)
        await client.start()
        try:
            results = await asyncio.gather(
                *[client.call("get_sku_inventory", {"i": i}) for i in range(4)]
            )
        finally:
            await client.stop()
        return results

    results = asyncio.run(main())
    assert [r["args"]["i"] for r in results] == [0, 1, 2, 3]
    # 两条会话各串行、彼此并行：最大并发应达到 2（单会话时必然是 1）
    assert state.snapshot()["max_active"] == 2


def test_pool_size_one_serializes():
    from app.mcp_client.client import _PooledClient

    state = _State()

    async def main():
        client = _PooledClient("erp", _make_factory(state), pool_size=1)
        await client.start()
        try:
            await asyncio.gather(*[client.call("t", {"i": i}) for i in range(3)])
        finally:
            await client.stop()

    asyncio.run(main())
    assert state.snapshot()["max_active"] == 1


def test_breaker_opens_and_recovers():
    from app.mcp_client.client import _Breaker

    breaker = _Breaker(threshold=3, reset_seconds=60.0)
    assert breaker.allow() is True
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state()["state"] == "closed"
    breaker.record_failure()
    assert breaker.state()["state"] == "open"
    assert breaker.allow() is False

    # 用「回拨打开时间」模拟熔断窗口结束（半开：放一次请求）
    breaker.opened_at -= 61.0
    assert breaker.allow() is True
    assert breaker.state()["state"] == "half_open"
    breaker.record_success()
    assert breaker.state() == {"state": "closed", "failures": 0, "retry_in_s": 0.0}


def test_hub_fast_fails_after_repeated_failures(monkeypatch, _settings):
    from app.mcp_client import client as mcp_client
    from app.observability import tool_stats

    class _BrokenClient:
        pending = 0

        async def call(self, tool, arguments):
            raise mcp_client.McpCallError("upstream down")

    monkeypatch.setattr(mcp_client.asyncio, "sleep", _no_sleep(monkeypatch))
    monkeypatch.setattr(_settings, "mcp_max_retries", 0)
    monkeypatch.setattr(_settings, "mcp_breaker_threshold", 2)
    monkeypatch.setattr(_settings, "mcp_breaker_reset_seconds", 60.0)

    async def main():
        hub = mcp_client.MCPHub(_settings)
        hub._started = True
        hub._clients = {"erp": _BrokenClient()}
        hub._breakers = {"erp": mcp_client._Breaker(threshold=2, reset_seconds=60.0)}
        errors = []
        for _ in range(3):
            try:
                await hub._call_raw("erp", "get_sku_inventory", {})
            except mcp_client.McpCallError as exc:
                errors.append(str(exc))
        return hub, errors

    hub, errors = asyncio.run(main())
    assert errors[0].startswith("调用 MCP 工具")
    assert errors[1].startswith("调用 MCP 工具")
    assert "熔断中" in errors[2]          # 第三次直接快速失败，不再等超时
    assert hub.health()["servers"] == {} or hub.health()["started"] is True
    breaker_state = asyncio.run(_breaker_state(hub))
    assert breaker_state["state"] == "open"
    row = tool_stats.snapshot()["tools"]["erp.get_sku_inventory"]
    assert row["outcomes"].get("breaker_open") == 1


async def _breaker_state(hub) -> dict:
    return hub._breakers["erp"].state()


def _no_sleep(monkeypatch):
    async def _sleep(_seconds):
        return None

    return _sleep


def test_business_error_payload_is_not_cached(monkeypatch, _settings):
    """工具返回 {"error": ...} 属于业务错误：不能缓存 10 分钟，否则后续重试也拿到同一个错误。"""
    from app.mcp_client import client as mcp_client

    class _BizErrorClient:
        pending = 0

        def __init__(self) -> None:
            self.calls = 0

        async def call(self, tool, arguments):
            self.calls += 1
            return {"error": "订单不存在：ORD-X"}

    async def main():
        hub = mcp_client.MCPHub(_settings)
        hub._started = True
        fake = _BizErrorClient()
        hub._clients = {"order": fake}
        hub._breakers = {"order": mcp_client._Breaker(threshold=99, reset_seconds=60.0)}
        first = await hub.call("order", "get_order_status", {"order_id": "ORD-X"})
        second = await hub.call("order", "get_order_status", {"order_id": "ORD-X"})
        return fake.calls, first, second

    calls, first, second = asyncio.run(main())
    assert calls == 2, "业务错误被缓存了：第二次没有再打下游"
    assert "不存在" in first["error"] and "不存在" in second["error"]


def test_half_open_releases_only_one_probe(monkeypatch, _settings):
    """熔断窗口结束只放**一个**探测请求，其余并发请求继续快速失败（避免恢复瞬间雪崩）。"""
    from app.mcp_client import client as mcp_client

    monkeypatch.setattr(_settings, "mcp_max_retries", 0)

    class _Broken:
        pending = 0

        def __init__(self) -> None:
            self.calls = 0

        async def call(self, tool, arguments):
            self.calls += 1
            await asyncio.sleep(0.01)
            raise mcp_client.McpCallError("upstream down")

    async def main():
        hub = mcp_client.MCPHub(_settings)
        hub._started = True
        broken = _Broken()
        breaker = mcp_client._Breaker(threshold=1, reset_seconds=60.0)
        hub._clients = {"erp": broken}
        hub._breakers = {"erp": breaker}
        breaker.record_failure()          # 先打开
        breaker.opened_at -= 61.0         # 模拟窗口结束 → 半开
        results = await asyncio.gather(
            *[hub._call_raw("erp", "get_sku_inventory", {}) for _ in range(5)],
            return_exceptions=True,
        )
        return broken.calls, results, breaker

    calls, results, breaker = asyncio.run(main())
    assert calls == 1, "半开期间只应有一个探测请求打到下游"
    denied = [r for r in results
              if isinstance(r, Exception) and "熔断中" in str(r)]
    assert len(denied) == 4
    assert breaker.state()["state"] in {"open", "half_open"}


def test_outcome_classification_aggregates():
    from app.observability import tool_stats

    tool_stats.record("chat", "get_inventory", True, 12.0)
    tool_stats.record("chat", "get_inventory", False, 15.0, error_kind="business")
    tool_stats.record("chat", "get_inventory", False, 3000.0, timeout=True)
    tool_stats.record("chat", "get_inventory", False, 900.0, outcome="breaker_open")
    row = tool_stats.snapshot()["tools"]["chat.get_inventory"]
    assert row["outcomes"] == {
        "ok": 1, "business_error": 1, "timeout": 1, "breaker_open": 1,
    }
    assert row["success_rate"] == pytest.approx(0.25)


def test_chat_tool_failure_tagged_business(seeded, monkeypatch):
    """权限/参数类失败记 business_error：这类重试无用，看板上要和链路故障区分。"""
    from app.auth import AuthUser
    from app.observability import tool_stats
    from app.tools import platform_tools
    from app.tools.platform_tools import build_platform_tools

    tool_stats.reset()
    user = AuthUser(username="worker1", role="user", department="男装")
    tools = {t.name: t for t in build_platform_tools(user)}
    result = asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-90-C1-M"}))  # 女装 SKU
    assert "无权限" in result["error"]
    row = tool_stats.snapshot()["tools"]["chat.get_inventory"]
    assert row["outcomes"].get("business_error") == 1
    assert platform_tools.get_platform_client() is not None
