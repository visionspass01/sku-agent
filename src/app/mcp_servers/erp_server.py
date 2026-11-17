"""ERP MCP Server（FastMCP）：库存/销量/在途/调拨/尺码结构/供应商/大促窗口。

数据来源：**数据中台 REST**（见 `_platform.py`）。本文件只做「工具契约 → 中台端点」的映射，
不碰任何数据库——早先它直连 Agent 私有库，属于评审指出的耦合点 1。
工具名/参数/返回结构保持不变，调用方（对话工具、批量 Worker）无需改动。
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from app.mcp_servers._platform import call, is_error

mcp = FastMCP("erp-mcp")


def _sku_dict(item: dict) -> dict:
    """中台指标 → 老 MCP 契约。字段名保持不变，避免调用方连锁改动。"""
    return {
        "sku_id": item.get("sku_id", ""),
        "style_id": item.get("style_id", ""),
        "style_name": item.get("style_name", ""),
        "category": item.get("category", ""),
        "season": item.get("season", ""),
        "color": item.get("color", ""),
        "size": item.get("size", ""),
        "warehouse_id": item.get("warehouse_id", ""),
        "platform": item.get("platform", ""),
        "price": item.get("price", 0),
        "list_price": item.get("list_price", 0),
        # 存量与可用是不同口径：`stock` 保持"现存库存"语义（老调用方按它算可售天数），
        # 治理口径的可用库存单独给出，避免历史脚本静默换口径。
        # 注意（复审 F1）：`available` 缺失时必须保留 None——`app/skills/stock.py` 靠它判断
        # "要不要回落现存并标注偏乐观"；这里若默认成 0，回落分支永远不生效，
        # 而且会把"上游没这个字段"误解释成"可用库存为 0"（超补 + 误判断码/缺货）。
        "stock": item.get("on_hand", 0),
        "available": item.get("available"),
        "return_rate": round(float(item.get("return_rate") or 0), 4),
        "supplier_id": item.get("supplier_id", ""),
        "lead_time_days": item.get("lead_time_days"),
        "lifecycle_stage": item.get("lifecycle_stage", ""),
        "size_gap_flag": item.get("size_gap_flag", 0),
    }


@mcp.tool()
async def get_sku_inventory(sku_ids: list[str]) -> dict:
    """查询 SKU 库存余量与基础资料（品类/季节/仓/平台/供应商交期）。"""
    ids = [str(s) for s in (sku_ids or []) if str(s).strip()][:50]
    if not ids:
        return {"sku_ids": [], "items": [], "count": 0}
    data = await call("erp", "GET", "/api/v1/skus/batch", params={"sku_ids": ",".join(ids)})
    if is_error(data):
        return data
    items = [_sku_dict(x) for x in (data.get("items") or [])]
    return {"sku_ids": ids, "items": items, "count": len(items),
            "missing": data.get("missing") or []}


@mcp.tool()
async def get_sales_history(sku_id: str, days: int = 7) -> dict:
    """查询 SKU 近 N 天日销量序列（含零销量天）。

    注意：小时级销量不在中台口径内（需要按需接入流量明细），这里显式标注而不是返回假数据。
    """
    days = max(1, min(int(days), 30))
    data = await call("erp", "GET", f"/api/v1/skus/{sku_id}/sales-series", params={"days": days})
    if is_error(data):
        return data
    return {
        "sku_id": sku_id, "days": days,
        "daily": data.get("daily") or [],
        "hourly_last_24h": [],
        "hourly_note": "中台只治理日粒度销量；小时级流量需单独接入（此字段返回空而不是零值序列）",
    }


@mcp.tool()
async def get_in_transit(sku_id: str) -> dict:
    """查询 SKU 在途采购（数量 / 预计到货日 / 采购单号）。"""
    data = await call("erp", "GET", f"/api/v1/skus/{sku_id}/in-transit")
    if is_error(data):
        return data
    return {"sku_id": sku_id, "items": data.get("items") or [],
            "total": data.get("total", 0)}


@mcp.tool()
async def get_warehouse_transfers(sku_id: str) -> dict:
    """查询 SKU 在途仓库调拨。"""
    data = await call("erp", "GET", f"/api/v1/skus/{sku_id}/transfers")
    if is_error(data):
        return data
    return {"sku_id": sku_id, "items": data.get("items") or []}


@mcp.tool()
async def get_sibling_sizes(style_id: str) -> dict:
    """查询同款（style_id）全部尺码的库存与**尺码语义**（主销码 / 断码标记）。

    为什么要把 `main_size_flag` / `size_gap_flag` 透传出去（复审 A1）：批量研判的
    断码判定必须与 `AGENTS.md`/平台 mart 一致——「主销码可用库存为 0 才是断码」，
    而主销码需要 30 天尺码曲线（只有中台算得了），本地按"任意码缺货"猜会误报。
    """
    data = await call("erp", "GET", f"/api/v1/styles/{style_id}/metrics")
    if is_error(data):
        return data
    items = [
        {"sku_id": row.get("sku_id", ""), "style_id": style_id,
         "color": row.get("color", ""), "size": row.get("size", ""),
         # `stock` 保留为「可用库存」的兼容别名（历史调用方按 stock 字段算缺货），
         # 语义以 `available` 为准（缺失时保留 None，见 F1）；`on_hand` 只用于对照。
         "stock": row.get("available") if row.get("available") is not None
         else row.get("on_hand", 0),
         "available": row.get("available"),
         "on_hand": row.get("on_hand", 0), "price": row.get("price", 0),
         "warehouse_id": row.get("warehouse_id", ""),
         "size_share": row.get("size_share", 0),
         "main_size_flag": row.get("main_size_flag"),
         "size_gap_flag": row.get("size_gap_flag")}
        for row in (data.get("size_breakdown") or [])
    ]
    return {
        "style_id": style_id, "items": items,
        "full_size_flag": data.get("full_size_flag"),
        "size_gap": data.get("size_gap") or {},
        "size_curve": data.get("size_curve") or [],
        "lifecycle_stage": data.get("lifecycle_stage", ""),
        # 尺码语义是否可用：False 时上层必须回落到旧判据并显式标注（老归档/老中台）
        "size_semantics": bool(
            any(row.get("main_size_flag") is not None for row in (data.get("size_breakdown") or []))
        ),
    }


@mcp.tool()
async def get_supplier_info(supplier_id: str) -> dict:
    """查询供应商交期 / MOQ / 准时率。"""
    data = await call("erp", "GET", f"/api/v1/suppliers/{supplier_id}")
    if is_error(data):
        return data
    return data


@mcp.tool()
async def get_platform_activities(platform: str, days: int = 45) -> dict:
    """查询平台在 N 天内开始/进行的大促活动窗口。"""
    days = max(1, min(int(days), 180))
    data = await call("erp", "GET", "/api/v1/platform-activities",
                      params={"platform": platform, "days": days})
    if is_error(data):
        return data
    return {"platform": data.get("platform", platform), "items": data.get("items") or []}


ERP_TOOLS = {
    "get_sku_inventory": get_sku_inventory,
    "get_sales_history": get_sales_history,
    "get_in_transit": get_in_transit,
    "get_warehouse_transfers": get_warehouse_transfers,
    "get_sibling_sizes": get_sibling_sizes,
    "get_supplier_info": get_supplier_info,
    "get_platform_activities": get_platform_activities,
}


if __name__ == "__main__":
    mcp.run(transport="stdio")
