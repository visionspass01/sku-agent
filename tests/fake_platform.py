"""测试用的「假中台」（httpx.MockTransport handler）。

为什么需要：业务域上移到中台之后，批量巡检 Worker / 对话工具 / 订单工具都改成走 REST，
单测若不打桩就会真的连 `127.0.0.1:8001`。这里提供一个**以 Agent 演示库为数据源**的参考实现
（等价于"中台跑在同一份演示数据上"），让既有用例的数值断言继续成立。

与 `tests/test_mcp_platform_adapter.py` / `tests/test_platform_orders.py` 的分工：
- 本文件：*集成环境替身*，覆盖大量端点的正常路径，供其它用例复用；
- 那两个文件：用**真实中台 app**（`httpx.ASGITransport`）验证契约与业务规则。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid

import httpx

OK = 200


def _json(payload: dict, status: int = OK) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _sku_facts(sku_id: str) -> dict | None:
    """从 Agent 演示库取 SKU 事实（等价于中台 mart 的一行）。"""
    from app.db import repos
    from app.db.engine import session_scope

    with session_scope() as session:
        sku = repos.get_sku(session, sku_id)
        if sku is None:
            return None
        rows = repos.get_stat_hours(session, sku_id,
                                    dt.datetime.now() - dt.timedelta(days=30), dt.datetime.now())
        qty30 = sum(int(q) for _ts, q in rows)
        qty7 = sum(int(q) for ts, q in rows if ts >= dt.datetime.now() - dt.timedelta(days=7))
        supplier = session.get(__import__("app.db.models", fromlist=["Supplier"]).Supplier,
                               sku.supplier_id) if sku.supplier_id else None
        return {
            "sku_id": sku.sku_id, "style_id": sku.style_id, "style_name": sku.style_name,
            "category": sku.category, "department": _dept(sku.category), "season": sku.season,
            "color": sku.color, "size": sku.size, "warehouse_id": sku.warehouse_id,
            "platform": sku.platform, "price": sku.price, "list_price": sku.list_price,
            "on_hand": sku.stock, "available": sku.stock,
            "daily_avg_7d": round(qty7 / 7.0, 2), "daily_avg_30d": round(qty30 / 30.0, 2),
            "qty_30d": qty30, "return_rate": round(float(sku.return_rate or 0), 4),
            "supplier_id": sku.supplier_id,
            "lead_time_days": supplier.lead_time_days if supplier else 10,
            "lifecycle_stage": "主推", "size_share": 0.2, "main_size_flag": 1,
            "size_gap_flag": 1 if int(sku.stock or 0) <= 0 else 0,
            "turnover_days": round(sku.stock / max(qty30 / 30.0, 0.01), 2) if qty30 else None,
            "sell_through_rate": round(qty30 / max(qty30 + int(sku.stock or 0), 1), 4),
            "days_since_launch": 60, "sold_30d_flag": 1 if qty30 else 0,
            "in_transit_7d": 0, "safety_stock": 0, "reorder_point": 0,
            "sell_through_days": None, "stockout_risk": "低",
            "as_of": dt.date.today().isoformat(), "data_version": 1, "schema_version": "1.1",
        }


def _dept(category: str | None) -> str | None:
    from app.config import department_of

    return department_of(category)


def _daily_series(sku_id: str, days: int) -> list[dict]:
    from app.db import repos
    from app.db.engine import session_scope

    today = dt.date.today()
    cutoff = today - dt.timedelta(days=days - 1)
    with session_scope() as session:
        rows = repos.get_stat_hours(session, sku_id,
                                    dt.datetime.combine(cutoff, dt.time()),
                                    dt.datetime.combine(today, dt.time(23, 59)))
    by_day: dict[str, int] = {}
    for ts, qty in rows:
        key = ts.date().isoformat()
        by_day[key] = by_day.get(key, 0) + int(qty)
    cursor = cutoff
    while cursor <= today:
        by_day.setdefault(cursor.isoformat(), 0)
        cursor += dt.timedelta(days=1)
    return [{"date": d, "qty": q} for d, q in sorted(by_day.items())]


def handler(request: httpx.Request) -> httpx.Response:  # noqa: C901 —— 分发器，按路径分支
    """按路径分发的中台替身。未覆盖的路径返回 404（便于发现"生产多调了哪个接口"）。"""
    path = request.url.path
    method = request.method.upper()
    q = request.url.params

    if path == "/api/v1/health":
        return _json({"status": "ok", "data_version": 1, "mart_rows": 1})

    # ---------------- 元数据 ----------------
    if path == "/api/v1/metadata/catalog":
        return _json({"items": _catalog_items(), "count": len(_catalog_items()),
                      "total": len(_catalog_items()), "next_offset": None,
                      "data_version": 1, "schema_version": "1.1"})
    if path == "/api/v1/metadata/departments":
        from app.config import DEPARTMENT_CATEGORIES

        return _json({"departments": {k: list(v) for k, v in DEPARTMENT_CATEGORIES.items()},
                      "version": "fake", "schema_version": "1.1"})
    if path == "/api/v1/metadata/metrics":
        from data_platform.governance import METRIC_DEFINITIONS

        return _json({"metrics": METRIC_DEFINITIONS, "prompt_context": "", "data_version": 1,
                      "schema_version": "1.1"})
    if path == "/api/v1/metadata/capabilities":
        from data_platform.capabilities import catalog

        return _json(catalog())

    # ---------------- SKU 维度 ----------------
    m = re.match(r"^/api/v1/skus/batch$", path)
    if m:
        ids = [x for x in (q.get("sku_ids") or "").split(",") if x]
        items, missing = [], []
        for sku_id in ids:
            facts = _sku_facts(sku_id)
            if facts is None:
                missing.append(sku_id)
            else:
                items.append(facts)
        return _json({"items": items, "count": len(items), "missing": missing,
                      "scope_filtered": False, "scope_dropped": 0, "data_version": 1})

    m = re.match(r"^/api/v1/skus/([^/]+)/(.+)$", path)
    if m:
        sku_id, action = m.group(1), m.group(2)
        facts = _sku_facts(sku_id)
        if facts is None:
            return _json({"detail": f"数据中台无该 SKU：{sku_id}"}, 404)
        if action == "metrics" or action == "inventory":
            return _json(facts)
        if action == "sales":
            return _json({"sku_id": sku_id, "qty_7d": int(facts["daily_avg_7d"] * 7),
                          "qty_30d": facts["qty_30d"], "daily_avg_7d": facts["daily_avg_7d"],
                          "daily_avg_30d": facts["daily_avg_30d"],
                          "sold_30d_flag": facts["sold_30d_flag"], "data_version": 1})
        if action == "sell-through":
            return _json({"sku_id": sku_id, "available": facts["available"],
                          "sell_through_days": facts["sell_through_days"],
                          "stockout_risk": facts["stockout_risk"], "data_version": 1})
        if action == "sales-series":
            days = int(q.get("days") or 7)
            return _json({"sku_id": sku_id, "days": days, "daily": _daily_series(sku_id, days)})
        if action == "in-transit":
            from app.db import repos
            from app.db.engine import session_scope

            with session_scope() as session:
                rows = repos.get_in_transit(session, sku_id)
                items = [{"qty": r.qty, "eta": r.eta.isoformat(),
                          "days_to_arrive": (r.eta - dt.date.today()).days,
                          "po_no": r.po_no, "status": r.status} for r in rows]
            return _json({"sku_id": sku_id, "items": items,
                          "total": sum(i["qty"] for i in items), "data_version": 1})
        if action == "transfers":
            from app.db import repos
            from app.db.engine import session_scope

            with session_scope() as session:
                rows = repos.get_transfers(session, sku_id)
                items = [{"transfer_no": "", "sku_id": r.sku_id,
                          "from_warehouse_id": r.from_warehouse_id,
                          "to_warehouse_id": r.to_warehouse_id, "qty": r.qty,
                          "eta": r.eta.isoformat(), "status": r.status} for r in rows]
            return _json({"sku_id": sku_id, "items": items, "count": len(items)})
        if action == "orders":
            from app.db.engine import session_scope
            from app.db.models import MockOrder

            days = int(q.get("days") or 7)
            limit = int(q.get("limit") or 30)
            cutoff = dt.datetime.now() - dt.timedelta(days=days)
            with session_scope() as session:
                rows = (session.query(MockOrder)
                        .filter(MockOrder.sku_id == sku_id, MockOrder.created_at >= cutoff)
                        .order_by(MockOrder.created_at.desc()).limit(limit).all())
                items = [{"order_id": r.order_id, "qty": r.qty, "amount": r.amount,
                          "order_date": r.created_at.date().isoformat(), "status": r.status,
                          "receiver": ""} for r in rows]
            return _json({"sku_id": sku_id, "days": days, "items": items,
                          "count": len(items),
                          "total_qty": sum(i["qty"] for i in items),
                          "pending_qty": sum(i["qty"] for i in items if i["status"] == "待发货"),
                          "data_version": 1})
        if action == "return-risk":
            from app.db.engine import session_scope
            from app.db.models import AfterSalesTicket

            with session_scope() as session:
                tickets = session.query(AfterSalesTicket).filter(
                    AfterSalesTicket.sku_id == sku_id).count()
            return _json({"sku_id": sku_id, "found": True,
                          "return_rate": facts["return_rate"], "orders_last_days": 1,
                          "after_sales_tickets": tickets,
                          "risk": "高" if facts["return_rate"] >= 0.18 else "低"})
        return _json({"detail": f"未覆盖的 SKU 子路径：{action}"}, 404)

    # ---------------- 款级 ----------------
    m = re.match(r"^/api/v1/styles/([^/]+)/(.+)$", path)
    if m:
        style_id, action = m.group(1), m.group(2)
        if action == "metrics":
            return _json(_style_payload(style_id))
        if action == "competitors":
            from app.db import repos
            from app.db.engine import session_scope

            with session_scope() as session:
                rows = repos.get_competitors(session, style_id)
                if not rows:
                    return _json({"style_id": style_id, "found": False, "competitors": [],
                                  "note": "未检索到同款竞品"})
                prices = [r.price for r in rows]
                sales7 = sum(r.sales_7d for r in rows)
                return _json({
                    "style_id": style_id, "found": True,
                    "competitors": [
                        {"shop_name": r.shop_name, "platform": r.platform, "price": r.price,
                         "sales_30d": r.sales_30d, "sales_7d": r.sales_7d, "rating": r.rating,
                         "trend_pct": r.trend_pct,
                         "snapshot_ts": r.snapshot_ts.isoformat(timespec="minutes")}
                        for r in rows],
                    "price_min": min(prices), "price_max": max(prices),
                    "price_avg": round(sum(prices) / len(prices), 2),
                    "heat_index": round(min(100.0, sales7 / 10.0), 1),
                    "trend_pct": 0.05,
                })
        if action == "heat":
            data = handler(httpx.Request("GET", f"http://x/api/v1/styles/{style_id}/competitors"))
            body = json.loads(data.content)
            sales7 = sum(int(c["sales_7d"]) for c in body.get("competitors") or [])
            return _json({"style_id": style_id, "found": body.get("found", False),
                          "competitor_sales_7d": sales7, "avg_trend_pct": 0.05,
                          "heat_index": round(min(100.0, sales7 / 10.0), 1)})
        if action == "price-band":
            data = handler(httpx.Request("GET", f"http://x/api/v1/styles/{style_id}/competitors"))
            body = json.loads(data.content)
            if not body.get("found"):
                return _json({"style_id": style_id, "found": False})
            return _json({"style_id": style_id, "found": True, "count": 1,
                          "price_min": body["price_min"], "price_max": body["price_max"],
                          "price_median": body["price_avg"], "price_avg": body["price_avg"]})
        return _json({"detail": f"未覆盖的款级子路径：{action}"}, 404)

    # ---------------- 订单 / 售后 ----------------
    if path == "/api/v1/after-sales":
        if method == "POST":
            body = json.loads(request.content or b"{}")
            from app.db.engine import session_scope
            from app.db.models import AfterSalesTicket, MockOrder

            reason = str(body.get("reason") or "")
            if reason not in ("尺码不合适", "质量问题", "7天无理由", "发错货", "物流破损"):
                return _json({"detail": f"不支持的售后原因：{reason}"}, 400)
            with session_scope() as session:
                order = session.get(MockOrder, body.get("order_id"))
                if order is None:
                    return _json({"detail": f"订单不存在：{body.get('order_id')}"}, 404)
                qty = int(body.get("qty") or order.qty or 0)
                if qty > int(order.qty or 0):
                    return _json({"detail": f"售后数量 {qty} 超过订单购买数量 {order.qty}"}, 400)
                key = f"{order.order_id}|{reason}|{qty}"
                existing = session.query(AfterSalesTicket).filter(
                    AfterSalesTicket.idempotency_key == key).first()
                if existing is not None:
                    return _json({"ticket_id": existing.ticket_id, "order_id": order.order_id,
                                  "sku_id": existing.sku_id, "reason": existing.reason,
                                  "qty": existing.qty, "status": existing.status,
                                  "deduped": True})
                ticket = AfterSalesTicket(
                    ticket_id=str(uuid.uuid4()), order_id=order.order_id, sku_id=order.sku_id,
                    reason=reason, qty=qty, status="pending_confirmation",
                    idempotency_key=key, created_at=dt.datetime.now())
                session.add(ticket)
                session.flush()
                return _json({"ticket_id": ticket.ticket_id, "order_id": order.order_id,
                              "sku_id": order.sku_id, "reason": reason, "qty": qty,
                              "status": ticket.status, "deduped": False})
        from app.db.engine import session_scope
        from app.db.models import AfterSalesTicket

        status = q.get("status") or "all"
        with session_scope() as session:
            query = session.query(AfterSalesTicket).order_by(AfterSalesTicket.created_at.desc())
            if status == "pending":
                query = query.filter(AfterSalesTicket.status == "pending_confirmation")
            elif status != "all":
                query = query.filter(AfterSalesTicket.status == status)
            rows = query.limit(int(q.get("limit") or 50)).all()
            items = [{"ticket_id": r.ticket_id, "order_id": r.order_id, "sku_id": r.sku_id,
                      "reason": r.reason, "qty": r.qty, "status": r.status,
                      "department": _dept((_sku_facts(r.sku_id) or {}).get("category")),
                      "approver": r.approver,
                      "decided_at": r.decided_at.isoformat(timespec="seconds") if r.decided_at else None,
                      "created_at": r.created_at.isoformat(timespec="seconds")} for r in rows]
        return _json({"status": status, "items": items, "count": len(items),
                      "scope_filtered": False, "scope_dropped": 0})

    m = re.match(r"^/api/v1/after-sales/([^/]+)/(approve|reject)$", path)
    if m:
        ticket_id, action = m.group(1), m.group(2)
        from app.db.engine import session_scope
        from app.db.models import AfterSalesTicket

        with session_scope() as session:
            row = session.get(AfterSalesTicket, ticket_id)
            if row is None:
                return _json({"detail": "售后工单不存在"}, 404)
            if row.status != "pending_confirmation":
                return _json({"detail": f"工单状态为 {row.status}，不可重复操作"}, 409)
            row.status = "approved" if action == "approve" else "rejected"
            row.approver = (json.loads(request.content or b"{}") or {}).get("approver", "tester")
            row.decided_at = dt.datetime.now()
            return _json({"ticket_id": ticket_id, "status": row.status,
                          "approver": row.approver,
                          "decided_at": row.decided_at.isoformat(timespec="seconds"),
                          "previous_status": "pending_confirmation"})

    m = re.match(r"^/api/v1/orders/([^/]+)(/logistics)?$", path)
    if m:
        order_id, logistics = m.group(1), m.group(2)
        from app.db.engine import session_scope
        from app.db.models import MockLogistics, MockOrder

        with session_scope() as session:
            order = session.get(MockOrder, order_id)
            if order is None:
                return _json({"detail": f"订单不存在：{order_id}"}, 404)
            log_row = session.get(MockLogistics, order_id)
            log = {} if log_row is None else {
                "carrier": log_row.carrier, "tracking_no": log_row.tracking_no,
                "status": log_row.status,
                "eta": log_row.eta.isoformat() if log_row.eta else "",
                "updated_at": log_row.updated_at.isoformat(timespec="minutes"),
            }
            if logistics:
                return _json({"order_id": order_id, "found": True, "logistics": log})
            tickets = session.query(__import__("app.db.models", fromlist=["AfterSalesTicket"])
                                    .AfterSalesTicket).filter(
                __import__("app.db.models", fromlist=["AfterSalesTicket"]).AfterSalesTicket.order_id
                == order_id).all()
            return _json({
                "order_id": order_id, "found": True,
                "order": {"order_id": order.order_id, "sku_id": order.sku_id, "qty": order.qty,
                          "amount": order.amount, "status": order.status,
                          "order_date": order.created_at.date().isoformat(), "receiver": ""},
                "logistics": log,
                "after_sales": [{"ticket_id": t.ticket_id, "status": t.status,
                                 "reason": t.reason, "qty": t.qty} for t in tickets],
            })

    # ---------------- 供应链 / 平台 ----------------
    m = re.match(r"^/api/v1/suppliers/([^/]+)$", path)
    if m:
        from app.db.engine import session_scope
        from app.db.models import Supplier

        with session_scope() as session:
            row = session.get(Supplier, m.group(1))
            if row is None:
                return _json({"supplier_id": m.group(1), "found": False})
            return _json({"supplier_id": row.id, "found": True, "name": row.name,
                          "lead_time_days": row.lead_time_days, "moq": row.moq,
                          "on_time_rate": row.on_time_rate})

    if path == "/api/v1/platform-activities":
        from app.db.engine import session_scope
        from app.db.models import PlatformActivity

        platform = q.get("platform") or ""
        days = int(q.get("days") or 45)
        today = dt.date.today()
        end = today + dt.timedelta(days=days)
        with session_scope() as session:
            rows = (session.query(PlatformActivity)
                    .filter(PlatformActivity.end_date >= today,
                            PlatformActivity.start_date <= end).all())
            items = [{"name": r.name, "platform": r.platform,
                      "start_date": r.start_date.isoformat(), "end_date": r.end_date.isoformat(),
                      "days_to_start": (r.start_date - today).days,
                      "category": r.category, "note": r.note}
                     for r in rows if not platform or r.platform in (platform, "全部")]
        return _json({"platform": platform, "items": items, "count": len(items)})

    # ---------------- 审批 / 聚合（Agent 侧列表页用） ----------------
    if path == "/api/v1/approvals":
        return _json({"status": q.get("status") or "pending", "items": [], "count": 0,
                      "scope_filtered": False, "scope_dropped": 0})
    if path == "/api/v1/metrics/sales-rate":
        return _json({"department": q.get("department") or "全部", "sold_30d_skus": 1,
                      "total_skus": 3, "sales_rate": 0.33, "effective_departments": None})
    if path == "/api/v1/metrics/apparel-overview":
        return _json({"department": q.get("department"), **_apparel_overview()})
    if path == "/api/v1/metrics/size-gap":
        overview = _apparel_overview()
        broken = [s for s in _style_summaries() if s["missing_sizes"]]
        return _json({
            "department": q.get("department"),
            "items": broken[: int(q.get("limit") or 20)],
            "count": len(broken),
            "full_size_rate": overview["full_size_rate"],
            "broken_size_rate": overview["broken_size_rate"],
            "formula": {"full_size_rate": "齐码率 = 全码可用>0 的款数 ÷ 在售款数"},
        })
    if path == "/api/v1/metrics/size-curve":
        style_id = q.get("style_id") or ""
        return _json({"style_id": style_id,
                      "items": _style_payload(style_id).get("size_curve") or []})

    return _json({"detail": f"假中台未覆盖：{method} {path}"}, 404)


def _catalog_items() -> list[dict]:
    from app.db import repos
    from app.db.engine import session_scope

    with session_scope() as session:
        rows = repos.list_skus(session, limit=2000)
        return [{"sku_id": s.sku_id, "style_id": s.style_id, "style_name": s.style_name,
                 "category": s.category, "department": _dept(s.category), "season": s.season,
                 "color": s.color, "size": s.size, "price": s.price,
                 "supplier_id": s.supplier_id} for s in rows]


def _style_payload(style_id: str) -> dict:
    from app.db import repos
    from app.db.engine import session_scope

    with session_scope() as session:
        rows = repos.get_style_skus(session, style_id)
        if not rows:
            return {"detail": f"数据中台无该款式：{style_id}"}
        sizes = sorted({s.size for s in rows})
        breakdown = [{"sku_id": s.sku_id, "style_id": s.style_id, "color": s.color,
                      "size": s.size, "available": s.stock, "on_hand": s.stock,
                      "price": s.price, "warehouse_id": s.warehouse_id,
                      "qty_30d": 0, "size_share": 0.2, "main_size_flag": 1,
                      "size_gap_flag": 1 if int(s.stock or 0) <= 0 else 0} for s in rows]
        curve = [{"size": z, "qty_30d": 0, "available": sum(
            int(s.stock or 0) for s in rows if s.size == z), "size_share": 0.2,
            "main_size_flag": 1,
            "gap": 1 if all(int(s.stock or 0) <= 0 for s in rows if s.size == z) else 0}
            for z in sizes]
        listed = [c for c in curve if c["available"] > 0]
        return {
            "style_id": style_id, "style_name": rows[0].style_name,
            "category": rows[0].category, "department": _dept(rows[0].category),
            "season": rows[0].season, "lifecycle_stage": "主推",
            "sku_count": len(rows), "color_count": len({s.color for s in rows}),
            "size_count": len(sizes), "listed_size_count": len(listed),
            "total_available": sum(int(s.stock or 0) for s in rows),
            "qty_30d": 0, "daily_avg_7d": 0.0, "sell_through_rate": 0.4,
            "turnover_days": 30.0,
            "full_size_flag": len(listed) == len(sizes),
            "size_gap": {"broken_size_count": sum(1 for c in curve if c["gap"]),
                         "size_gap_rate": 0.0,
                         "missing_sizes": [c["size"] for c in curve if c["gap"]],
                         "listed_sizes": [c["size"] for c in listed]},
            "size_curve": curve, "size_breakdown": breakdown, "data_version": 1,
        }

def _style_summaries() -> list[dict]:
    """按款汇总（给断码清单/概览用）：缺哪些码、是否齐码。"""
    from app.db import repos
    from app.db.engine import session_scope

    out: list[dict] = []
    with session_scope() as session:
        style_ids = sorted({s.style_id for s in repos.list_skus(session, limit=2000)})
        for style_id in style_ids:
            rows = repos.get_style_skus(session, style_id)
            if not rows:
                continue
            stock_by_size: dict[str, int] = {}
            for r in rows:
                stock_by_size[r.size] = stock_by_size.get(r.size, 0) + int(r.stock or 0)
            missing = sorted(s for s, qty in stock_by_size.items() if qty <= 0)
            out.append({
                "style_id": style_id,
                "style_name": rows[0].style_name,
                "department": _dept(rows[0].category),
                "category": rows[0].category,
                "lifecycle_stage": "主推",
                "broken_size_count": len(missing),
                "size_gap_rate": round(len(missing) / len(stock_by_size), 4) if stock_by_size else 0.0,
                "missing_sizes": missing,
                "listed_sizes": sorted(s for s, qty in stock_by_size.items() if qty > 0),
                "size_count": len(stock_by_size),
                "total_available": sum(stock_by_size.values()),
                "qty_30d": 0,
            })
    return out


def _apparel_overview() -> dict:
    summaries = _style_summaries()
    total = len(summaries) or 1
    full = sum(1 for s in summaries if not s["missing_sizes"])
    broken = sum(1 for s in summaries if s["missing_sizes"])
    return {
        "style_count": len(summaries),
        "full_size_styles": full,
        "broken_size_styles": broken,
        "full_size_rate": round(full / total, 4),
        "broken_size_rate": round(broken / total, 4),
        "avg_turnover_days": None,
        "avg_sell_through_rate": 0.0,
        "lifecycle_breakdown": {"主推": len(summaries)},
    }
