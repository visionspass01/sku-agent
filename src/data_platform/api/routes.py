"""数据中台 RESTful 服务：治理指标 / 库存 / 在途 / 预订 / 订单 / 补货审批（Mock ERP）。

治理下沉之后的职责划分：
- **认证/配额/审计**在 `api/main.py` 的中间件里（谁在调、超没超配额、结果如何）；
- **本文件**负责「业务 + scope 声明 + 部门作用域校验 + 条件更新」：
  每个端点用 `require_scope(...)` 声明要什么能力，用 `_guard_sku(...)` 校验部门，
  审批用**单条条件 UPDATE** 防止两个 Agent 同时确认（早先是读-判断-写，会双写）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from data_platform.api import audit, cache
from data_platform.api import schemas as sc
from data_platform.api.authz import (
    Principal,
    current_principal,
    department_scope,
    ensure_department,
    require_scope,
)
from data_platform.capabilities import catalog
from data_platform.config import (
    DEPARTMENT_CATEGORIES,
    INTERNAL_TOKEN,  # noqa: F401 —— 兼容旧 import（真正校验在中间件）
    REPLENISH_AMOUNT_THRESHOLD,
    REPLENISH_QTY_THRESHOLD,
    department_of,
)
from data_platform.contract import contract_meta
from data_platform.db import (
    ApprovalRequest,
    DimSku,
    ErpReplenishOrder,
    EtlRunLog,
    FctSalesDaily,
    MartSkuMetrics,
    MartStyleMetrics,
    MetaDataVersion,
    RawAfterSalesTicket,
    RawCompetitorListing,
    RawCrmCustomer,
    RawCrmReservation,
    RawErpInTransit,
    RawErpInventory,
    RawLogistics,
    RawOrder,
    RawPlatformActivity,
    RawSupplier,
    RawTransferOrder,
    session_scope,
)
from data_platform.governance import METRIC_DEFINITIONS, QUERY_SYNONYMS, metadata_prompt_text

router = APIRouter(prefix="/api/v1")


def verify_token(request: Request) -> Principal:
    """兼容旧代码的入口：真正的认证在中间件，这里只做"拿不到主体就 401"的纵深校验。"""
    return current_principal(request)


def _data_version(session, request: Request | None = None) -> int:  # noqa: ANN001
    """mart 数据版本；顺手写进 request.state 供中间件回 `X-Data-Version` 响应头。"""
    row = session.get(MetaDataVersion, "mart")
    version = int(row.version) if row else 0
    if request is not None:
        request.state.data_version = version
    return version


def _mapping_version() -> str:
    """部门映射的内容指纹：映射变了指纹就变，Agent 侧据此刷新本地缓存（无需重启）。"""
    payload = json.dumps({k: sorted(v) for k, v in DEPARTMENT_CATEGORIES.items()},
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]



def _cache_key(version: int, name: str, args: str) -> str:
    return f"v{version}:{name}:{args}"


def _scope_tag(principal: Principal) -> str:
    """缓存 key 里必须带作用域：同一接口对不同主体可能返回不同范围的数据。"""
    return "all" if principal.departments is None else "+".join(sorted(principal.departments))


def _mart(session, sku_id: str) -> MartSkuMetrics:  # noqa: ANN001
    row = session.scalar(
        select(MartSkuMetrics).where(MartSkuMetrics.sku_id == sku_id)
        .order_by(MartSkuMetrics.as_of.desc())
    )
    if row is None:
        raise HTTPException(404, f"数据中台无该 SKU：{sku_id}")
    return row


def _guard_sku(session, principal: Principal, sku_id: str, request: Request):  # noqa: ANN001
    """取到指标行 + 部门校验（越权 403：中台是边界，需要可诊断）。"""
    row = _mart(session, sku_id)
    ensure_department(principal, row.category, request, sku_id=sku_id)
    return row


def _metric_payload(row: MartSkuMetrics, version: int) -> dict:
    return {
        "sku_id": row.sku_id,
        "as_of": row.as_of.isoformat(),
        "style_id": getattr(row, "style_id", "") or "",
        "category": row.category,
        "department": row.department,
        "warehouse_id": row.warehouse_id,
        "color": getattr(row, "color", "") or "",
        "size": getattr(row, "size", "") or "",
        "on_hand": row.on_hand,
        "locked": row.locked,
        "reserved_crm": row.reserved_crm,
        "order_hold": row.order_hold,
        "available": row.available,
        "daily_avg_7d": row.daily_avg_7d,
        "daily_avg_30d": row.daily_avg_30d,
        "qty_30d": row.qty_30d,
        "sold_30d_flag": row.sold_30d_flag,
        "in_transit_7d": row.in_transit_7d,
        "safety_stock": row.safety_stock,
        "reorder_point": row.reorder_point,
        "sell_through_days": row.sell_through_days,
        "stockout_risk": row.stockout_risk,
        "lead_time_days": row.lead_time_days,
        "price": row.price,
        # ---- 服装口径（公式见 governance.METRIC_DEFINITIONS）----
        "return_rate": getattr(row, "return_rate", 0.0) or 0.0,
        "size_share": getattr(row, "size_share", 0.0) or 0.0,
        "main_size_flag": getattr(row, "main_size_flag", 0) or 0,
        "size_gap_flag": getattr(row, "size_gap_flag", 0) or 0,
        "turnover_days": getattr(row, "turnover_days", None),
        "sell_through_rate": getattr(row, "sell_through_rate", 0.0) or 0.0,
        "lifecycle_stage": getattr(row, "lifecycle_stage", "") or "",
        "days_since_launch": getattr(row, "days_since_launch", None),
        "data_version": version,
    }


def _style_payload(row: MartStyleMetrics, version: int) -> dict:
    return {
        "style_id": row.style_id,
        "style_name": row.style_name,
        "category": row.category,
        "department": row.department,
        "season": row.season,
        "lifecycle_stage": row.lifecycle_stage,
        "launch_date": row.launch_date.isoformat() if row.launch_date else None,
        "days_since_launch": row.days_since_launch,
        "sku_count": row.sku_count,
        "color_count": row.color_count,
        "size_count": row.size_count,
        "listed_size_count": row.listed_size_count,
        "total_available": row.total_available,
        "qty_30d": row.qty_30d,
        "daily_avg_7d": row.daily_avg_7d,
        "sell_through_rate": row.sell_through_rate,
        "turnover_days": row.turnover_days,
        "full_size_flag": bool(row.full_size_flag),
        "size_gap": {
            "broken_size_count": row.broken_size_count,
            "size_gap_rate": row.size_gap_rate,
            "missing_sizes": [c["size"] for c in (row.size_curve or []) if c.get("gap")],
            "listed_sizes": [c["size"] for c in (row.size_curve or []) if c.get("available", 0) > 0],
        },
        "size_curve": row.size_curve or [],
        "data_version": version,
    }


def _mart_style(session, style_id: str) -> MartStyleMetrics:  # noqa: ANN001
    row = session.scalar(
        select(MartStyleMetrics).where(MartStyleMetrics.style_id == style_id)
        .order_by(MartStyleMetrics.as_of.desc())
    )
    if row is None:
        raise HTTPException(404, f"数据中台无该款式：{style_id}")
    return row


def _guard_style(session, principal: Principal, style_id: str, request: Request):  # noqa: ANN001
    """款级端点同样按部门校验（款 → 品类 → 部门），越权 403 便于诊断。"""
    row = _mart_style(session, style_id)
    ensure_department(principal, row.category, request, sku_id=f"style:{style_id}")
    return row


# ---------------------------------------------------------------- 探活 / 元数据
@router.get("/health", response_model=sc.HealthOut)
def health(request: Request) -> dict:
    """探活：不要求令牌（compose healthcheck / Agent 预热）。带令牌时回显调用方。"""
    principal = getattr(request.state, "principal", None)
    with session_scope() as session:
        version = _data_version(session, request)
        count = session.scalar(select(func.count()).select_from(MartSkuMetrics)) or 0
    return {"status": "ok", "time": dt.datetime.now().isoformat(timespec="seconds"),
            "data_version": version, "mart_rows": int(count), "cache_keys": cache.stats(),
            "caller": principal.to_public() if principal is not None else None}


@router.get("/metadata/metrics", response_model=sc.MetricsMetaOut,
            dependencies=[Depends(require_scope("read"))])
def metadata_metrics(request: Request) -> dict:
    with session_scope() as session:
        version = _data_version(session, request)
    return {
        "data_version": version,
        "metrics": METRIC_DEFINITIONS,
        "prompt_context": metadata_prompt_text(),
        "desensitization": {
            "note": "客户姓名/手机号/收货人信息在接入层脱敏，API 不返回原始 PII",
        },
        # 契约版本随元数据下发：Agent 侧可只凭一次调用完成「能力/口径/契约」三项自检
        "contract": contract_meta(),
    }


@router.get("/metadata/departments", response_model=sc.DepartmentsOut,
            dependencies=[Depends(require_scope("read"))])
def metadata_departments() -> dict:
    """部门 ↔ 品类映射的**唯一真相**（Agent 启动时拉取，离线用本地默认值兜底）。"""
    return {
        "departments": {name: list(cats) for name, cats in DEPARTMENT_CATEGORIES.items()},
        "version": _mapping_version(),
        "note": "部门作用域以中台为准；Agent 侧仅缓存副本，避免两侧手写映射漂移",
    }


@router.get("/metadata/capabilities", response_model=sc.CapabilitiesOut,
            dependencies=[Depends(require_scope("read"))])
def metadata_capabilities() -> dict:
    """能力目录：Agent 工具分组/作用域声明的**唯一真相**（见 data_platform/capabilities.py）。

    Agent 侧用提交到仓库的快照离线启动，启动时与本接口比对版本；不一致会出现在
    `/api/health.capabilities`，而不是等到某个工具调用凭空失效才发现。
    """
    return catalog()


@router.get("/metadata/synonyms", response_model=sc.SynonymsOut,
            dependencies=[Depends(require_scope("read"))])
def metadata_synonyms(request: Request) -> dict:
    """检索同义词（RAG 查询改写用）：**业务知识归中台**，Agent 不再硬编码一份词表。"""
    with session_scope() as session:
        version = _data_version(session, request)
    payload = json.dumps(QUERY_SYNONYMS, ensure_ascii=False, sort_keys=True)
    return {
        "version": int(hashlib.sha1(payload.encode("utf-8")).hexdigest()[:8], 16),
        "synonyms": {k: list(v) for k, v in QUERY_SYNONYMS.items()},
        "data_version": version,
        "note": "改词表不用发 Agent 版本：Agent 启动时拉一次并按 TTL 刷新",
    }


@router.get("/metadata/catalog", response_model=sc.CatalogOut,
            dependencies=[Depends(require_scope("read"))])
def metadata_catalog(request: Request,
                     offset: int = Query(0, ge=0),
                     limit: int = Query(500, ge=1, le=2000),
                     principal: Principal = Depends(current_principal)) -> dict:
    """SKU 参考目录（只含标识与分类，不含库存/销量等业务事实）。

    为什么需要它：Agent 侧做「部门作用域二次防御」时要判 `sku_id → category`，
    早先直接读自己的 `sku` 表（业务数据双份真相）；现在读这份**元数据**目录，
    并按调用方部门过滤——业务事实仍然只在业务接口里。
    """
    scope = department_scope(principal)
    with session_scope() as session:
        version = _data_version(session, request)
        stmt = select(DimSku)
        if scope is not None:
            stmt = stmt.where(DimSku.department.in_(list(scope)))
        total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        rows = session.scalars(stmt.order_by(DimSku.sku_id).offset(offset).limit(limit)).all()
        items = [
            {
                "sku_id": r.sku_id, "style_id": r.style_id, "style_name": r.style_name,
                "category": r.category, "department": r.department, "season": r.season,
                "color": r.color, "size": r.size, "price": r.price,
                "supplier_id": r.supplier_id,
            }
            for r in rows
        ]
    next_offset = offset + len(items)
    return {
        "items": items, "count": len(items), "total": total, "offset": offset,
        "next_offset": next_offset if next_offset < total else None,
        "scope": list(scope) if scope is not None else None,
        "scope_filtered": scope is not None,
        "data_version": version,
        "note": "仅元数据（不含库存/销量）；按调用方部门过滤，分页拉全量",
    }


# ---------------------------------------------------------------- 读接口（按 SKU）
@router.get("/skus/{sku_id}/inventory", response_model=sc.InventoryOut,
            dependencies=[Depends(require_scope("read"))])
def sku_inventory(sku_id: str, request: Request,
                  principal: Principal = Depends(current_principal)) -> dict:
    with session_scope() as session:
        version = _data_version(session, request)
        row = _guard_sku(session, principal, sku_id, request)

        def produce():
            return {
                "sku_id": sku_id, "as_of": row.as_of.isoformat(),
                "warehouse_id": row.warehouse_id,
                "on_hand": row.on_hand, "locked": row.locked,
                "reserved_crm": row.reserved_crm, "order_hold": row.order_hold,
                "available": row.available, "data_version": version,
            }

        value, hit = cache.get_or_set(_cache_key(version, "inventory", sku_id), produce)
    value["cached"] = hit
    return value


@router.get("/skus/batch", response_model=sc.SkuBatchOut,
            dependencies=[Depends(require_scope("read"))])
def skus_batch(request: Request, sku_ids: str = Query("", description="逗号分隔，最多 50 个"),
               principal: Principal = Depends(current_principal)) -> dict:
    """批量取 SKU 指标（一次调用拿 N 个）。

    为什么要批量：Agent 的批量巡检/对比场景逐个调 `/skus/{id}/metrics` 会把一轮对话变成
    N 次往返。跨部门的 SKU 不会报错，而是被**剔除并在 `scope_dropped` 里标注**——
    批量接口报错会让调用方整批失败，反而更容易被当成"数据缺失"。
    """
    ids = [x.strip() for x in (sku_ids or "").split(",") if x.strip()]
    if not ids:
        raise HTTPException(400, "sku_ids 必填（逗号分隔）")
    if len(ids) > 50:
        raise HTTPException(400, "单次最多 50 个 sku_id（先聚合后明细）")
    scope = department_scope(principal)
    with session_scope() as session:
        version = _data_version(session, request)
        rows = session.scalars(
            select(MartSkuMetrics).where(MartSkuMetrics.sku_id.in_(ids))
            .order_by(MartSkuMetrics.as_of.desc())
        ).all()
        found: dict[str, MartSkuMetrics] = {}
        for row in rows:
            found.setdefault(row.sku_id, row)      # 同 SKU 多天只取最新一天
        items, dropped = [], 0
        for sku_id in ids:
            row = found.get(sku_id)
            if row is None:
                continue
            if scope is not None and not principal.department_allowed(row.category):
                dropped += 1
                continue
            items.append(_metric_payload(row, version))
        missing = [sku_id for sku_id in ids if sku_id not in found]
    return {"items": items, "count": len(items), "missing": missing,
            "scope": list(scope) if scope is not None else None,
            "scope_filtered": scope is not None, "scope_dropped": dropped,
            "data_version": version}


@router.get("/skus/{sku_id}/sales", response_model=sc.SalesOut,
            dependencies=[Depends(require_scope("read"))])
def sku_sales(sku_id: str, request: Request, days: int = Query(7, ge=1, le=30),
              principal: Principal = Depends(current_principal)) -> dict:
    with session_scope() as session:
        version = _data_version(session, request)
        row = _guard_sku(session, principal, sku_id, request)

        def produce():
            fct = session.scalar(
                select(FctSalesDaily).where(FctSalesDaily.sku_id == sku_id)
                .order_by(FctSalesDaily.as_of.desc())
            )
            return {
                "sku_id": sku_id, "as_of": row.as_of.isoformat(),
                "days": days,
                "qty_7d": fct.qty_7d if fct else 0,
                "qty_30d": row.qty_30d,
                "daily_avg_7d": row.daily_avg_7d,
                "daily_avg_30d": row.daily_avg_30d,
                "sold_30d_flag": row.sold_30d_flag,
                "data_version": version,
            }

        value, hit = cache.get_or_set(_cache_key(version, "sales", f"{sku_id}:{days}"), produce)
    value["cached"] = hit
    return value


@router.get("/skus/{sku_id}/in-transit", response_model=sc.InTransitOut,
            dependencies=[Depends(require_scope("read"))])
def sku_in_transit(sku_id: str, request: Request,
                   principal: Principal = Depends(current_principal)) -> dict:
    with session_scope() as session:
        version = _data_version(session, request)
        _guard_sku(session, principal, sku_id, request)

        def produce():
            today = dt.date.today()
            # 直接读 ERP 原始在途单（带采购单号/状态），而不是只看聚合表：
            # 采购单号是「这张单凭什么说在途」的证据，聚合表已经把它丢了。
            rows = session.scalars(
                select(RawErpInTransit).where(RawErpInTransit.sku_id == sku_id)
                .order_by(RawErpInTransit.eta)
            ).all()
            items = [
                {"qty": r.qty, "eta": r.eta.isoformat(),
                 "days_to_arrive": max(0, (r.eta - today).days),
                 "po_no": r.po_no, "status": r.status, "supplier_id": r.supplier_id}
                for r in rows
            ]
            return {
                "sku_id": sku_id, "items": items,
                "total": sum(r.qty for r in rows),
                "in_transit_7d": sum(r.qty for r in rows if (r.eta - today).days <= 7),
                "as_of": today.isoformat(),
                "data_version": version,
            }

        value, hit = cache.get_or_set(_cache_key(version, "in_transit", sku_id), produce)
    value["cached"] = hit
    return value


@router.get("/skus/{sku_id}/reservations", response_model=sc.ReservationsOut,
            dependencies=[Depends(require_scope("read"))])
def sku_reservations(sku_id: str, request: Request,
                     principal: Principal = Depends(current_principal)) -> dict:
    with session_scope() as session:
        version = _data_version(session, request)
        _guard_sku(session, principal, sku_id, request)

        def produce():
            rows = session.execute(
                select(RawCrmReservation, RawCrmCustomer)
                .join(RawCrmCustomer, RawCrmReservation.customer_id == RawCrmCustomer.customer_id)
                .where(RawCrmReservation.sku_id == sku_id,
                       RawCrmReservation.status == "待发货")
                .order_by(RawCrmReservation.expected_ship)
            ).all()
            items = [
                {
                    "reservation_id": r.RawCrmReservation.reservation_id,
                    "qty": r.RawCrmReservation.qty,
                    "customer": {
                        "customer_id": r.RawCrmCustomer.customer_id,
                        "name": r.RawCrmCustomer.name_masked,
                        "phone": r.RawCrmCustomer.phone_masked,
                        "tier": r.RawCrmCustomer.tier,
                        "region": r.RawCrmCustomer.region,
                    },
                    "reserved_at": r.RawCrmReservation.reserved_at.isoformat(timespec="minutes"),
                    "expected_ship": r.RawCrmReservation.expected_ship.isoformat(),
                }
                for r in rows
            ]
            return {"sku_id": sku_id, "items": items, "total_qty": sum(i["qty"] for i in items),
                    "data_version": version}

        value, hit = cache.get_or_set(_cache_key(version, "reservations", sku_id), produce)
    value["cached"] = hit
    return value


@router.get("/skus/{sku_id}/orders", response_model=sc.OrdersOut,
            dependencies=[Depends(require_scope("read"))])
def sku_orders(sku_id: str, request: Request, days: int = Query(7, ge=1, le=30),
               principal: Principal = Depends(current_principal)) -> dict:
    with session_scope() as session:
        version = _data_version(session, request)
        _guard_sku(session, principal, sku_id, request)

        def produce():
            cutoff = dt.date.today() - dt.timedelta(days=days)
            rows = session.scalars(
                select(RawOrder).where(RawOrder.sku_id == sku_id, RawOrder.order_date >= cutoff)
                .order_by(RawOrder.order_date.desc()).limit(30)
            ).all()
            return {
                "sku_id": sku_id, "days": days,
                "items": [
                    {"order_id": r.order_id, "qty": r.qty, "amount": r.amount,
                     "order_date": r.order_date.isoformat(), "status": r.status,
                     "receiver": r.receiver_masked}
                    for r in rows
                ],
                "total_qty": sum(r.qty for r in rows),
                "pending_qty": sum(r.qty for r in rows if r.status == "待发货"),
                "data_version": version,
            }

        value, hit = cache.get_or_set(_cache_key(version, "orders", f"{sku_id}:{days}"), produce)
    value["cached"] = hit
    return value


@router.get("/skus/{sku_id}/metrics", response_model=sc.SkuMetricsOut,
            dependencies=[Depends(require_scope("read"))])
def sku_metrics(sku_id: str, request: Request,
                principal: Principal = Depends(current_principal)) -> dict:
    with session_scope() as session:
        version = _data_version(session, request)
        row = _guard_sku(session, principal, sku_id, request)

        def produce():
            return _metric_payload(row, version)

        value, hit = cache.get_or_set(_cache_key(version, "metrics", sku_id), produce)
    value["cached"] = hit
    return value


@router.get("/skus/{sku_id}/sell-through", response_model=sc.SellThroughOut,
            dependencies=[Depends(require_scope("read"))])
def sku_sell_through(sku_id: str, request: Request,
                     principal: Principal = Depends(current_principal)) -> dict:
    with session_scope() as session:
        version = _data_version(session, request)
        row = _guard_sku(session, principal, sku_id, request)

        def produce():
            return {
                "sku_id": sku_id, "as_of": row.as_of.isoformat(),
                "available": row.available,
                "in_transit_7d": row.in_transit_7d,
                "daily_avg_7d": row.daily_avg_7d,
                "sell_through_days": row.sell_through_days,
                "stockout_risk": row.stockout_risk,
                "formula": "预计售罄天数 = (可用库存 + 7天内到货在途) ÷ 日均销量(近7天)",
                "data_version": version,
            }

        value, hit = cache.get_or_set(_cache_key(version, "sell_through", sku_id), produce)
    value["cached"] = hit
    return value


# ---------------------------------------------------------------- 订单域：详情 / 物流 / 退货风险
def _order_dict(row: RawOrder) -> dict:
    return {
        "order_id": row.order_id, "sku_id": row.sku_id, "qty": row.qty,
        "amount": row.amount, "order_date": row.order_date.isoformat(),
        "status": row.status, "receiver": row.receiver_masked,
    }


def _logistics_dict(row: RawLogistics | None) -> dict:
    if row is None:
        return {}
    return {
        "carrier": row.carrier, "tracking_no": row.tracking_no, "status": row.status,
        "eta": row.eta.isoformat() if row.eta else "",
        "updated_at": row.updated_at.isoformat(timespec="minutes") if row.updated_at else "",
    }


def _guard_order(session, principal: Principal, order_id: str, request: Request):  # noqa: ANN001
    """订单 → SKU → 部门（订单是业务边界，越权要可诊断）。"""
    order = session.get(RawOrder, order_id)
    if order is None:
        raise HTTPException(404, f"订单不存在：{order_id}")
    mart = session.scalar(
        select(MartSkuMetrics).where(MartSkuMetrics.sku_id == order.sku_id)
        .order_by(MartSkuMetrics.as_of.desc())
    )
    ensure_department(principal, mart.category if mart else None, request, sku_id=order.sku_id)
    return order


@router.get("/orders/{order_id}", response_model=sc.OrderDetailOut,
            dependencies=[Depends(require_scope("read"))])
def order_detail(order_id: str, request: Request,
                 principal: Principal = Depends(current_principal)) -> dict:
    """订单详情：交易事实 + 物流 + 售后工单摘要（「这单到哪了/能不能退」一次答完）。"""
    with session_scope() as session:
        order = _guard_order(session, principal, order_id, request)
        logistics = session.get(RawLogistics, order_id)
        tickets = session.scalars(
            select(RawAfterSalesTicket).where(RawAfterSalesTicket.order_id == order_id)
            .order_by(RawAfterSalesTicket.created_at.desc())
        ).all()
        return {
            "order_id": order_id, "found": True,
            "order": _order_dict(order),
            "logistics": _logistics_dict(logistics),
            "after_sales": [
                {"ticket_id": t.ticket_id, "status": t.status, "reason": t.reason, "qty": t.qty}
                for t in tickets
            ],
        }


@router.get("/orders/{order_id}/logistics", response_model=sc.LogisticsOut,
            dependencies=[Depends(require_scope("read"))])
def order_logistics(order_id: str, request: Request,
                    principal: Principal = Depends(current_principal)) -> dict:
    with session_scope() as session:
        _guard_order(session, principal, order_id, request)
        row = session.get(RawLogistics, order_id)
        data = _logistics_dict(row)
        if not data:
            return {"order_id": order_id, "found": True, "logistics": {},
                    "note": "该订单暂无物流单（可能未发货）"}
        return {"order_id": order_id, "found": True, "logistics": data}


@router.get("/skus/{sku_id}/return-risk", response_model=sc.ReturnRiskOut,
            dependencies=[Depends(require_scope("read"))])
def sku_return_risk(sku_id: str, request: Request, days: int = Query(30, ge=1, le=90),
                    principal: Principal = Depends(current_principal)) -> dict:
    """退货风险：退货率 + 近期订单量 + 售后工单量（判断该款值不值得继续推）。"""
    with session_scope() as session:
        row = _guard_sku(session, principal, sku_id, request)
        cutoff = dt.date.today() - dt.timedelta(days=days)
        orders = int(session.scalar(
            select(func.count()).select_from(RawOrder)
            .where(RawOrder.sku_id == sku_id, RawOrder.order_date >= cutoff)
        ) or 0)
        tickets = int(session.scalar(
            select(func.count()).select_from(RawAfterSalesTicket)
            .where(RawAfterSalesTicket.sku_id == sku_id)
        ) or 0)
        return_rate = float(getattr(row, "return_rate", 0.0) or 0.0)
        return {
            "sku_id": sku_id, "found": True,
            "return_rate": round(return_rate, 4),
            "orders_last_days": orders, "after_sales_tickets": tickets,
            "risk": "高" if return_rate >= 0.18 else "低",
            "note": "退货率 ≥ 18% 视为高风险（与量化筛选口径一致）",
        }


# ---------------------------------------------------------------- 售后工单（幂等 + 条件更新）
_AFTER_SALES_REASONS = ("尺码不合适", "质量问题", "7天无理由", "发错货", "物流破损")


@router.post("/after-sales", response_model=sc.AfterSalesOut,
             dependencies=[Depends(require_scope("write"))])
def create_after_sales(payload: dict, request: Request,
                       principal: Principal = Depends(current_principal)) -> dict:
    """发起售后工单草稿（幂等：同订单+原因+数量只建一单，返回 pending_confirmation）。"""
    body = payload or {}
    order_id = str(body.get("order_id") or "")
    reason = str(body.get("reason") or "").strip()
    if not order_id:
        raise HTTPException(400, "order_id 必填")
    if reason not in _AFTER_SALES_REASONS:
        raise HTTPException(400, f"不支持的售后原因：{reason}；可选：{'、'.join(_AFTER_SALES_REASONS)}")
    with session_scope() as session:
        order = _guard_order(session, principal, order_id, request)
        qty = int(body.get("qty") or order.qty or 0)
        if qty <= 0:
            raise HTTPException(400, "售后数量必须大于 0")
        if qty > int(order.qty or 0):
            raise HTTPException(400, f"售后数量 {qty} 超过订单购买数量 {order.qty}")
        sku_id = order.sku_id
        key = hashlib.sha1(f"{order_id}|{reason}|{qty}".encode("utf-8")).hexdigest() \
            if not body.get("idempotency_key") else str(body["idempotency_key"])[:160]

    try:
        with session_scope() as session:
            existing = session.scalar(
                select(RawAfterSalesTicket).where(RawAfterSalesTicket.idempotency_key == key)
            )
            if existing is not None:
                return {
                    "ticket_id": existing.ticket_id, "order_id": existing.order_id,
                    "sku_id": existing.sku_id, "reason": existing.reason, "qty": existing.qty,
                    "status": existing.status, "deduped": True,
                    "note": "幂等命中：该售后工单已存在，未重复创建",
                }
            ticket = RawAfterSalesTicket(
                ticket_id=str(uuid.uuid4()), order_id=order_id, sku_id=sku_id,
                reason=reason, qty=qty, status="pending_confirmation",
                idempotency_key=key, created_at=dt.datetime.now(),
            )
            session.add(ticket)
            session.flush()
            return {
                "ticket_id": ticket.ticket_id, "order_id": order_id, "sku_id": sku_id,
                "reason": reason, "qty": qty, "status": ticket.status, "deduped": False,
                "note": "已生成售后工单草稿，等待人工确认（不会自动退款）",
            }
    except IntegrityError:
        with session_scope() as session:
            existing = session.scalar(
                select(RawAfterSalesTicket).where(RawAfterSalesTicket.idempotency_key == key)
            )
            if existing is None:
                raise
            return {
                "ticket_id": existing.ticket_id, "order_id": existing.order_id,
                "sku_id": existing.sku_id, "reason": existing.reason, "qty": existing.qty,
                "status": existing.status, "deduped": True,
                "note": "幂等命中：该售后工单已存在，未重复创建",
            }


@router.get("/after-sales", response_model=sc.AfterSalesListOut,
            dependencies=[Depends(require_scope("read"))])
def list_after_sales(request: Request, status: str = Query("pending"),
                     limit: int = Query(50, ge=1, le=200),
                     principal: Principal = Depends(current_principal)) -> dict:
    """售后工单列表（**按调用方部门过滤**，被滤掉多少条显式标注）。"""
    scope = department_scope(principal)
    with session_scope() as session:
        stmt = select(RawAfterSalesTicket).order_by(RawAfterSalesTicket.created_at.desc()).limit(limit)
        if status == "pending":
            stmt = stmt.where(RawAfterSalesTicket.status == "pending_confirmation")
        elif status != "all":
            stmt = stmt.where(RawAfterSalesTicket.status == status)
        rows = session.scalars(stmt).all()
        cats = {}
        if rows:
            for m in session.scalars(select(MartSkuMetrics).where(
                    MartSkuMetrics.sku_id.in_({r.sku_id for r in rows}))):
                cats[m.sku_id] = m.category
        items = []
        dropped = 0
        for r in rows:
            category = cats.get(r.sku_id)
            if scope is not None and not principal.department_allowed(category):
                dropped += 1
                continue
            items.append({
                "ticket_id": r.ticket_id, "order_id": r.order_id, "sku_id": r.sku_id,
                "reason": r.reason, "qty": r.qty, "status": r.status,
                "department": department_of(category),
                "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else None,
                "approver": r.approver,
                "decided_at": r.decided_at.isoformat(timespec="seconds") if r.decided_at else None,
            })
    return {"status": status, "items": items, "count": len(items),
            "scope": list(scope) if scope is not None else None,
            "scope_filtered": scope is not None, "scope_dropped": dropped}


def _decide_after_sales(ticket_id: str, *, approve: bool, payload: dict | None,
                        principal: Principal, request: Request) -> dict:
    """售后审批：单条条件 UPDATE（两个 Agent 同时确认只有一个成功，另一个 409）。"""
    now = dt.datetime.now()
    actor = ((payload or {}).get("approver") or principal.agent_id)[:32]
    with session_scope() as session:
        ticket = session.get(RawAfterSalesTicket, ticket_id)
        if ticket is None:
            raise HTTPException(404, "售后工单不存在")
        mart = session.scalar(
            select(MartSkuMetrics).where(MartSkuMetrics.sku_id == ticket.sku_id)
            .order_by(MartSkuMetrics.as_of.desc())
        )
        ensure_department(principal, mart.category if mart else None, request, sku_id=ticket.sku_id)
        previous = ticket.status

    target = "approved" if approve else "rejected"
    values: dict = {"status": target, "approver": actor, "decided_at": now}
    if not approve:
        note = ((payload or {}).get("reason") or "").strip()
        if note:
            values["note"] = note[:250]
    with session_scope() as session:
        result = session.execute(
            update(RawAfterSalesTicket)
            .where(RawAfterSalesTicket.ticket_id == ticket_id,
                   RawAfterSalesTicket.status == "pending_confirmation")
            .values(**values)
        )
        if result.rowcount == 0:
            row = session.get(RawAfterSalesTicket, ticket_id)
            if row is None:
                raise HTTPException(404, "售后工单不存在")
            raise HTTPException(
                409, f"工单状态为 {row.status}（已由 {row.approver or '他人'} 处理），不可重复操作")
        return {"ticket_id": ticket_id, "status": target, "approver": actor,
                "decided_at": now.isoformat(timespec="seconds"),
                "caller": principal.agent_id, "previous_status": previous}


@router.post("/after-sales/{ticket_id}/approve", response_model=sc.AfterSalesDecisionOut,
             dependencies=[Depends(require_scope("approve"))])
def approve_after_sales(ticket_id: str, request: Request, payload: dict | None = None,
                        principal: Principal = Depends(current_principal)) -> dict:
    return _decide_after_sales(ticket_id, approve=True, payload=payload,
                               principal=principal, request=request)


@router.post("/after-sales/{ticket_id}/reject", response_model=sc.AfterSalesDecisionOut,
             dependencies=[Depends(require_scope("approve"))])
def reject_after_sales(ticket_id: str, request: Request, payload: dict | None = None,
                       principal: Principal = Depends(current_principal)) -> dict:
    return _decide_after_sales(ticket_id, approve=False, payload=payload,
                               principal=principal, request=request)


# ---------------------------------------------------------------- 供应链：供应商 / 调拨 / 大促窗口
@router.get("/suppliers/{supplier_id}", response_model=sc.SupplierOut,
            dependencies=[Depends(require_scope("read"))])
def supplier_info(supplier_id: str) -> dict:
    with session_scope() as session:
        row = session.get(RawSupplier, supplier_id)
        if row is None:
            return {"supplier_id": supplier_id, "found": False}
        return {
            "supplier_id": row.supplier_id, "found": True, "name": row.name,
            "lead_time_days": row.lead_time_days, "moq": row.moq,
            "on_time_rate": row.on_time_rate,
        }


@router.get("/skus/{sku_id}/transfers", response_model=sc.TransfersOut,
            dependencies=[Depends(require_scope("read"))])
def sku_transfers(sku_id: str, request: Request,
                  principal: Principal = Depends(current_principal)) -> dict:
    with session_scope() as session:
        _guard_sku(session, principal, sku_id, request)
        rows = session.scalars(
            select(RawTransferOrder).where(RawTransferOrder.sku_id == sku_id)
            .order_by(RawTransferOrder.eta)
        ).all()
        items = [
            {"transfer_no": r.transfer_no, "sku_id": r.sku_id,
             "from_warehouse_id": r.from_warehouse_id, "to_warehouse_id": r.to_warehouse_id,
             "qty": r.qty, "eta": r.eta.isoformat(), "status": r.status}
            for r in rows
        ]
    return {"sku_id": sku_id, "items": items, "count": len(items)}


@router.get("/platform-activities", response_model=sc.ActivitiesOut,
            dependencies=[Depends(require_scope("read"))])
def platform_activities(platform: str = Query(""), days: int = Query(45, ge=1, le=180)) -> dict:
    """平台大促窗口：回答「要不要提前备货 / 现在报活动还来不来得及」。"""
    today = dt.date.today()
    end = today + dt.timedelta(days=days)
    with session_scope() as session:
        stmt = (select(RawPlatformActivity)
                .where(RawPlatformActivity.end_date >= today,
                       RawPlatformActivity.start_date <= end)
                .order_by(RawPlatformActivity.start_date))
        if platform:
            stmt = stmt.where(RawPlatformActivity.platform.in_([platform, "全部"]))
        rows = session.scalars(stmt).all()
        items = [
            {"name": r.name, "platform": r.platform,
             "start_date": r.start_date.isoformat(), "end_date": r.end_date.isoformat(),
             "days_to_start": (r.start_date - today).days,
             "category": r.category, "note": r.note}
            for r in rows
        ]
    return {"platform": platform or "全部", "items": items, "count": len(items)}


# ---------------------------------------------------------------- 竞品行情（原拍立淘 MCP）
def _competitor_rows(session, style_id: str) -> list[RawCompetitorListing]:  # noqa: ANN001
    return list(session.scalars(
        select(RawCompetitorListing).where(RawCompetitorListing.style_id == style_id)
        .order_by(RawCompetitorListing.price)
    ))


@router.get("/styles/{style_id}/competitors", response_model=sc.CompetitorsOut,
            dependencies=[Depends(require_scope("read"))])
def style_competitors(style_id: str, request: Request,
                      principal: Principal = Depends(current_principal)) -> dict:
    """同款竞品：店铺、价格、近 30/7 天销量、评分、趋势。"""
    with session_scope() as session:
        _guard_style(session, principal, style_id, request)
        rows = _competitor_rows(session, style_id)
        if not rows:
            return {"style_id": style_id, "found": False, "competitors": [],
                    "note": f"未检索到同款竞品（style_id={style_id}），可能为新款或竞品未上架"}
        prices = [float(r.price) for r in rows]
        sales7 = sum(int(r.sales_7d or 0) for r in rows)
        sales30 = sum(int(r.sales_30d or 0) for r in rows)
        trend = (sales7 / max(sales30 / 30 * 7, 1)) - 1.0
        return {
            "style_id": style_id, "found": True,
            "competitors": [
                {"shop_name": r.shop_name, "platform": r.platform, "price": r.price,
                 "sales_30d": r.sales_30d, "sales_7d": r.sales_7d, "rating": r.rating,
                 "trend_pct": r.trend_pct,
                 "snapshot_ts": r.snapshot_ts.isoformat(timespec="minutes")}
                for r in rows
            ],
            "price_min": min(prices), "price_max": max(prices),
            "price_avg": round(sum(prices) / len(prices), 2),
            "heat_index": round(min(100.0, sales7 / 10.0), 1),
            "trend_pct": round(trend, 4),
        }


@router.get("/styles/{style_id}/price-band", response_model=sc.PriceBandOut,
            dependencies=[Depends(require_scope("read"))])
def style_price_band(style_id: str, request: Request,
                     principal: Principal = Depends(current_principal)) -> dict:
    with session_scope() as session:
        _guard_style(session, principal, style_id, request)
        rows = _competitor_rows(session, style_id)
        if not rows:
            return {"style_id": style_id, "found": False}
        prices = sorted(float(r.price) for r in rows)
        return {
            "style_id": style_id, "found": True, "count": len(prices),
            "price_min": prices[0], "price_max": prices[-1],
            "price_median": prices[len(prices) // 2],
            "price_avg": round(sum(prices) / len(prices), 2),
        }


@router.get("/styles/{style_id}/heat", response_model=sc.StyleHeatOut,
            dependencies=[Depends(require_scope("read"))])
def style_heat(style_id: str, request: Request,
               principal: Principal = Depends(current_principal)) -> dict:
    with session_scope() as session:
        _guard_style(session, principal, style_id, request)
        rows = _competitor_rows(session, style_id)
        if not rows:
            return {"style_id": style_id, "found": False}
        sales7 = sum(int(r.sales_7d or 0) for r in rows)
        trend = sum(float(r.trend_pct or 0) for r in rows) / len(rows)
        return {
            "style_id": style_id, "found": True, "competitor_sales_7d": sales7,
            "avg_trend_pct": round(trend, 4), "heat_index": round(min(100.0, sales7 / 10.0), 1),
        }


# ---------------------------------------------------------------- 款级 / 尺码结构（服装口径）
@router.get("/styles/{style_id}/metrics", response_model=sc.StyleMetricsOut,
            dependencies=[Depends(require_scope("read"))])
def style_metrics(style_id: str, request: Request,
                  principal: Principal = Depends(current_principal)) -> dict:
    """款级指标：生命周期 + 尺码曲线 + 齐码/断码（SKU 级指标答不了的问题）。"""
    with session_scope() as session:
        version = _data_version(session, request)
        row = _guard_style(session, principal, style_id, request)

        def produce():
            payload = _style_payload(row, version)
            payload["size_breakdown"] = _size_breakdown(session, style_id)
            return payload

        value, hit = cache.get_or_set(_cache_key(version, "style_metrics", style_id), produce)
    value["cached"] = hit
    return value


def _size_breakdown(session, style_id: str) -> list[dict]:  # noqa: ANN001
    """款 × 色 × 码 明细：断码定位要到「哪个颜色的哪个码断了」这一层。"""
    rows = session.scalars(
        select(MartSkuMetrics).where(MartSkuMetrics.style_id == style_id)
        .order_by(MartSkuMetrics.color, MartSkuMetrics.sku_id)
    ).all()
    return [
        {"sku_id": r.sku_id, "color": r.color, "size": r.size, "available": r.available,
         "on_hand": r.on_hand, "price": r.price, "warehouse_id": r.warehouse_id,
         "qty_30d": r.qty_30d, "size_share": r.size_share,
         "main_size_flag": r.main_size_flag, "size_gap_flag": r.size_gap_flag}
        for r in rows
    ]


@router.get("/metrics/size-curve", response_model=sc.SizeCurveOut,
            dependencies=[Depends(require_scope("read"))])
def size_curve(style_id: str, request: Request,
               principal: Principal = Depends(current_principal)) -> dict:
    """尺码曲线：各码销量占比，识别主销码（≥15%）与长尾码。"""
    with session_scope() as session:
        version = _data_version(session, request)
        row = _guard_style(session, principal, style_id, request)
        value, hit = cache.get_or_set(
            _cache_key(version, "size_curve", style_id),
            lambda: {"style_id": style_id, "items": row.size_curve or [],
                     "data_version": version,
                     "note": "size_share = 该码近30天销量 ÷ 同款近30天总销量；≥15% 为主销码"},
        )
    value["cached"] = hit
    return value


@router.get("/metrics/size-gap", response_model=sc.SizeGapOut,
            dependencies=[Depends(require_scope("read"))])
def size_gap(request: Request, department: str | None = None,
             limit: int = Query(20, ge=1, le=100),
             principal: Principal = Depends(current_principal)) -> dict:
    """断码款清单：主销码缺货的款（含缺哪些码、缺几个）。"""
    scope = department_scope(principal)
    if department and scope is not None and department not in scope:
        request.state.audit_decision = "denied_department"
        request.state.audit_detail = f"department={department}"
        raise HTTPException(403, f"调用方 {principal.agent_id} 只能查询 {list(scope)} 部门的指标")
    with session_scope() as session:
        version = _data_version(session, request)
        stmt = select(MartStyleMetrics).where(MartStyleMetrics.broken_size_count > 0)
        if department:
            stmt = stmt.where(MartStyleMetrics.department == department)
        elif scope is not None:
            stmt = stmt.where(MartStyleMetrics.department.in_(list(scope)))
        rows = session.scalars(
            stmt.order_by(MartStyleMetrics.size_gap_rate.desc())
            .order_by(MartStyleMetrics.total_available.desc()).limit(limit)
        ).all()
        items = [
            {"style_id": r.style_id, "style_name": r.style_name, "department": r.department,
             "category": r.category, "lifecycle_stage": r.lifecycle_stage,
             "broken_size_count": r.broken_size_count, "size_gap_rate": r.size_gap_rate,
             "missing_sizes": [c["size"] for c in (r.size_curve or []) if c.get("gap")],
             "listed_sizes": [c["size"] for c in (r.size_curve or []) if c.get("available", 0) > 0],
             "size_count": r.size_count, "total_available": r.total_available,
             "qty_30d": r.qty_30d}
            for r in rows
        ]
        totals = session.execute(
            select(func.count(), func.sum(MartStyleMetrics.full_size_flag))
            .where(*_apparel_scope_filters(department, scope))
        ).one()
        total_styles, full_styles = int(totals[0] or 0), int(totals[1] or 0)
    return {
        "department": department, "items": items, "count": len(items),
        "full_size_rate": round(full_styles / total_styles, 4) if total_styles else 0.0,
        "broken_size_rate": round(1 - full_styles / total_styles, 4) if total_styles else 0.0,
        "formula": {
            "full_size_rate": "齐码率 = 全码可用>0 的款数 ÷ 在售款数",
            "size_gap_rate": "断码率 = 主销码可用=0 的款数 ÷ 在售款数",
            "main_size": "主销码 = 该码销量占同款 ≥15%",
        },
        "data_version": version,
    }


def _apparel_scope_filters(department: str | None, scope: tuple[str, ...] | None):  # noqa: ANN001
    """款级聚合的部门过滤条件（与单个端点保持一致，避免两个口径）。"""
    if department:
        return [MartStyleMetrics.department == department]
    if scope is not None:
        return [MartStyleMetrics.department.in_(list(scope))]
    return []


@router.get("/metrics/apparel-overview", response_model=sc.ApparelOverviewOut,
            dependencies=[Depends(require_scope("read"))])
def apparel_overview(request: Request, department: str | None = None,
                     principal: Principal = Depends(current_principal)) -> dict:
    """部门级服装经营概览：齐码率 / 断码率 / 周转 / 售罄 / 生命周期分布。"""
    scope = department_scope(principal)
    if department and scope is not None and department not in scope:
        request.state.audit_decision = "denied_department"
        request.state.audit_detail = f"department={department}"
        raise HTTPException(403, f"调用方 {principal.agent_id} 只能查询 {list(scope)} 部门的指标")
    with session_scope() as session:
        version = _data_version(session, request)
        filters = _apparel_scope_filters(department, scope)

        def produce():
            rows = session.scalars(select(MartStyleMetrics).where(*filters)).all()
            styles = len(rows)
            full = sum(int(r.full_size_flag or 0) for r in rows)
            broken = sum(1 for r in rows if int(r.broken_size_count or 0) > 0)
            turnovers = [float(r.turnover_days) for r in rows if r.turnover_days is not None]
            sell = [float(r.sell_through_rate or 0) for r in rows]
            lifecycle: dict[str, int] = {}
            for r in rows:
                key = r.lifecycle_stage or "未标注"
                lifecycle[key] = lifecycle.get(key, 0) + 1
            return {
                "department": department, "style_count": styles,
                "full_size_styles": full, "broken_size_styles": broken,
                "full_size_rate": round(full / styles, 4) if styles else 0.0,
                "broken_size_rate": round(broken / styles, 4) if styles else 0.0,
                "avg_turnover_days": round(sum(turnovers) / len(turnovers), 2) if turnovers else None,
                "avg_sell_through_rate": round(sum(sell) / len(sell), 4) if sell else 0.0,
                "lifecycle_breakdown": lifecycle,
                "formula": {
                    "full_size_rate": "齐码率 = 全码可用>0 的款数 ÷ 在售款数",
                    "broken_size_rate": "断码率 = 主销码可用=0 的款数 ÷ 在售款数",
                    "turnover_days": "库存周转天数 = 可用库存 ÷ 日均销量(近30天)",
                    "sell_through_rate": "售罄率 = 近30天销量 ÷ (近30天销量 + 当前可用库存)",
                },
                "data_version": version,
            }

        value, hit = cache.get_or_set(
            _cache_key(version, "apparel_overview",
                       f"{department or 'all'}|{_scope_tag(principal)}"), produce)
    value["cached"] = hit
    return value


@router.get("/skus/{sku_id}/sales-series", response_model=sc.SalesSeriesOut,
            dependencies=[Depends(require_scope("read"))])
def sku_sales_series(sku_id: str, request: Request, days: int = Query(7, ge=1, le=30),
                     principal: Principal = Depends(current_principal)) -> dict:
    """日销量序列（补齐零销量日）：趋势/回归类分析必须拿到连续窗口。"""
    with session_scope() as session:
        _guard_sku(session, principal, sku_id, request)
        today = dt.date.today()
        cutoff = today - dt.timedelta(days=days - 1)
        rows = session.execute(
            select(RawOrder.order_date, func.sum(RawOrder.qty))
            .where(RawOrder.sku_id == sku_id, RawOrder.order_date >= cutoff)
            .group_by(RawOrder.order_date)
        ).all()
        by_day = {d.isoformat(): int(q or 0) for d, q in rows}
        cursor = cutoff
        while cursor <= today:
            by_day.setdefault(cursor.isoformat(), 0)
            cursor += dt.timedelta(days=1)
        daily = [{"date": d, "qty": q} for d, q in sorted(by_day.items())]
    return {"sku_id": sku_id, "days": days, "daily": daily,
            "note": "零销量日已补齐；小时级销量不在中台口径内（需按需接入流量明细）"}


# ---------------------------------------------------------------- 聚合指标（按作用域）
@router.get("/metrics/sales-rate", response_model=sc.SalesRateOut,
            dependencies=[Depends(require_scope("read"))])
def sales_rate(request: Request, department: str | None = None,
               principal: Principal = Depends(current_principal)) -> dict:
    """动销率。部门作用域下沉后：请求别的部门 → 403；不传 → 按调用方可见范围聚合。"""
    scope = department_scope(principal)
    if department and scope is not None and department not in scope:
        request.state.audit_decision = "denied_department"
        request.state.audit_detail = f"department={department}"
        raise HTTPException(
            403, f"调用方 {principal.agent_id} 只能查询 {list(scope)} 部门的指标")
    with session_scope() as session:
        version = _data_version(session, request)
        effective = department or (None if scope is None else list(scope))
        cache_args = f"{department or 'all'}|{_scope_tag(principal)}"

        def produce():
            stmt = select(
                func.count().filter(MartSkuMetrics.sold_30d_flag == 1),
                func.count(),
            )
            if department:
                stmt = stmt.where(MartSkuMetrics.department == department)
            elif scope is not None:
                stmt = stmt.where(MartSkuMetrics.department.in_(list(scope)))
            sold, total = session.execute(stmt).one()
            sold, total = int(sold or 0), int(total or 0)
            return {
                "department": department or "全部",
                "scope": list(scope) if scope is not None else None,
                "effective_departments": ([department] if department
                                          else (list(effective) if effective else None)),
                "sold_30d_skus": sold, "total_skus": total,
                "sales_rate": round(sold / total, 4) if total else 0.0,
                "formula": "动销率 = 近30天有销量SKU数 ÷ 有货SKU总数",
                "data_version": version,
            }

        value, hit = cache.get_or_set(_cache_key(version, "sales_rate", cache_args), produce)
    value["cached"] = hit
    return value


# ---------------------------------------------------------------- 动作：补货草稿 / 审批 / 调拨
def _approval_payload(row: ApprovalRequest, *, deduped: bool) -> dict:
    return {
        "approval_id": row.id,
        "status": row.status,
        "sku_id": row.sku_id,
        "qty": row.qty,
        "amount": row.amount,
        "po_no": row.erp_order_id,
        "deduped": deduped,
        "high_risk": row.status == "pending_confirmation",
        "requested_by": row.requested_by,
        "message": ("幂等命中：该补货单已存在，未重复创建" if deduped
                    else ("已生成补货草稿单，等待人工确认" if row.status == "pending_confirmation"
                          else "已自动创建补货草稿单")),
    }


@router.post("/erp/replenish-orders", response_model=sc.ApprovalOut,
             dependencies=[Depends(require_scope("write"))])
def create_replenish_order(payload: dict, request: Request,
                           principal: Principal = Depends(current_principal)) -> dict:
    sku_id = (payload or {}).get("sku_id")
    qty = int((payload or {}).get("qty") or 0)
    reason = (payload or {}).get("reason") or ""
    idem_key = (payload or {}).get("idempotency_key") or None
    if not sku_id or qty <= 0:
        raise HTTPException(400, "sku_id 与 qty（>0）必填")

    # ① 只读事务：拿事实 + 部门校验（不把读写在同一个事务里，避免快照冲突）
    with session_scope() as session:
        row = _guard_sku(session, principal, sku_id, request)
        price = float(row.price or 0)

    amount = round(qty * price, 2)
    high_risk = qty >= REPLENISH_QTY_THRESHOLD or amount >= REPLENISH_AMOUNT_THRESHOLD
    status = "pending_confirmation" if high_risk else "auto_approved"
    po_no = f"PO{dt.datetime.now().strftime('%y%m%d%H%M')}{uuid.uuid4().hex[:4].upper()}"
    # 业务归属（requested_by）是 Agent 侧登录用户的**声明式**信息；可信的调用方身份
    # 记录在审计行与 X-Agent-Id 响应头里（见 api/audit.py），两者分开、互不冒充。
    actor = ((payload or {}).get("requested_by") or principal.agent_id)[:32]
    approval = ApprovalRequest(
        id=str(uuid.uuid4()), type="replenish", sku_id=sku_id, qty=qty, amount=amount,
        reason=reason[:250], status=status, requested_by=actor,
        created_at=dt.datetime.now(), erp_order_id=po_no, idempotency_key=idem_key,
    )
    erp = ErpReplenishOrder(
        po_no=po_no, sku_id=sku_id, qty=qty, amount=amount,
        status="draft" if high_risk else "approved",
        approval_id=approval.id, created_at=dt.datetime.now(),
        approved_at=None if high_risk else dt.datetime.now(),
    )

    # ② 写事务：幂等键有唯一约束，并发重试会撞约束 → 捕获后回查返回 dedupe（不再 500）
    try:
        with session_scope() as session:
            if idem_key:
                existing = session.scalar(
                    select(ApprovalRequest).where(ApprovalRequest.idempotency_key == idem_key)
                )
                if existing is not None:
                    return _approval_payload(existing, deduped=True)
            session.add(approval)
            session.add(erp)
            session.flush()
            # 在事务内组装返回体：出事务后 ORM 实例会 detach，读属性会报错
            result = _approval_payload(approval, deduped=False)
    except IntegrityError:
        with session_scope() as session:
            existing = session.scalar(
                select(ApprovalRequest).where(ApprovalRequest.idempotency_key == idem_key)
            )
            if existing is None:       # 不是幂等键冲突，原样上抛
                raise
            return _approval_payload(existing, deduped=True)

    result["thresholds"] = {"qty": REPLENISH_QTY_THRESHOLD, "amount": REPLENISH_AMOUNT_THRESHOLD}
    result["caller"] = principal.agent_id
    return result


@router.get("/approvals", response_model=sc.ApprovalsOut,
            dependencies=[Depends(require_scope("read"))])
def list_approvals(request: Request, status: str = Query("pending"),
                   within_hours: int | None = Query(None, ge=1, le=168),
                   principal: Principal = Depends(current_principal)) -> dict:
    """approval 列表（**按调用方部门过滤**，过滤了多少条显式标注，不静默少数据）。"""
    scope = department_scope(principal)
    with session_scope() as session:
        stmt = select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc()).limit(100)
        if status == "pending":
            stmt = stmt.where(ApprovalRequest.status == "pending_confirmation")
        elif status != "all":
            stmt = stmt.where(ApprovalRequest.status == status)
        if within_hours:
            cutoff = dt.datetime.now() - dt.timedelta(hours=within_hours)
            stmt = stmt.where(ApprovalRequest.created_at >= cutoff)
        rows = session.scalars(stmt).all()

        # 当前库存快照（mart 指标宽表）
        sku_ids = {r.sku_id for r in rows}
        mart_map = {}
        if sku_ids:
            for m in session.scalars(
                select(MartSkuMetrics).where(MartSkuMetrics.sku_id.in_(sku_ids))
            ):
                mart_map[m.sku_id] = m
        erp_map = {}
        po_nos = {r.erp_order_id for r in rows if r.erp_order_id}
        if po_nos:
            for po in session.scalars(
                select(ErpReplenishOrder).where(ErpReplenishOrder.po_no.in_(po_nos))
            ):
                erp_map[po.po_no] = po

        items = []
        dropped = 0
        for r in rows:
            mart = mart_map.get(r.sku_id)
            po = erp_map.get(r.erp_order_id) if r.erp_order_id else None
            category = mart.category if mart else None
            if scope is not None and not principal.department_allowed(category):
                dropped += 1
                continue
            items.append({
                "approval_id": r.id, "sku_id": r.sku_id, "qty": r.qty, "amount": r.amount,
                "reason": r.reason, "status": r.status, "requested_by": r.requested_by,
                "approver": r.approver,
                "decided_at": (r.decided_at.isoformat(timespec="seconds")
                               if r.decided_at else None),
                "po_no": r.erp_order_id,
                "erp_status": po.status if po else None,
                "created_at": r.created_at.isoformat(timespec="seconds"),
                "warehouse_id": mart.warehouse_id if mart else None,
                "department": department_of(category),
                "current_available": mart.available if mart else None,
                "current_on_hand": mart.on_hand if mart else None,
                "daily_avg_7d": mart.daily_avg_7d if mart else None,
            })
    return {"status": status, "window_hours": within_hours, "items": items,
            "count": len(items), "scope": list(scope) if scope is not None else None,
            "scope_filtered": scope is not None, "scope_dropped": dropped}


def _decide_approval(approval_id: str, *, approve: bool, payload: dict | None,
                     principal: Principal, request: Request) -> dict:
    """审批决策：**单条条件 UPDATE**（`WHERE status='pending_confirmation'`）。

    两个 Agent 同时确认同一单时，只有一个的 rowcount=1，另一个拿到 409 —— 早先是
    「读 → 判断 → 写」，两边都能通过检查，造成双写（approver/decided_at 被后写者覆盖，
    真实 ERP 场景就是重复下单）。
    """
    now = dt.datetime.now()
    # 业务归属（approver）同样取「声明式」的操作人，可信调用方身份见审计与 caller 字段
    actor = ((payload or {}).get("approver") or principal.agent_id)[:32]
    # ① 只读事务：存在性 + 部门作用域（拿到 category 就关掉事务）
    with session_scope() as session:
        req = session.get(ApprovalRequest, approval_id)
        if req is None:
            raise HTTPException(404, "审批单不存在")
        mart = session.scalar(
            select(MartSkuMetrics).where(MartSkuMetrics.sku_id == req.sku_id)
            .order_by(MartSkuMetrics.as_of.desc())
        )
        ensure_department(principal, mart.category if mart else None, request, sku_id=req.sku_id)
        current_status = req.status

    # ② 写事务：条件更新，rowcount 决定成败
    target = "approved" if approve else "rejected"
    values: dict = {"status": target, "approver": actor, "decided_at": now}
    if not approve:
        reason = ((payload or {}).get("reason") or "").strip()
        if reason:
            values["reason"] = reason[:250]
    with session_scope() as session:
        result = session.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == approval_id,
                   ApprovalRequest.status == "pending_confirmation")
            .values(**values)
        )
        if result.rowcount == 0:
            row = session.get(ApprovalRequest, approval_id)
            if row is None:
                raise HTTPException(404, "审批单不存在")
            raise HTTPException(
                409,
                f"审批单状态为 {row.status}（已由 {row.approver or '他人'} 于 "
                f"{row.decided_at.isoformat(timespec='seconds') if row.decided_at else '未知时间'} "
                "处理），不可重复操作",
            )
        row = session.get(ApprovalRequest, approval_id)
        po = session.get(ErpReplenishOrder, row.erp_order_id) if row.erp_order_id else None
        if po is not None:
            po.status = target
            if approve:
                po.approved_at = now
        session.flush()
        return {"approval_id": approval_id, "status": target, "po_no": row.erp_order_id,
                "erp_status": po.status if po else None, "approver": actor,
                "caller": principal.agent_id, "previous_status": current_status}


@router.post("/approvals/{approval_id}/approve", response_model=sc.ApprovalDecisionOut,
             dependencies=[Depends(require_scope("approve"))])
def approve(approval_id: str, request: Request, payload: dict | None = None,
            principal: Principal = Depends(current_principal)) -> dict:
    return _decide_approval(approval_id, approve=True, payload=payload,
                            principal=principal, request=request)


@router.post("/approvals/{approval_id}/reject", response_model=sc.ApprovalDecisionOut,
             dependencies=[Depends(require_scope("approve"))])
def reject(approval_id: str, request: Request, payload: dict | None = None,
           principal: Principal = Depends(current_principal)) -> dict:
    return _decide_approval(approval_id, approve=False, payload=payload,
                            principal=principal, request=request)


@router.post("/transfer-suggestions", response_model=sc.TransferSuggestionOut,
             dependencies=[Depends(require_scope("read"))])
def transfer_suggestion(payload: dict, request: Request,
                        principal: Principal = Depends(current_principal)) -> dict:
    sku_id = (payload or {}).get("sku_id")
    if not sku_id:
        raise HTTPException(400, "sku_id 必填")
    with session_scope() as session:
        row = _guard_sku(session, principal, sku_id, request)
        rows = session.scalars(
            select(RawErpInventory).where(RawErpInventory.sku_id == sku_id)
        ).all()
        candidates = [
            {"warehouse_id": r.warehouse_id, "on_hand": r.on_hand}
            for r in rows if r.warehouse_id != row.warehouse_id and r.on_hand > 0
        ]
        if not candidates:
            return {"sku_id": sku_id, "suggestion": None,
                    "message": "同款其他仓库无可用库存，无法调拨，建议走补货"}
        best = max(candidates, key=lambda c: c["on_hand"])
        qty = min(int(row.daily_avg_7d * 7) or 10, best["on_hand"])
        return {
            "sku_id": sku_id,
            "suggestion": {"from_warehouse_id": best["warehouse_id"],
                           "to_warehouse_id": row.warehouse_id, "qty": qty},
            "message": f"建议从 {best['warehouse_id']} 调拨 {qty} 件到 {row.warehouse_id}",
        }


# ---------------------------------------------------------------- 运维：ETL 日志 / 调用审计
@router.get("/etl/runs", response_model=sc.EtlRunsOut,
            dependencies=[Depends(require_scope("etl"))])
def etl_runs(limit: int = Query(20, le=100)) -> dict:
    with session_scope() as session:
        rows = session.scalars(
            select(EtlRunLog).order_by(EtlRunLog.id.desc()).limit(limit)
        ).all()
        items = [
            {"dag_id": r.dag_id, "task_id": r.task_id, "run_id": r.run_id, "status": r.status,
             "rows": r.rows, "started_at": r.started_at.isoformat(timespec="seconds"),
             "finished_at": r.finished_at.isoformat(timespec="seconds") if r.finished_at else None,
             "error": r.error}
            for r in rows
        ]
    return {"count": len(items), "items": items}


@router.get("/audit", response_model=sc.AuditOut,
            dependencies=[Depends(require_scope("etl"))])
def list_audit(limit: int = Query(50, ge=1, le=200), agent_id: str = "") -> dict:
    """调用审计：谁在什么时候调了什么、被允许还是被拒（含原因）。"""
    items = audit.recent(limit, agent_id)
    counts: dict[str, int] = {}
    for item in items:
        counts[item["decision"]] = counts.get(item["decision"], 0) + 1
    return {"count": len(items), "by_decision": counts, "items": items}
