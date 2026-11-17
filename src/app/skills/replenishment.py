"""Skill 子图 2：补货测算（LangGraph SubGraph，确定性计算，不依赖 LLM）。

口径来自知识库《补货 SOP》：
- 日均销量 = 近 7 天小时销量 ÷ 7
- 安全库存 = 日均 × 交期 × 0.3
- 再订货点 = 日均 × (交期 + 3) + 安全库存
- 建议补货量 = (交期 + 21) × 日均 − 现有库存 − 7 天内在途 + 安全库存，并按 MOQ 向上取整
- 单次补货上限 500 件（演示策略，可配置）
"""

from __future__ import annotations

import math
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.skills.stock import available_of, stock_source

MAX_SINGLE_REPLENISH = 500


class ReplenishState(TypedDict, total=False):
    sku: dict
    features: dict
    in_transit: list[dict]
    supplier: dict
    replenish: dict


def _replenish(state: ReplenishState) -> dict:
    sku = state.get("sku") or {}
    features = state.get("features") or {}
    in_transit = state.get("in_transit") or []
    supplier = state.get("supplier") or {}

    daily = float(features.get("base_daily") or 0.0)
    # 复审 A1：补货量以**可用库存**为基准（AGENTS.md 口径）；用现存会少补。
    stock = available_of(sku)
    source = stock_source(sku)
    lead = int(supplier.get("lead_time_days") or 10)
    moq = int(supplier.get("moq") or 100)
    notes: list[str] = []
    if source == "on_hand":
        notes.append("缺少 available 字段：补货量按现存库存估算（偏保守/少补），建议人工复核")

    safety = math.ceil(daily * lead * 0.3)
    reorder_point = math.ceil(daily * (lead + 3)) + safety
    target_days = lead + 21

    in_transit_7d = sum(
        int(it.get("qty") or 0) for it in in_transit
        if int(it.get("days_to_arrive", 999)) <= 7
    )

    # 再订货点优先：库存（含 7 天内在途）高于再订货点时，目标覆盖天数测算不应再触发补货，
    # 否则会出现「库存已高于再订货点却仍建议补货」的互相矛盾口径。
    effective_stock = stock + in_transit_7d
    if effective_stock >= reorder_point:
        suggested = 0
        notes.append("库存（含 7 天内在途）高于再订货点，无需补货")
    else:
        suggested = math.ceil(target_days * daily - stock - in_transit_7d + safety)
        if suggested <= 0:
            suggested = 0
            notes.append("覆盖天数已满足，建议不补货，转为观察/促销动作")
        elif suggested < moq:
            suggested = moq
            notes.append(f"测算量低于供应商 MOQ({moq})，按 MOQ 补货")
        else:
            suggested = math.ceil(suggested / moq) * moq
            notes.append(f"按 MOQ({moq}) 整数倍向上取整")

    if suggested > MAX_SINGLE_REPLENISH:
        notes.append(f"测算量超过单次上限 {MAX_SINGLE_REPLENISH} 件，已截断为上限")
        suggested = MAX_SINGLE_REPLENISH

    coverage_after = (stock + in_transit_7d + suggested) / max(daily, 0.5)

    return {
        "replenish": {
            "daily_sales": round(daily, 2),
            "lead_time_days": lead,
            "safety_stock": safety,
            "reorder_point": reorder_point,
            "target_days": target_days,
            "suggested_qty": suggested,
            "moq": moq,
            "in_transit_7d": in_transit_7d,
            "coverage_days_after": round(coverage_after, 1),
            "stock_source": source,
            "available_stock": stock,
            "notes": notes,
        }
    }


def build_replenishment_skill():
    graph = StateGraph(ReplenishState)
    graph.add_node("replenish", _replenish)
    graph.add_edge(START, "replenish")
    graph.add_edge("replenish", END)
    return graph.compile(name="skill_replenishment")
