"""Agent 输入上下文拼装：把 MCP 数据包/Skill 研判格式化成紧凑 Prompt。"""

from __future__ import annotations

import json

from app.mcp_client.registry import tools_prompt
from app.skills.stock import available_of, on_hand_of, stock_source


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)[:6000]


def format_single_sku_brief(sku: dict, features: dict) -> str:
    return (
        f"SKU: {sku.get('sku_id')} 款号:{sku.get('style_id')} 品名:{sku.get('style_name')}\n"
        f"品类:{sku.get('category')} 季节:{sku.get('season')} 尺码:{sku.get('size')} 颜色:{sku.get('color')}\n"
        f"仓:{sku.get('warehouse_id')} 平台:{sku.get('platform')} 售价:{sku.get('price')}\n"
        f"可用库存:{available_of(sku)}（现存 {on_hand_of(sku)}） 在途/调拨见数据包 "
        f"退货率:{sku.get('return_rate')}\n"
        f"供应商:{sku.get('supplier_id')} 交期:{sku.get('lead_time_days')}天 MOQ:{sku.get('moq')}\n"
        f"近7天日均销量:{features.get('base_daily')} 可售天数:{features.get('days_of_supply')} "
        f"销量斜率:{features.get('slope_norm')}"
    )


def format_data_bundle(data: dict) -> str:
    erp = data.get("erp") or {}
    market = data.get("market") or {}
    sku = erp.get("sku") or {}
    sales = erp.get("sales") or {}
    parts = [
        "## SKU 基础信息",
        _json(sku),
        # 复审 A1：把两种库存口径摆在同一行，避免模型把「现存」当「可用」直接换算。
        "## 库存口径（可用 = 现存 − 锁定 − CRM 预订 − 订单占用）",
        f"可用库存: {available_of(sku)}  现存: {on_hand_of(sku)}  "
        f"数据来源: {stock_source(sku)}"
        + ("（缺 available 字段，按现存估算）" if stock_source(sku) == "on_hand" else ""),
        "## 日销量序列（近 7 天）",
        _json(sales.get("daily") or []),
        "## 在途采购",
        _json(erp.get("in_transit") or []),
        "## 在途调拨",
        _json(erp.get("transfers") or []),
        "## 同款尺码结构",
        _json(erp.get("sibling_sizes") or []),
        "## 供应商交期",
        _json(erp.get("supplier") or {}),
        "## 平台活动窗口",
        _json(erp.get("activities") or []),
        "## 拍立淘同款行情",
        _json(market or {}),
    ]
    return "\n".join(parts)


def format_skill_outputs(judgment: dict, replenish: dict) -> str:
    return (
        "## 库存研判 Skill 输出\n" + _json(judgment)
        + "\n## 补货测算 Skill 输出\n" + _json(replenish)
    )


def format_rules(context: str) -> str:
    return "## 检索到的 SOP/规则片段（引用时使用 [chunk_id]）\n" + (context or "（无）")


def format_task_brief(state: dict) -> str:
    data = state.get("data") or {}
    erp = data.get("erp") or {}
    sku = erp.get("sku") or {}
    return format_single_sku_brief(sku, erp.get("features") or {})


__all__ = [
    "tools_prompt",
    "format_single_sku_brief",
    "format_data_bundle",
    "format_skill_outputs",
    "format_rules",
    "format_task_brief",
]
