"""MCP 适配器 ↔ 真实中台（进程内 ASGI，无网络无端口）。

验证评审 耦合点1 的修法是否真的成立：
1. 3 个 MCP server 的所有工具都落在中台的端点上（不再碰 Agent 私有库）；
2. 中台的治理能看见 MCP 通道（审计里 `agent_id=mcp-<server>`）；
3. 错误映射保持旧契约（404 → `found=false` 业务形态；5xx/连不上 → 抛错计入熔断）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import pathlib

import httpx
import pytest

TOKEN = "demo-internal-token"


@pytest.fixture(scope="module")
def platform_mart(tmp_path_factory):
    """真实中台：独立 SQLite + 自举数据 + 跑完 ETL（mart 就绪）。"""
    from data_platform.api import cache as pcache
    from data_platform.db import init_schema, make_engine, set_engine
    from data_platform.ingest.airflow_mock import run_dag
    from data_platform.mock_sources import generate_all

    tmp = pathlib.Path(tmp_path_factory.mktemp("mcp_adapter"))
    engine = make_engine(f"sqlite:///{(tmp / 'platform.db').as_posix()}")
    set_engine(engine)
    init_schema(engine)
    pcache.reset_client()
    generate_all(snapshot_date=dt.date(2026, 11, 3), seed=7)
    result = run_dag("daily_sync")
    assert all(t["status"] == "success" for t in result["tasks"].values()), result

    from data_platform.db import RawLogistics, RawOrder, session_scope

    with session_scope() as session:
        order = session.query(RawOrder).filter(RawOrder.status == "已发货").first()
        sku_id = order.sku_id
        order_id = order.order_id
        logistics = session.get(RawLogistics, order_id)
        tracking_no = logistics.tracking_no if logistics is not None else ""

    from data_platform.api.main import app

    yield {
        "engine": engine, "app": app, "sku_id": sku_id, "order_id": order_id,
        "tracking_no": tracking_no,
    }
    set_engine(None)


@pytest.fixture()
def wired(platform_mart, _settings):
    """把 MCP 适配层的 httpx 客户端指向进程内的中台 app，并预载 SKU 目录。"""
    from app.mcp_servers import _platform as mcp_platform
    from app.tools import catalog
    from data_platform.db import set_engine

    set_engine(platform_mart["engine"])
    transport = httpx.ASGITransport(app=platform_mart["app"])
    mcp_platform.set_transport(transport)
    # 目录预载：Agent 侧部门作用域判定不落到本地库兜底
    from app.tools import platform_tools

    platform_tools.set_platform_client(platform_tools.PlatformClient(
        base_url="http://platform.local", token=TOKEN, transport=transport))
    try:
        asyncio.run(catalog.sync())
        yield platform_mart
    finally:
        catalog.reset()
        platform_tools.set_platform_client(None)
        mcp_platform.reset_transport()


def _call(server: str, tool: str, args: dict) -> dict:
    from app.mcp_client.client import get_mcp_hub

    return asyncio.run(get_mcp_hub().call(server, tool, args, use_cache=False))


# ---------------------------------------------------------------- ERP server
def test_erp_inventory_maps_platform_payload(wired):
    data = _call("erp", "get_sku_inventory", {"sku_ids": [wired["sku_id"], "ST-99-C9-X"]})
    assert data["count"] == 1
    item = data["items"][0]
    assert item["sku_id"] == wired["sku_id"]
    # 老契约字段齐全（调用方不用改）
    for key in ("style_id", "style_name", "category", "season", "color", "size",
                "warehouse_id", "platform", "price", "stock", "available",
                "return_rate", "supplier_id", "lead_time_days"):
        assert key in item, key
    # 缺失 SKU 不报错，而是显式列出（批量接口不该整批失败）
    assert data["missing"] == ["ST-99-C9-X"]


def test_erp_sales_and_transit(wired):
    series = _call("erp", "get_sales_history", {"sku_id": wired["sku_id"], "days": 7})
    assert len(series["daily"]) == 7
    assert series["hourly_last_24h"] == [] and series["hourly_note"]

    transit = _call("erp", "get_in_transit", {"sku_id": wired["sku_id"]})
    assert "items" in transit


def test_erp_sibling_sizes_expose_size_curve(wired):
    data = _call("erp", "get_sibling_sizes", {"style_id": "ST-02"})
    assert data["items"]
    assert data["size_curve"] and data["size_curve"][0]["size"]
    assert data["size_gap"]["missing_sizes"] == ["M", "L"]


def test_erp_supplier_and_activities(wired):
    supplier = _call("erp", "get_supplier_info", {"supplier_id": "SUP-01"})
    assert supplier["found"] is True and supplier["on_time_rate"] > 0
    activities = _call("erp", "get_platform_activities", {"platform": "淘系", "days": 45})
    assert activities["items"] and activities["items"][0]["start_date"]


# ---------------------------------------------------------------- order server
def test_order_status_comes_from_platform(wired):
    data = _call("order", "get_order_status", {"order_id": wired["order_id"]})
    assert data["found"] is True
    assert data["order"]["sku_id"] == wired["sku_id"]
    if wired["tracking_no"]:
        assert data["logistics"]["tracking_no"] == wired["tracking_no"]


def test_missing_order_is_business_not_found(wired):
    data = _call("order", "get_order_status", {"order_id": "ORD-NOT-EXIST"})
    assert data["found"] is False and data["note"] == "订单不存在"
    assert "error" not in data


def test_list_orders_and_return_risk(wired):
    listed = _call("order", "list_orders", {"sku_id": wired["sku_id"], "days": 30, "limit": 5})
    assert listed["found"] is True and listed["count"] <= 5
    risk = _call("order", "get_return_risk", {"sku_id": wired["sku_id"]})
    assert risk["found"] is True and risk["risk"] in ("高", "低")


def test_after_sales_idempotent_through_adapter(wired):
    args = {"order_id": wired["order_id"], "reason": "尺码不合适", "qty": 1}
    first = _call("order", "create_after_sales_proposal", args)
    second = _call("order", "create_after_sales_proposal", args)
    assert first["deduped"] is False
    assert second["deduped"] is True and second["ticket_id"] == first["ticket_id"]


def test_after_sales_validation_errors_pass_through(wired):
    bad = _call("order", "create_after_sales_proposal",
                {"order_id": wired["order_id"], "reason": "随便退", "qty": 1})
    assert "不支持的售后原因" in bad["error"]
    missing = _call("order", "create_after_sales_proposal",
                    {"order_id": "ORD-NOT-EXIST", "reason": "质量问题", "qty": 1})
    assert "订单不存在" in missing["error"]


# ---------------------------------------------------------------- pailitao server
def test_competitor_tools(wired):
    same = _call("pailitao", "search_same_style", {"style_id": "ST-01"})
    assert same["found"] is True and same["competitors"]
    band = _call("pailitao", "get_price_band", {"style_id": "ST-01"})
    assert band["price_min"] <= band["price_max"]
    heat = _call("pailitao", "get_style_heat", {"style_id": "ST-01"})
    assert heat["found"] is True and heat["heat_index"] >= 0


# ---------------------------------------------------------------- 治理覆盖
def test_mcp_calls_are_audited_as_mcp_identity(wired):
    _call("order", "get_order_status", {"order_id": wired["order_id"]})
    _call("erp", "get_supplier_info", {"supplier_id": "SUP-02"})

    # ASGITransport 只支持异步客户端
    async def _audit():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wired["app"]),
                                     base_url="http://platform.local",
                                     headers={"X-Internal-Token": TOKEN}) as client:
            return (await client.get("/api/v1/audit", params={"limit": 200})).json()

    audit = asyncio.run(_audit())
    agents = {item["agent_id"] for item in audit["items"]}
    assert "mcp-order" in agents, "MCP 通道没有进审计：治理覆盖不全"
    assert "mcp-erp" in agents


def test_link_failure_raises_for_breaker(wired):
    """连不上/5xx 必须抛错（计入 MCP 熔断），而不是返回一个看起来正常的结果。"""
    from app.mcp_servers import _platform as mcp_platform

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "platform down"})

    mcp_platform.set_transport(httpx.MockTransport(handler))
    # 直接打适配层：MCP hub 会把异常包一层（并计入自己的熔断），这里要验的是适配层语义
    with pytest.raises(mcp_platform.McpPlatformError) as exc:
        asyncio.run(mcp_platform.call("order", "GET", "/api/v1/orders/ORD-1"))
    assert "503" in str(exc.value)
