"""dbt 式转换模型：stg → dim/fct → mart（治理指标宽表）。

每个模型 = 一次全量重建（Demo 数据量小），按依赖顺序在 run_transforms 中执行。

服装行业口径（与 `governance.METRIC_DEFINITIONS` 一一对应，改公式必须两边一起改）：
- 尺码曲线 `size_share` = 该码近 30 天销量 ÷ 同款近 30 天总销量（主销码阈值 15%）
- 断码 `size_gap` = 主销码可用库存 ≤ 0（款级看 `broken_size_count` / `size_gap_rate`）
- 齐码 `full_size` = 该款所有应有尺码的可用库存都 > 0
- 周转 `turnover_days` = 可用库存 ÷ 日均销量（近 30 天口径，衡量健康度）
- 售罄 `sell_through_rate` = 近 30 天销量 ÷（近 30 天销量 + 当前可用库存）
- 生命周期 `lifecycle_stage`：上新(≤14 天) / 清仓(售罄率≥85% 或 >120 天) /
  长尾(>180 天且售罄率<20%) / 主推（其余）
"""

from __future__ import annotations

import datetime as dt
import math

from sqlalchemy import case, delete, func, select
from sqlalchemy.orm import Session

from data_platform.config import department_of
from data_platform.db import (
    DimCustomer,
    DimSku,
    DimStyle,
    FctInTransit,
    FctInventoryDaily,
    FctSalesDaily,
    MartSkuMetrics,
    MartStyleMetrics,
    RawCrmReservation,
    RawErpInTransit,
    RawErpInventory,
    RawOrder,
    StgErpInventory,
    StgOrder,
)

# 主销码阈值：该码销量占同款 ≥15% 视为主销码（服装行业通行的经验阈值）
MAIN_SIZE_SHARE = 0.15
# 生命周期阈值（天数）
NEW_LAUNCH_DAYS = 14
CLEARANCE_DAYS = 120
LONG_TAIL_DAYS = 180
CLEARANCE_SELL_THROUGH = 0.85
LONG_TAIL_SELL_THROUGH = 0.20


def lifecycle_of(days_since_launch: int | None, sell_through_rate: float) -> str:
    """款式生命周期阶段（确定性规则，见模块 docstring）。"""
    if days_since_launch is None:
        return ""
    if days_since_launch <= NEW_LAUNCH_DAYS:
        return "上新"
    if sell_through_rate >= CLEARANCE_SELL_THROUGH:
        return "清仓"
    if days_since_launch > LONG_TAIL_DAYS and sell_through_rate < LONG_TAIL_SELL_THROUGH:
        return "长尾"
    if days_since_launch > CLEARANCE_DAYS:
        return "清仓"
    return "主推"


def build_stg_erp_inventory(session: Session) -> int:
    session.execute(delete(StgErpInventory))
    rows = session.scalars(select(RawErpInventory)).all()
    for r in rows:
        session.add(StgErpInventory(
            sku_id=r.sku_id, warehouse_id=r.warehouse_id, on_hand=r.on_hand,
            locked=r.locked, category=r.category, lead_time_days=r.lead_time_days,
        ))
    return len(rows)


def build_stg_orders(session: Session) -> int:
    session.execute(delete(StgOrder))
    rows = session.scalars(select(RawOrder)).all()
    for r in rows:
        session.add(StgOrder(
            order_id=r.order_id, sku_id=r.sku_id, customer_id=r.customer_id,
            qty=r.qty, amount=r.amount, order_date=r.order_date, status=r.status,
        ))
    return len(rows)


def build_dim_sku(session: Session) -> int:
    session.execute(delete(DimSku))
    seen: set[str] = set()
    # 同款同一个尺码可能出现多次（多仓），尺码/颜色排序按首次出现的次序稳定生成
    size_rank: dict[str, int] = {}
    color_rank: dict[str, int] = {}
    for r in session.scalars(select(RawErpInventory)).all():
        if r.sku_id in seen:
            continue
        seen.add(r.sku_id)
        size_rank.setdefault(r.size, len(size_rank) + 1)
        if r.color not in color_rank:
            color_rank[r.color] = len(color_rank) + 1
        launch = None
        if r.snapshot_date and int(r.launch_days_ago or 0):
            launch = r.snapshot_date - dt.timedelta(days=int(r.launch_days_ago))
        session.add(DimSku(
            sku_id=r.sku_id, style_id=r.style_id, style_name=r.style_name,
            category=r.category, department=department_of(r.category) or "",
            season=r.season, color=r.color, size=r.size, price=r.price,
            platform=r.platform,
            list_price=float(r.list_price or r.price or 0),
            supplier_id=r.supplier_id, lead_time_days=r.lead_time_days,
            launch_date=launch,
            size_rank=size_rank[r.size], color_rank=color_rank.get(r.color, 0),
        ))
    return len(seen)


def build_dim_style(session: Session) -> int:
    """款维度：服装的最小企划单元（多款 × 多色 × 多码里的「款」）。"""
    session.execute(delete(DimStyle))
    skus: dict[str, list[DimSku]] = {}
    for row in session.scalars(select(DimSku)):
        skus.setdefault(row.style_id, []).append(row)
    for style_id, rows in skus.items():
        head = rows[0]
        sizes = sorted({r.size for r in rows}, key=lambda s: min(
            (r.size_rank for r in rows if r.size == s), default=0))
        session.add(DimStyle(
            style_id=style_id, style_name=head.style_name, category=head.category,
            department=head.department, season=head.season, launch_date=head.launch_date,
            size_spec=sizes,
            color_count=len({r.color for r in rows}),
            size_count=len(sizes), sku_count=len(rows),
        ))
    return len(skus)


def build_dim_customer(session: Session) -> int:
    session.execute(delete(DimCustomer))
    from data_platform.db import RawCrmCustomer

    rows = session.scalars(select(RawCrmCustomer)).all()
    for r in rows:
        session.add(DimCustomer(
            customer_id=r.customer_id, name_masked=r.name_masked,
            phone_masked=r.phone_masked, tier=r.tier, region=r.region,
        ))
    return len(rows)


def _active_reservations(session: Session, as_of: dt.date) -> dict[str, int]:
    rows = session.execute(
        select(RawCrmReservation.sku_id, func.sum(RawCrmReservation.qty))
        .where(RawCrmReservation.status == "待发货", RawCrmReservation.expected_ship >= as_of)
        .group_by(RawCrmReservation.sku_id)
    ).all()
    return {sku_id: int(qty or 0) for sku_id, qty in rows}


def _order_hold(session: Session, as_of: dt.date) -> dict[str, int]:
    rows = session.execute(
        select(RawOrder.sku_id, func.sum(RawOrder.qty))
        .where(RawOrder.status == "待发货", RawOrder.order_date >= as_of - dt.timedelta(days=6))
        .group_by(RawOrder.sku_id)
    ).all()
    return {sku_id: int(qty or 0) for sku_id, qty in rows}


def build_fct_inventory_daily(session: Session, as_of: dt.date) -> int:
    session.execute(delete(FctInventoryDaily))
    inv = session.execute(
        select(RawErpInventory.sku_id, func.sum(RawErpInventory.on_hand), func.sum(RawErpInventory.locked))
        .group_by(RawErpInventory.sku_id)
    ).all()
    reserved = _active_reservations(session, as_of)
    hold = _order_hold(session, as_of)
    # 主仓取库存最大的仓库（同一 SKU 可能有第二仓库存，用于调拨建议）
    main_wh: dict[str, str] = {}
    wh_on_hand: dict[str, int] = {}
    for r in session.scalars(select(RawErpInventory)):
        if r.sku_id not in wh_on_hand or r.on_hand > wh_on_hand[r.sku_id]:
            wh_on_hand[r.sku_id] = r.on_hand
            main_wh[r.sku_id] = r.warehouse_id
    n = 0
    for sku_id, on_hand, locked in inv:
        session.add(FctInventoryDaily(
            as_of=as_of, sku_id=sku_id, warehouse_id=main_wh.get(sku_id, ""),
            on_hand=int(on_hand or 0), locked=int(locked or 0),
            reserved_crm=reserved.get(sku_id, 0), order_hold=hold.get(sku_id, 0),
        ))
        n += 1
    return n


def build_fct_sales_daily(session: Session, as_of: dt.date) -> int:
    session.execute(delete(FctSalesDaily))
    d7 = as_of - dt.timedelta(days=6)
    d30 = as_of - dt.timedelta(days=29)
    # 用 CASE WHEN 而不是 SQLite 专有的 `iif()`：同一段 SQL 在 MySQL 也能跑
    q7_expr = case((RawOrder.order_date >= d7, RawOrder.qty), else_=0)
    q30_expr = case((RawOrder.order_date >= d30, RawOrder.qty), else_=0)
    rows = session.execute(
        select(
            RawOrder.sku_id,
            func.sum(q7_expr),
            func.sum(q30_expr),
        ).group_by(RawOrder.sku_id)
    ).all()
    n = 0
    for sku_id, q7, q30 in rows:
        q7, q30 = int(q7 or 0), int(q30 or 0)
        session.add(FctSalesDaily(
            as_of=as_of, sku_id=sku_id, qty_7d=q7, qty_30d=q30,
            daily_avg_7d=round(q7 / 7.0, 2), daily_avg_30d=round(q30 / 30.0, 2),
        ))
        n += 1
    return n


def build_fct_in_transit(session: Session, as_of: dt.date) -> int:
    session.execute(delete(FctInTransit))
    rows = session.scalars(select(RawErpInTransit)).all()
    for r in rows:
        session.add(FctInTransit(
            as_of=as_of, sku_id=r.sku_id, qty=r.qty, eta=r.eta,
            days_to_arrive=max(0, (r.eta - as_of).days),
        ))
    return len(rows)


def _sku_facts(session: Session) -> dict[str, dict]:
    """把 mart 需要的「SKU 级事实」一次性算好（库存/销量/在途/退货率/价格）。"""
    dims = {r.sku_id: r for r in session.scalars(select(DimSku))}
    inv = {r.sku_id: r for r in session.scalars(select(FctInventoryDaily))}
    sales = {r.sku_id: r for r in session.scalars(select(FctSalesDaily))}
    transit_7d: dict[str, int] = {}
    for r in session.scalars(select(FctInTransit)):
        if r.days_to_arrive <= 7:
            transit_7d[r.sku_id] = transit_7d.get(r.sku_id, 0) + r.qty
    # 退货率来自 ERP 主数据（多仓重复记录取第一条）
    return_rate: dict[str, float] = {}
    for r in session.scalars(select(RawErpInventory)):
        return_rate.setdefault(r.sku_id, float(r.return_rate or 0))

    facts: dict[str, dict] = {}
    for sku_id, dim in dims.items():
        inv_row = inv.get(sku_id)
        sales_row = sales.get(sku_id)
        on_hand = int(inv_row.on_hand) if inv_row else 0
        locked = int(inv_row.locked) if inv_row else 0
        reserved = int(inv_row.reserved_crm) if inv_row else 0
        hold = int(inv_row.order_hold) if inv_row else 0
        available = max(0, on_hand - locked - reserved - hold)
        daily7 = float(sales_row.daily_avg_7d) if sales_row else 0.0
        daily30 = float(sales_row.daily_avg_30d) if sales_row else 0.0
        qty30 = int(sales_row.qty_30d) if sales_row else 0
        facts[sku_id] = {
            "dim": dim, "on_hand": on_hand, "locked": locked, "reserved": reserved,
            "hold": hold, "available": available, "daily7": daily7, "daily30": daily30,
            "qty30": qty30, "transit7": int(transit_7d.get(sku_id, 0)),
            "return_rate": float(return_rate.get(sku_id, 0.0)),
            "warehouse_id": inv_row.warehouse_id if inv_row else "",
        }
    return facts


def build_mart_sku_metrics(session: Session, as_of: dt.date) -> int:
    """SKU 级 mart：基础经营指标 + 服装口径（尺码占比/断码/周转/售罄/生命周期）。"""
    session.execute(delete(MartSkuMetrics))
    facts = _sku_facts(session)

    # 先按款聚合，得到尺码曲线与生命周期（SKU 级字段从款级继承，保证口径一致）
    by_style: dict[str, list[dict]] = {}
    for sku_id, fact in facts.items():
        by_style.setdefault(fact["dim"].style_id, []).append(fact)

    style_ctx: dict[str, dict] = {}
    for style_id, rows in by_style.items():
        qty30_total = sum(r["qty30"] for r in rows)
        available_total = sum(r["available"] for r in rows)
        sell_through = (qty30_total / (qty30_total + available_total)) if (qty30_total + available_total) else 0.0
        launch = rows[0]["dim"].launch_date
        days = (as_of - launch).days if launch else None
        # 同款同尺码可能有多个颜色：尺码曲线按「尺码合计」算
        size_qty: dict[str, int] = {}
        for r in rows:
            size_qty[r["dim"].size] = size_qty.get(r["dim"].size, 0) + r["qty30"]
        style_ctx[style_id] = {
            "qty30_total": qty30_total,
            "size_qty": size_qty,
            "sell_through": sell_through,
            "days": days,
            "lifecycle": lifecycle_of(days, sell_through),
        }

    n = 0
    for sku_id, fact in facts.items():
        dim = fact["dim"]
        ctx = style_ctx.get(dim.style_id, {})
        qty30_total = int(ctx.get("qty30_total") or 0)
        size_qty = int((ctx.get("size_qty") or {}).get(dim.size, 0))
        size_share = round(size_qty / qty30_total, 4) if qty30_total else 0.0
        main_size = 1 if size_share >= MAIN_SIZE_SHARE else 0
        gap = 1 if (main_size and fact["available"] <= 0) else 0
        turnover = round(fact["available"] / fact["daily30"], 2) if fact["daily30"] > 0 else None
        sell_through = round(float(ctx.get("sell_through") or 0.0), 4)

        daily7, transit = fact["daily7"], fact["transit7"]
        safety = math.ceil(daily7 * dim.lead_time_days * 0.3)
        reorder = math.ceil(daily7 * (dim.lead_time_days + 3)) + safety
        sell_days = round((fact["available"] + transit) / daily7, 2) if daily7 > 0 else None
        if sell_days is None:
            risk = "低"
        elif sell_days <= 7:
            risk = "高"
        elif sell_days <= 15:
            risk = "中"
        else:
            risk = "低"

        session.add(MartSkuMetrics(
            as_of=as_of, sku_id=sku_id, style_id=dim.style_id,
            category=dim.category, department=dim.department,
            warehouse_id=fact["warehouse_id"], color=dim.color, size=dim.size,
            on_hand=fact["on_hand"], locked=fact["locked"], reserved_crm=fact["reserved"],
            order_hold=fact["hold"], available=fact["available"],
            daily_avg_7d=round(daily7, 2), daily_avg_30d=round(fact["daily30"], 2),
            qty_30d=fact["qty30"], sold_30d_flag=1 if fact["qty30"] > 0 else 0,
            in_transit_7d=transit, safety_stock=safety, reorder_point=reorder,
            sell_through_days=sell_days, stockout_risk=risk,
            lead_time_days=dim.lead_time_days, price=dim.price,
            platform=dim.platform, list_price=dim.list_price,
            return_rate=round(fact["return_rate"], 4),
            size_share=size_share, main_size_flag=main_size, size_gap_flag=gap,
            turnover_days=turnover, sell_through_rate=sell_through,
            lifecycle_stage=str(ctx.get("lifecycle") or ""),
            days_since_launch=ctx.get("days"),
            updated_at=dt.datetime.now(),
        ))
        n += 1
    return n


def build_mart_style_metrics(session: Session, as_of: dt.date) -> int:
    """款级 mart：服装决策的最小单元（齐码率/断码率/尺码曲线/生命周期/周转/售罄）。"""
    session.execute(delete(MartStyleMetrics))
    dims = {r.style_id: r for r in session.scalars(select(DimStyle))}
    launch_by_style: dict[str, dt.date | None] = {
        r.style_id: r.launch_date for r in session.scalars(select(DimSku))
    }
    sku_rows = [r for r in session.scalars(select(MartSkuMetrics))]
    by_style: dict[str, list[MartSkuMetrics]] = {}
    for row in sku_rows:
        by_style.setdefault(row.style_id, []).append(row)

    n = 0
    for style_id, rows in by_style.items():
        dim = dims.get(style_id)
        size_spec = list(dim.size_spec) if dim is not None and dim.size_spec else sorted(
            {r.size for r in rows})
        qty30_total = sum(int(r.qty_30d or 0) for r in rows)
        available_total = sum(int(r.available or 0) for r in rows)
        daily7_total = sum(float(r.daily_avg_7d or 0) for r in rows)
        daily30_total = sum(float(r.daily_avg_30d or 0) for r in rows)
        sell_through = (qty30_total / (qty30_total + available_total)
                        if (qty30_total + available_total) else 0.0)
        launch = launch_by_style.get(style_id)

        # 尺码曲线：按尺码聚合（同码多色相加），占比用于识别主销码
        size_qty: dict[str, int] = {}
        size_available: dict[str, int] = {}
        for r in rows:
            size_qty[r.size] = size_qty.get(r.size, 0) + int(r.qty_30d or 0)
            size_available[r.size] = size_available.get(r.size, 0) + int(r.available or 0)
        curve: list[dict] = []
        for size in size_spec:
            qty = size_qty.get(size, 0)
            share = round(qty / qty30_total, 4) if qty30_total else 0.0
            curve.append({
                "size": size, "qty_30d": qty, "available": size_available.get(size, 0),
                "size_share": share, "main_size_flag": 1 if share >= MAIN_SIZE_SHARE else 0,
                "gap": 1 if (share >= MAIN_SIZE_SHARE and size_available.get(size, 0) <= 0) else 0,
            })
        listed = [c for c in curve if c["available"] > 0]
        gaps = [c for c in curve if c["gap"]]
        days = (as_of - launch).days if launch else None
        session.add(MartStyleMetrics(
            as_of=as_of, style_id=style_id,
            style_name=(dim.style_name if dim is not None else ""),
            category=rows[0].category, department=rows[0].department,
            season=dim.season if dim is not None else "",
            launch_date=launch, days_since_launch=days,
            lifecycle_stage=lifecycle_of(days, sell_through),
            sku_count=len(rows),
            color_count=len({r.color for r in rows}),
            size_count=len(size_spec), listed_size_count=len(listed),
            total_available=available_total, qty_30d=qty30_total,
            daily_avg_7d=round(daily7_total, 2),
            sell_through_rate=round(sell_through, 4),
            turnover_days=(round(available_total / daily30_total, 2)
                           if daily30_total > 0 else None),
            full_size_flag=1 if len(listed) == len(size_spec) and size_spec else 0,
            broken_size_count=len(gaps),
            size_gap_rate=round(len(gaps) / len(size_spec), 4) if size_spec else 0.0,
            size_curve=curve,
            updated_at=dt.datetime.now(),
        ))
        n += 1
    return n
