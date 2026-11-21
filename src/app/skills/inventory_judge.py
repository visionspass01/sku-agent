"""Skill 子图 1：库存研判（LangGraph SubGraph，状态隔离、纯计算、可单测）。

输入：ERP/拍立淘 MCP 拉回的数据包；输出：可售天数、健康度、风险标签。
主图只消费 judgment 结果，不感知内部实现——（子图封装业务技能）。
"""

from __future__ import annotations

from typing import TypedDict

import numpy as np
from langgraph.graph import END, START, StateGraph

from app.skills.stock import available_of, stock_source


class JudgeState(TypedDict, total=False):
    sku: dict
    features: dict
    sibling_sizes: list[dict]
    activities: list[dict]
    as_of: str
    judgment: dict


ACTIVE_SEASON_MONTHS = {
    "秋冬": {9, 10, 11, 12, 1, 2},
    "春夏": {3, 4, 5, 6, 7, 8},
    "四季": set(range(1, 13)),
}


def _judge(state: JudgeState) -> dict:
    sku = state.get("sku") or {}
    features = state.get("features") or {}
    siblings = state.get("sibling_sizes") or []
    activities = state.get("activities") or []

    daily = float(features.get("base_daily") or 0.0)
    # 复审 A1：库存一律按**可用**口径（AGENTS.md：可用 = 现存 − 锁定 − 预订 − 未发货）；
    # `stock` 是现存，直接拿来算可售天数会偏乐观。取不到 available 时回落到现存，
    # 并由 `stock_source` 在 notes 里显式标注。
    stock = available_of(sku)
    source = stock_source(sku)
    days = float(features.get("days_of_supply") or (stock / max(daily, 0.5)))
    slope = float(features.get("slope_norm") or 0.0)
    zero_sizes = int(features.get("zero_sizes") or 0)
    broken_size_count = features.get("style_broken_size_count")
    self_broken = features.get("self_broken_size")
    size_semantics = str(features.get("size_semantics") or "")

    month = int((state.get("as_of") or "")[5:7] or 1)
    active = month in ACTIVE_SEASON_MONTHS.get(sku.get("season", "四季"), ACTIVE_SEASON_MONTHS["四季"])
    # 大促备货：有活动窗口且本身是有动销的高库存（避免把低动销滞销款误判成备货）
    promo_near = any(int(a.get("days_to_start", 999)) <= 30 for a in activities) and daily >= 3

    flags: list[str] = []
    health = 100.0
    notes: list[str] = []

    # 断码：`AGENTS.md` 口径是「**本码**为主销码（30 天尺码曲线占比 ≥15%）且可用库存为 0」——
    # 长尾码缺货不算断码；同款其它码断了也不代表本 SKU 断码（只在 notes 里说明）。
    # 旧归档没有主销码标记时回落到「≥2 个码缺货且本 SKU 无库存」，并注明是旧口径。
    if self_broken is not None:
        if self_broken:
            flags.append("断码")
            health -= 15 * min(max(int(broken_size_count or 1), 1), 3)
            notes.append("本码为主销码且可用库存为 0（断码）")
        if int(broken_size_count or 0) > 0:
            sizes_text = "、".join(features.get("style_broken_sizes") or [])
            notes.append(f"同款主销码断货 {int(broken_size_count)} 个"
                         + (f"（{sizes_text}）" if sizes_text else ""))
    elif zero_sizes >= 2 and stock <= 0:
        flags.append("断码")
        health -= 15 * min(zero_sizes, 3)
        notes.append(f"同款 {zero_sizes} 个尺码断货（旧口径：无主销码标记）")
    if days <= 7 and daily >= 3:
        flags.append("缺货")
        health -= 40
        notes.append(f"可售仅 {days:.1f} 天")
    if days >= 60 and slope <= 0.02:
        flags.append("积压候选")
        health -= 20
        notes.append(f"可售 {days:.0f} 天且销量不增")
    if 45 <= days < 60 and active and slope <= 0.05:
        flags.append("换季季末")
        health -= 12
        notes.append("在店季节临近尾声且需求未增长")
    if promo_near and days >= 60:
        flags.append("大促备货")
        notes.append("30 天内有大促活动，高库存可能属正常备货")
    if float(sku.get("return_rate") or 0) >= 0.18:
        flags.append("退货率异常")
        health -= 10
    if source == "on_hand":
        notes.append("当前环境缺少 available 字段，可售天数/缺货判定按现存库存估算（偏乐观）")

    style_sales = sum(
        1 for s in siblings if int(s.get("stock") or 0) > 0
    )
    notes.append(f"同款有货尺码 {style_sales}/{len(siblings)}")

    return {
        "judgment": {
            "days_of_supply": round(days, 1),
            "health_score": int(max(0, min(100, round(health)))),
            "risk_flags": flags,
            "zero_sizes": zero_sizes,
            "broken_size_count": broken_size_count,
            "self_broken_size": self_broken,
            "size_semantics": size_semantics or ("main_size" if self_broken is not None else "legacy"),
            "stock_source": source,
            "available_stock": stock,
            "active_season": active,
            "promo_window_near": promo_near,
            "notes": notes,
        }
    }


def build_inventory_judge_skill():
    graph = StateGraph(JudgeState)
    graph.add_node("judge", _judge)
    graph.add_edge(START, "judge")
    graph.add_edge("judge", END)
    return graph.compile(name="skill_inventory_judge")


def compute_feature_hints(sku: dict, daily_series: list[dict]) -> dict:
    """从 MCP 拉回的日销量序列计算技能所需特征（与筛查脚本口径一致）。"""
    qty = np.array([float(d.get("qty", 0)) for d in daily_series], dtype=float) if daily_series else np.zeros(1)
    if len(qty) >= 3 and qty.mean() > 0:
        t = np.linspace(0.0, 1.0, len(qty))
        slope = float(np.polyfit(t, qty, 1)[0])
        slope_norm = slope / max(float(qty.mean()), 1e-6)
    else:
        slope_norm = 0.0
    base_daily = float(qty[-7:].mean()) if len(qty) else 0.0
    # 复审 A1：可售天数按**可用库存**算（现存会偏乐观）；缺字段回落现存并由 stock_source 标注
    stock = available_of(sku)
    return {
        "base_daily": round(base_daily, 2),
        "slope_norm": round(slope_norm, 4),
        "r2": 0.0,
        "days_of_supply": round(stock / max(base_daily, 0.5), 1),
        "zero_sizes": 0,
        "stock_source": stock_source(sku),
    }
