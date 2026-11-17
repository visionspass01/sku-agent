"""订单/物流/售后 MCP：状态查询、列表、幂等写操作、部门隔离。

幂等是这里最关键的断言：售后是写操作，重复提交不能建出两张工单。
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.auth import AuthUser
from app.db.engine import session_scope
from app.db.models import AfterSalesTicket, MockLogistics, MockOrder


def _orders():
    now = dt.datetime(2026, 11, 2, 10, 0)
    rows = [
        MockOrder(order_id="ORD-9001", sku_id="ST-90-C1-M", qty=2, amount=598.0,
                  status="delivered", channel="淘系", created_at=now - dt.timedelta(days=3),
                  paid_at=now - dt.timedelta(days=3), shipped_at=now - dt.timedelta(days=2)),
        MockOrder(order_id="ORD-9002", sku_id="ST-92-C1-M", qty=1, amount=299.0,
                  status="paid", channel="抖音", created_at=now - dt.timedelta(days=1),
                  paid_at=now - dt.timedelta(days=1), shipped_at=None),
        MockOrder(order_id="ORD-9003", sku_id="ST-90-C1-M", qty=1, amount=299.0,
                  status="after_sales", channel="淘系", created_at=now - dt.timedelta(days=5),
                  paid_at=now - dt.timedelta(days=5), shipped_at=now - dt.timedelta(days=4)),
    ]
    logistics = [
        MockLogistics(order_id="ORD-9001", carrier="顺丰", tracking_no="SFD1234567890",
                      status="已签收", eta=(now - dt.timedelta(days=1)).date(),
                      updated_at=now - dt.timedelta(days=1)),
        MockLogistics(order_id="ORD-9003", carrier="中通", tracking_no="ZD9876543210",
                      status="运输中", eta=(now + dt.timedelta(days=1)).date(),
                      updated_at=now),
    ]
    ticket = AfterSalesTicket(ticket_id="t-9003", order_id="ORD-9003", sku_id="ST-90-C1-M",
                              reason="尺码不合适", qty=1, status="created",
                              idempotency_key="seed-key-9003", created_at=now)
    return rows, logistics, ticket


@pytest.fixture()
def order_data(seeded):
    rows, logistics, ticket = _orders()
    with session_scope() as session:
        if session.get(MockOrder, "ORD-9001") is None:
            session.add_all(rows)
            session.add_all(logistics)
            session.add(ticket)
    return {"now": dt.datetime(2026, 11, 2, 10, 0)}


def _call(server: str, tool: str, args: dict, use_cache: bool = False) -> dict:
    from app.mcp_client.client import get_mcp_hub

    return asyncio.run(get_mcp_hub().call(server, tool, args, use_cache=use_cache))


def test_order_status_and_logistics(order_data):
    data = _call("order", "get_order_status", {"order_id": "ORD-9001"})
    assert data["found"] is True
    assert data["order"]["sku_id"] == "ST-90-C1-M"
    assert data["order"]["status"] == "delivered"
    assert data["logistics"]["tracking_no"] == "SFD1234567890"

    missing = _call("order", "get_order_status", {"order_id": "ORD-0000"})
    assert missing["found"] is False

    logistics = _call("order", "get_logistics", {"order_id": "ORD-9001"})
    assert logistics["logistics"]["carrier"] == "顺丰"


def test_list_orders_by_sku(order_data):
    data = _call("order", "list_orders", {"sku_id": "ST-90-C1-M", "days": 30})
    assert data["found"] is True
    # 只断言本用例种下的订单：共享测试库里别的用例也会往同一个 SKU 写订单（ORD-W*），
    # 全等断言会让测试结果依赖文件执行顺序
    got = {o["order_id"] for o in data["orders"] if o["order_id"].startswith("ORD-900")}
    assert got == {"ORD-9001", "ORD-9003"}

    empty = _call("order", "list_orders", {"sku_id": "ST-99-C1-M", "days": 30})
    assert empty["found"] is False and empty["orders"] == []


def test_return_risk(order_data):
    data = _call("order", "get_return_risk", {"sku_id": "ST-90-C1-M"})
    assert data["found"] is True
    assert data["risk"] in ("高", "低")
    assert data["orders_last_days"] >= 1


def test_after_sales_idempotent(order_data):
    first = _call("order", "create_after_sales_proposal",
                  {"order_id": "ORD-9001", "reason": "尺码不合适", "qty": 1})
    assert first["status"] == "pending_confirmation" and first["deduped"] is False
    second = _call("order", "create_after_sales_proposal",
                   {"order_id": "ORD-9001", "reason": "尺码不合适", "qty": 1})
    assert second["deduped"] is True
    assert second["ticket_id"] == first["ticket_id"]
    with session_scope() as session:
        rows = [t for t in session.query(AfterSalesTicket)
                if t.order_id == "ORD-9001" and t.reason == "尺码不合适"]
        assert len(rows) == 1, "相同幂等键只应有一张工单"


def test_after_sales_validation(order_data):
    bad_reason = _call("order", "create_after_sales_proposal",
                       {"order_id": "ORD-9001", "reason": "随便退", "qty": 1})
    assert "不支持的售后原因" in bad_reason["error"]

    too_many = _call("order", "create_after_sales_proposal",
                     {"order_id": "ORD-9001", "reason": "质量问题", "qty": 5})
    assert "超过订单购买数量" in too_many["error"]

    no_order = _call("order", "create_after_sales_proposal",
                     {"order_id": "ORD-0000", "reason": "质量问题", "qty": 1})
    assert "订单不存在" in no_order["error"]


def test_order_tools_respect_department_scope(order_data):
    from app.tools import platform_tools

    worker2 = AuthUser(username="worker2", role="user", department="女装")
    worker1 = AuthUser(username="worker1", role="user", department="男装")
    tools_w2 = {t.name: t for t in platform_tools.build_platform_tools(worker2)}
    tools_w1 = {t.name: t for t in platform_tools.build_platform_tools(worker1)}

    # 女装账号看男装订单 → 无权限（不泄露订单内容）
    denied = asyncio.run(tools_w2["get_order_status"].ainvoke({"order_id": "ORD-9002"}))
    assert "无权限" in str(denied.get("error", denied.get("summary", "")))

    # 女装账号看本部门订单 → 正常
    ok = asyncio.run(tools_w2["get_order_status"].ainvoke({"order_id": "ORD-9001"}))
    assert ok.get("found") is True or "ST-90-C1-M" in str(ok)

    # 男装账号看女装订单 → 无权限
    denied2 = asyncio.run(tools_w1["list_orders"].ainvoke({"sku_id": "ST-90-C1-M", "days": 30}))
    assert "无权限" in str(denied2.get("error", denied2.get("summary", "")))


def test_create_after_sales_tool_goes_through_mcp(order_data):
    from app.tools import platform_tools

    admin = AuthUser(username="admin", role="admin", department=None)
    tools = {t.name: t for t in platform_tools.build_platform_tools(admin)}
    result = asyncio.run(tools["create_after_sales"].ainvoke(
        {"order_id": "ORD-9002", "reason": "7天无理由", "qty": 1}
    ))
    assert result.get("status") == "pending_confirmation" or "ticket_id" in str(result)
