"""Agent Tools 测试：中台调用（MockTransport）、部门权限、HITL 阈值。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest


def _canned_platform() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/inventory"):
            return httpx.Response(200, json={
                "sku_id": "ST-05-C1-M", "on_hand": 600, "locked": 30,
                "reserved_crm": 40, "order_hold": 20, "available": 510,
                "warehouse_id": "WH-HZ", "as_of": "2026-11-03", "cached": False,
            })
        if path.endswith("/sell-through"):
            return httpx.Response(200, json={
                "sku_id": "ST-05-C1-M", "available": 510, "in_transit_7d": 100,
                "daily_avg_7d": 20.0, "sell_through_days": 30.5, "stockout_risk": "低",
            })
        if path.endswith("/erp/replenish-orders"):
            body = json.loads(request.content or b"{}")
            qty = int(body.get("qty") or 0)
            high = qty >= 200
            return httpx.Response(200, json={
                "approval_id": "appr-1", "status": "pending_confirmation" if high else "auto_approved",
                "qty": qty, "amount": qty * 100.0, "po_no": "PO-TEST",
                "high_risk": high, "message": "ok",
            })
        if path.endswith("/approvals"):
            return httpx.Response(200, json={"status": "pending", "items": [
                {"approval_id": "demo-1", "sku_id": "ST-05-C1-M", "qty": 500, "amount": 50000,
                 "reason": "test", "status": "pending_confirmation", "po_no": "PO-X"},
            ]})
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.MockTransport(handler)


@pytest.fixture()
def platform_client(monkeypatch):
    from app.tools import platform_tools

    client = platform_tools.PlatformClient(
        base_url="http://mock.platform", token="demo-internal-token", transport=_canned_platform(),
    )
    platform_tools.set_platform_client(client)
    yield client
    platform_tools.set_platform_client(None)


def _call(tool, payload):
    return asyncio.run(tool.ainvoke(payload))


def test_tool_permission_denied(seeded, platform_client):
    from app.auth import AuthUser
    from app.tools.platform_tools import build_platform_tools

    user = AuthUser(username="worker1", role="user", department="男装")
    tools = {t.name: t for t in build_platform_tools(user)}
    result = _call(tools["get_inventory"], {"sku_id": "ST-90-C1-M"})  # 女装 SKU
    assert "error" in result and "无权限" in result["error"]


def test_tool_calls_platform_for_own_department(seeded, platform_client):
    from app.auth import AuthUser
    from app.tools.platform_tools import build_platform_tools

    user = AuthUser(username="worker1", role="user", department="男装")
    tools = {t.name: t for t in build_platform_tools(user)}
    inv = _call(tools["get_inventory"], {"sku_id": "ST-92-C1-M"})  # 男装
    assert inv["available"] == 510
    st = _call(tools["get_sell_through"], {"sku_id": "ST-92-C1-M"})
    assert st["sell_through_days"] == 30.5


def test_replenish_tool_high_risk_pending(seeded, platform_client):
    from app.auth import AuthUser
    from app.tools.platform_tools import build_platform_tools

    user = AuthUser(username="admin", role="admin", department=None)
    tools = {t.name: t for t in build_platform_tools(user)}
    res = _call(tools["create_replenishment_order"],
                {"sku_id": "ST-92-C1-M", "qty": 500, "reason": "低库存"})
    assert res["status"] == "pending_confirmation"
    low = _call(tools["create_replenishment_order"],
                {"sku_id": "ST-92-C1-M", "qty": 50, "reason": "小单"})
    assert low["status"] == "auto_approved"


def test_pending_approvals_tool(seeded, platform_client):
    from app.auth import AuthUser
    from app.tools.platform_tools import build_platform_tools

    user = AuthUser(username="admin", role="admin", department=None)
    tools = {t.name: t for t in build_platform_tools(user)}
    data = _call(tools["list_pending_approvals"], {})
    assert data["items"][0]["approval_id"] == "demo-1"


def test_replenish_tool_sends_stable_idempotency_key(seeded):
    """重试安全：同一轮次内同参数重复提交 → 幂等键一致；参数或轮次变化 → 键变化。"""
    from app.auth import AuthUser
    from app.memory.runtime import tool_context, turn_log_context
    from app.tools import platform_tools
    from app.tools.platform_tools import build_platform_tools

    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/erp/replenish-orders"):
            body = json.loads(request.content or b"{}")
            keys.append(body.get("idempotency_key") or "")
            return httpx.Response(200, json={"approval_id": "appr-1", "status": "auto_approved"})
        return httpx.Response(404, json={"detail": "not found"})

    platform_tools.set_platform_client(platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler)))
    try:
        user = AuthUser(username="admin", role="admin", department=None)
        tools = {t.name: t for t in build_platform_tools(user)}
        payload = {"sku_id": "ST-92-C1-M", "qty": 50, "reason": "低库存"}

        with tool_context(session_id="sess-1", username="admin"), \
                turn_log_context(turn_id="turn-1", session_id="sess-1"):
            _call(tools["create_replenishment_order"], payload)
            _call(tools["create_replenishment_order"], payload)          # 同轮重试
            _call(tools["create_replenishment_order"], {**payload, "qty": 51})
        with tool_context(session_id="sess-1", username="admin"), \
                turn_log_context(turn_id="turn-2", session_id="sess-1"):
            _call(tools["create_replenishment_order"], payload)          # 下一轮 = 新意图

        assert len(keys) == 4 and all(len(k) == 40 for k in keys)
        assert keys[0] == keys[1]                 # 幂等：重试键相同
        assert keys[0] != keys[2]                 # 数量变了 → 新键
        assert keys[0] != keys[3]                 # 轮次变了 → 新键
    finally:
        platform_tools.set_platform_client(None)
