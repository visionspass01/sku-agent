"""拍立淘 MCP Server：竞品同款行情（价格带 / 近 30 天销量 / 热度趋势）。

数据来源：**数据中台 REST**（`/api/v1/styles/{id}/competitors|price-band|heat`）。
竞品行情是公司级事实（生图 Agent 以后也要用它定价），因此归中台治理，不放 Agent 私有库。
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from app.mcp_servers._platform import call, is_error

mcp = FastMCP("pailitao-mcp")


@mcp.tool()
async def search_same_style(style_id: str) -> dict:
    """检索同款竞品商品：店铺、价格、近 30 天/7 天销量、评分、销量趋势。"""
    data = await call("pailitao", "GET", f"/api/v1/styles/{style_id}/competitors")
    if is_error(data):
        return data
    if not data.get("found"):
        return {
            "style_id": style_id, "found": False, "competitors": [],
            "note": data.get("note") or f"未检索到同款竞品（style_id={style_id}）",
        }
    return {
        "style_id": style_id, "found": True,
        "competitors": data.get("competitors") or [],
        "price_min": data.get("price_min"), "price_max": data.get("price_max"),
        "price_avg": data.get("price_avg"),
        # 老字段名（styled_heat_index）保留：批量 Worker/脚本按它取值
        "styled_heat_index": data.get("heat_index", 0.0),
        "trend_pct": data.get("trend_pct", 0.0),
    }


@mcp.tool()
async def get_price_band(style_id: str) -> dict:
    """查询同款竞品价格带（最低/最高/均价/中位），用于定价与清仓折扣决策。"""
    data = await call("pailitao", "GET", f"/api/v1/styles/{style_id}/price-band")
    return data


@mcp.tool()
async def get_style_heat(style_id: str) -> dict:
    """查询同款市场热度：近 7 天竞品销量环比、热度指数。"""
    data = await call("pailitao", "GET", f"/api/v1/styles/{style_id}/heat")
    return data


PAILITAO_TOOLS = {
    "search_same_style": search_same_style,
    "get_price_band": get_price_band,
    "get_style_heat": get_style_heat,
}


if __name__ == "__main__":
    mcp.run(transport="stdio")
