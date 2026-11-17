"""工具调用可靠性（成功率/重试/超时/P95）与 token 成本估算。

为什么单独测：这两块是"Demo 与工程的分水岭"——没有成功率就说不清"工具调用可靠"，
没有成本口径就说不清"上下文/成本控制"。
"""

from __future__ import annotations

import asyncio

import httpx


def test_tool_stats_counts_success_rate_retries_timeouts():
    from app.observability import tool_stats

    tool_stats.reset()
    tool_stats.record("erp", "get_sku_inventory", True, 12.0)
    tool_stats.record("erp", "get_sku_inventory", True, 20.0)
    tool_stats.record("erp", "get_sku_inventory", False, 5000.0, retries=2, timeout=True)
    snap = tool_stats.snapshot()
    row = snap["tools"]["erp.get_sku_inventory"]
    assert row["calls"] == 3
    assert row["ok"] == 2
    assert row["errors"] == 1
    assert row["success_rate"] == round(2 / 3, 4)
    assert row["retries"] == 2 and row["timeouts"] == 1
    assert row["p50"] >= 12.0 and row["p95"] >= 20.0
    assert snap["overall"]["calls"] == 3
    assert "成功率" in snap["note"]
    tool_stats.reset()
    assert tool_stats.snapshot()["overall"]["calls"] == 0


def test_mcp_hub_records_retry_then_success(monkeypatch):
    """MCP 调用先失败一次再成功：成功率按「最终成功」记，重试次数可见。"""
    from app.cache.ttl_cache import reset_ttl_cache
    from app.mcp_client import client as mcp_client
    from app.observability import tool_stats

    tool_stats.reset()
    reset_ttl_cache()

    calls = {"n": 0}

    class FlakyClient:
        async def call(self, tool, arguments):
            calls["n"] += 1
            if calls["n"] == 1:
                raise mcp_client.McpCallError("boom")
            return {"ok": True}

    hub = mcp_client.MCPHub()
    hub._started = True
    hub._clients = {"erp": FlakyClient()}
    monkeypatch.setattr(mcp_client.asyncio, "sleep", _no_sleep)

    result = asyncio.run(hub.call("erp", "get_sales_history", {"sku_id": "ST-01-C1-M"}))
    assert result == {"ok": True} and calls["n"] == 2
    row = tool_stats.snapshot()["tools"]["erp.get_sales_history"]
    assert row["calls"] == 1 and row["ok"] == 1
    assert row["retries"] == 1 and row["success_rate"] == 1.0
    tool_stats.reset()


def test_mcp_hub_records_final_failure(monkeypatch):
    from app.cache.ttl_cache import reset_ttl_cache
    from app.mcp_client import client as mcp_client
    from app.observability import tool_stats

    tool_stats.reset()
    reset_ttl_cache()

    class BrokenClient:
        async def call(self, tool, arguments):
            raise mcp_client.McpCallError("down")

    hub = mcp_client.MCPHub()
    hub._started = True
    hub._clients = {"pailitao": BrokenClient()}
    monkeypatch.setattr(mcp_client.asyncio, "sleep", _no_sleep)
    try:
        asyncio.run(hub.call("pailitao", "get_style_heat", {"style_id": "ST-01"}))
    except mcp_client.McpCallError:
        pass
    row = tool_stats.snapshot()["tools"]["pailitao.get_style_heat"]
    assert row["calls"] == 1 and row["errors"] == 1 and row["success_rate"] == 0.0
    tool_stats.reset()


def test_chat_tool_contract_records_reliability(seeded):
    """对话侧工具（tool_contract 装饰）也进同一张可靠性表（server=chat）。"""
    from app.auth import AuthUser
    from app.observability import tool_stats
    from app.tools import platform_tools

    tool_stats.reset()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sku_id": "ST-92-C1-M", "available": 10})

    client = platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler),
    )
    platform_tools.set_platform_client(client)
    try:
        user = AuthUser(username="admin", role="admin", department=None)
        tools = {t.name: t for t in platform_tools.build_platform_tools(user)}
        asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-92-C1-M"}))
    finally:
        platform_tools.set_platform_client(None)
    row = tool_stats.snapshot()["tools"].get("chat.get_inventory")
    assert row and row["calls"] == 1 and row["ok"] == 1
    tool_stats.reset()


def test_metrics_exposes_tools_and_cost(app_client):
    from app.observability import tool_stats

    tool_stats.reset()
    tool_stats.record("erp", "get_supplier_info", True, 8.0)
    data = app_client.get("/api/metrics").json()
    assert "tools" in data
    assert data["tools"]["tools"]["erp.get_supplier_info"]["calls"] == 1
    cost = data["llm"]["cost"]
    assert cost["currency"] == "CNY"
    assert "price_input_per_million" in cost and "total_cny" in cost
    tool_stats.reset()


def test_run_detail_carries_turn_cost():
    from app.agents.chat_agent import _build_run_detail
    from app.auth import AuthUser
    from app.config import get_settings
    from app.memory.runtime import turn_log_context

    user = AuthUser(username="admin", role="admin", department=None)
    settings = get_settings()
    usage = {"calls": 2, "input_tokens": 1000, "output_tokens": 500}
    with turn_log_context("t_cost", "s_1"):
        detail = _build_run_detail("t_cost", {}, usage, user)
    expected = round(
        (1000 / 1_000_000) * settings.llm_price_input
        + (500 / 1_000_000) * settings.llm_price_output,
        6,
    )
    assert detail["llm"]["cost_cny"] == expected
    assert detail["llm"]["cost_cny"] > 0


def test_health_features_advertise_tool_stats_and_cost(app_client):
    data = app_client.get("/api/health").json()
    assert "tool_stats" in data["features"] and "cost" in data["features"]


def test_latency_snapshot_has_mcp_call_category():
    from app.observability import latency

    latency.record("mcp_call", 5.0)
    snap = latency.snapshot()
    assert "mcp_call" in snap["categories"]
    assert "MCP" in snap["labels"]["mcp_call"]


async def _no_sleep(_seconds):
    return None
