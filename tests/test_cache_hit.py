"""Agent 侧「工具取数」旁路缓存：命中/不缓存 POST/错误不缓存 + 缓存明细接口。"""

from __future__ import annotations

import asyncio

import httpx
import pytest


def _platform(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.fixture()
def platform_client():
    from app.tools import platform_tools

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/inventory"):
            return httpx.Response(200, json={"sku_id": "ST-92-C1-M", "available": 693,
                                             "as_of": "2026-11-04", "cached": True})
        if path.endswith("/sell-through"):
            return httpx.Response(200, json={"sku_id": "ST-92-C1-M", "sell_through_days": 32.78})
        if path.endswith("/erp/replenish-orders"):
            return httpx.Response(200, json={"approval_id": "appr-1", "status": "auto_approved"})
        if path.endswith("/broken"):
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(404, json={"detail": "not found"})

    client = platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=_platform(handler),
    )
    platform_tools.set_platform_client(client)
    yield client
    platform_tools.set_platform_client(None)


def _tools(role: str = "admin"):
    from app.auth import AuthUser
    from app.tools.platform_tools import build_platform_tools

    user = AuthUser(username=role, role=role, department=None)
    return {t.name: t for t in build_platform_tools(user)}


def test_get_is_cached_second_call_hits(seeded, platform_client):
    from app.cache.ttl_cache import get_ttl_cache

    tools = _tools()
    first = asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-92-C1-M"}))
    second = asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-92-C1-M"}))
    assert first["agent_cached"] is False
    assert second["agent_cached"] is True
    assert first["available"] == second["available"] == 693
    # 每次调用仍有独立 result_id（取数台账不合并）
    assert first["result_id"] != second["result_id"]
    stats = get_ttl_cache().stats.snapshot()
    assert stats["hits"] >= 1 and stats["misses"] >= 1
    assert stats["hit_rate"] > 0


def test_different_params_not_shared(seeded, platform_client):
    tools = _tools()
    asyncio.run(tools["get_orders"].ainvoke({"sku_id": "ST-92-C1-M", "days": 7}))
    asyncio.run(tools["get_orders"].ainvoke({"sku_id": "ST-92-C1-M", "days": 14}))
    from app.cache.ttl_cache import get_ttl_cache

    stats = get_ttl_cache().stats.snapshot()
    assert stats["hits"] == 0, stats


def test_post_is_never_cached(seeded, platform_client):
    from app.cache.ttl_cache import get_ttl_cache

    tools = _tools()
    for _ in range(2):
        res = asyncio.run(tools["create_replenishment_order"].ainvoke(
            {"sku_id": "ST-92-C1-M", "qty": 50, "reason": "测试"}))
        assert res.get("agent_cached") is None
    stats = get_ttl_cache().stats.snapshot()
    assert stats["hits"] == 0 and stats["misses"] == 0


def test_error_response_is_not_cached(seeded, platform_client):
    from app.cache.ttl_cache import get_ttl_cache

    client = platform_client
    first = asyncio.run(client.get("/api/v1/whatever/broken"))
    second = asyncio.run(client.get("/api/v1/whatever/broken"))
    assert "error" in first and "error" in second
    assert first["agent_cached"] is False and second["agent_cached"] is False
    stats = get_ttl_cache().stats.snapshot()
    assert stats["hits"] == 0
    assert stats["misses"] == 2
    assert stats["writes"] == 0


def test_tool_step_carries_agent_cached(seeded, platform_client):
    """运行详情里的工具步骤带上 agent_cached，供时间线展示「缓存」列。"""
    from app.memory.runtime import current_turn_log, turn_log_context

    tools = _tools()
    with turn_log_context("t_cache_test", "s-cache"):
        asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-92-C1-M"}))
        asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-92-C1-M"}))
        steps = list(current_turn_log()["steps"])
    assert [s["agent_cached"] for s in steps] == [False, True]


def test_cache_breakdown_endpoint(app_client, seeded, platform_client):
    from app.cache.ttl_cache import get_ttl_cache

    tools = _tools()
    asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-92-C1-M"}))
    asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-92-C1-M"}))
    data = app_client.get("/api/cache/breakdown").json()
    assert data["agent"]["hits"] >= 1
    assert data["ttl"]["platform"] >= 1 and "mcp" in data["ttl"] and "rag" in data["ttl"]
    assert "命中率" in data["note"]
    # 中台不可用时 platform=None 且给出原因（不抛错）
    assert data["platform"] is None or isinstance(data["platform"], dict)
    assert get_ttl_cache().stats.snapshot()["hits"] >= 1


def test_metrics_cache_is_agent_layer(app_client, seeded, platform_client):
    tools = _tools()
    asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-92-C1-M"}))
    asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-92-C1-M"}))
    data = app_client.get("/api/metrics").json()
    cache = data["cache"]
    assert cache["hits"] >= 1 and cache["misses"] >= 1
    assert cache["hit_rate"] > 0
