"""MCP 工具注册表：把工具签名/语义整理成 Prompt 片段，Agent 动态感知可用工具。"""

from __future__ import annotations

ERP_TOOL_DOCS = {
    "get_sku_inventory": "查询 SKU 库存余量与资料：库存、退货率、品类、季节、仓、平台、供应商交期/MOQ。参数：sku_ids: list[str]",
    "get_sales_history": "查询 SKU 近 N 天日销量序列与最近 24h 小时销量。参数：sku_id: str, days: int（默认 7）",
    "get_in_transit": "查询 SKU 在途采购：数量、ETA、剩余到货天数。参数：sku_id: str",
    "get_warehouse_transfers": "查询 SKU 在途调拨：来源仓/目标仓/数量/ETA。参数：sku_id: str",
    "get_sibling_sizes": "查询同款（style_id）全部尺码库存与价格，用于断码分析。参数：style_id: str",
    "get_supplier_info": "查询供应商交期、MOQ、准时交付率。参数：supplier_id: str",
    "get_platform_activities": "查询平台近 N 天内的大促活动窗口与报名规则。参数：platform: str, days: int（默认 45）",
}

PAILITAO_TOOL_DOCS = {
    "search_same_style": "拍立淘同款检索：竞品店铺/价格/近 30 天与 7 天销量/评分/趋势，返回价格带与热度指数。参数：style_id: str",
    "get_price_band": "查询同款竞品价格带（最低/最高/中位/均价）。参数：style_id: str",
    "get_style_heat": "查询同款市场热度趋势（近 7 天竞品销量与环比）。参数：style_id: str",
}


def tools_prompt() -> str:
    lines = ["## ERP MCP 工具（server=erp）"]
    for name, doc in ERP_TOOL_DOCS.items():
        lines.append(f"- {name}: {doc}")
    lines.append("")
    lines.append("## 拍立淘 MCP 工具（server=pailitao）")
    for name, doc in PAILITAO_TOOL_DOCS.items():
        lines.append(f"- {name}: {doc}")
    return "\n".join(lines)
