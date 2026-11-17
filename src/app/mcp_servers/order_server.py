"""订单 / 物流 / 售后 MCP Server（FastMCP）。

数据来源：**数据中台 REST**（`/api/v1/orders/*`、`/api/v1/after-sales`）。
早先这里直连 Agent 私有库（`mock_order`/`mock_logistics`/`after_sales_ticket`），
属于评审指出的耦合点 1；现在订单域是公司级业务事实，统一由中台持有与治理。

写操作（发起售后）带**幂等键**：同订单 + 同原因 + 同数量重复提交只建一张工单，
返回 `pending_confirmation`（与补货单一致的人工确认语义）。
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from app.mcp_servers._platform import call, is_error

mcp = FastMCP("order-mcp")


@mcp.tool()
async def get_order_status(order_id: str) -> dict:
    """查询订单状态（含物流与售后工单摘要）。"""
    data = await call("order", "GET", f"/api/v1/orders/{order_id}")
    if is_error(data):
        if data.get("status_code") == 404:
            return {"order_id": order_id, "found": False, "note": "订单不存在"}
        return data
    return {
        "order_id": order_id, "found": True,
        "order": data.get("order") or {},
        "logistics": data.get("logistics") or {},
        "after_sales": data.get("after_sales") or [],
    }


@mcp.tool()
async def list_orders(sku_id: str, days: int = 30, limit: int = 10) -> dict:
    """查询某 SKU 近 N 天的订单（按时间倒序）。"""
    days = max(1, min(int(days), 90))
    limit = max(1, min(int(limit), 50))
    data = await call("order", "GET", f"/api/v1/skus/{sku_id}/orders",
                      params={"days": min(days, 30), "limit": limit})
    if is_error(data):
        if data.get("status_code") == 404:
            return {"sku_id": sku_id, "found": False, "days": days, "count": 0, "orders": [],
                    "note": f"SKU {sku_id} 不存在"}
        return data
    orders = (data.get("items") or [])[:limit]
    return {
        "sku_id": sku_id, "found": True, "days": days,
        "count": len(orders), "orders": orders,
        "total_qty": data.get("total_qty", 0), "pending_qty": data.get("pending_qty", 0),
        "note": "",
    }


@mcp.tool()
async def get_logistics(order_id: str) -> dict:
    """查询订单物流状态与预计到货。"""
    data = await call("order", "GET", f"/api/v1/orders/{order_id}/logistics")
    if is_error(data):
        if data.get("status_code") == 404:
            return {"order_id": order_id, "found": False, "note": "订单不存在"}
        return data
    return data


@mcp.tool()
async def get_return_risk(sku_id: str, days: int = 30) -> dict:
    """查询 SKU 退货风险：退货率、近期订单量、售后工单量。"""
    days = max(1, min(int(days), 90))
    data = await call("order", "GET", f"/api/v1/skus/{sku_id}/return-risk", params={"days": days})
    if is_error(data):
        if data.get("status_code") == 404:
            return {"sku_id": sku_id, "found": False, "note": "SKU 不存在"}
        return data
    return data


@mcp.tool()
async def create_after_sales_proposal(order_id: str, reason: str, qty: int | None = None) -> dict:
    """发起售后工单（幂等；返回 pending_confirmation，等待人工确认，不会自动退款）。"""
    body: dict = {"order_id": order_id, "reason": (reason or "").strip()}
    if qty is not None:
        body["qty"] = int(qty)
    data = await call("order", "POST", "/api/v1/after-sales", json_body=body)
    if is_error(data):
        return {"error": data.get("error") or "售后工单创建失败"}
    return data


ORDER_TOOLS = {
    "get_order_status": get_order_status,
    "list_orders": list_orders,
    "get_logistics": get_logistics,
    "get_return_risk": get_return_risk,
    "create_after_sales_proposal": create_after_sales_proposal,
}


if __name__ == "__main__":
    mcp.run(transport="stdio")
