"""中台业务域与服装口径（真实 app + 真实 ETL，手算复算）。

评审 2.1-1 / 2.1-5 的验收：
- 订单 / 物流 / 售后 / 竞品 / 供应商 / 大促窗口 都在中台，且**
  部门作用域与幂等、并发确认**这些"业务正确性"由中台兜住；
- 服装口径（尺码曲线 / 齐码率 / 断码率 / 周转 / 售罄 / 生命周期）能被独立手算复现
  —— 指标只要无法复算，就迟早会变成"看着像对的"。
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

FULL = {"X-Internal-Token": "demo-internal-token"}
MEN = {"X-Internal-Token": "k-mens"}


@pytest.fixture(scope="module")
def platform(tmp_path_factory):
    from data_platform.api import cache as pcache
    from data_platform.db import init_schema, make_engine, set_engine
    from data_platform.ingest.airflow_mock import run_dag
    from data_platform.mock_sources import generate_all

    tmp = pathlib.Path(tmp_path_factory.mktemp("platform_orders"))
    engine = make_engine(f"sqlite:///{(tmp / 'platform.db').as_posix()}")
    set_engine(engine)
    init_schema(engine)
    pcache.reset_client()
    generate_all(snapshot_date=dt.date(2026, 11, 3), seed=7)
    run_dag("daily_sync")
    yield {"engine": engine}
    set_engine(None)


@pytest.fixture(autouse=True)
def _principals(monkeypatch):
    from data_platform import config
    from data_platform.api import authz, quota

    keys = {"analysis-agent": {"key": "k-mens", "scopes": ["read"], "departments": ["男装"]}}
    monkeypatch.setattr(config, "API_KEYS_RAW", json.dumps(keys, ensure_ascii=False))
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 600)
    authz.reset_principals()
    quota.reset()
    yield
    authz.reset_principals()
    quota.reset()


@pytest.fixture()
def client(platform):
    from data_platform.api.main import app

    with TestClient(app) as c:
        yield c


def _first_order(client, status: str = "已发货") -> dict:
    from data_platform.db import RawOrder, session_scope

    with session_scope() as session:
        row = session.query(RawOrder).filter(RawOrder.status == status).first()
        return {"order_id": row.order_id, "sku_id": row.sku_id, "qty": row.qty}


# ---------------------------------------------------------------- 订单域
def test_order_detail_includes_logistics_and_tickets(client):
    order = _first_order(client)
    body = client.get(f"/api/v1/orders/{order['order_id']}", headers=FULL).json()
    assert body["found"] is True
    assert body["order"]["sku_id"] == order["sku_id"]
    assert body["logistics"]["tracking_no"], "已发货订单必须有物流单"
    assert isinstance(body["after_sales"], list)


def test_missing_order_is_404(client):
    assert client.get("/api/v1/orders/ORD-NOPE", headers=FULL).status_code == 404


def test_order_department_scope(client):
    from data_platform.db import MartSkuMetrics, RawOrder, session_scope

    with session_scope() as session:
        women = session.query(RawOrder).join(
            MartSkuMetrics, MartSkuMetrics.sku_id == RawOrder.sku_id
        ).filter(MartSkuMetrics.department == "女装", RawOrder.status == "已发货").first()
        order_id = women.order_id
    resp = client.get(f"/api/v1/orders/{order_id}", headers=MEN)
    assert resp.status_code == 403 and "只能访问" in resp.json()["detail"]


def test_return_risk_and_logistics_endpoints(client):
    order = _first_order(client)
    risk = client.get(f"/api/v1/skus/{order['sku_id']}/return-risk", headers=FULL).json()
    assert risk["found"] is True and 0 <= risk["return_rate"] < 1
    logistics = client.get(f"/api/v1/orders/{order['order_id']}/logistics",
                           headers=FULL).json()
    assert logistics["found"] is True and logistics["logistics"]["carrier"]


# ---------------------------------------------------------------- 售后（幂等 + 并发确认）
def test_after_sales_idempotent_and_conflict(client):
    order = _first_order(client)
    payload = {"order_id": order["order_id"], "reason": "尺码不合适", "qty": 1}
    first = client.post("/api/v1/after-sales", headers=FULL, json=payload).json()
    second = client.post("/api/v1/after-sales", headers=FULL, json=payload).json()
    assert first["deduped"] is False and second["deduped"] is True
    assert first["ticket_id"] == second["ticket_id"]
    assert first["status"] == "pending_confirmation"

    ok = client.post(f"/api/v1/after-sales/{first['ticket_id']}/approve", headers=FULL,
                     json={"approver": "worker1"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "approved" and ok.json()["decided_at"]
    again = client.post(f"/api/v1/after-sales/{first['ticket_id']}/approve", headers=FULL, json={})
    assert again.status_code == 409, "重复确认必须 409（条件更新）"


def test_after_sales_validation(client):
    order = _first_order(client)
    bad_reason = client.post("/api/v1/after-sales", headers=FULL,
                             json={"order_id": order["order_id"], "reason": "随便退"})
    assert bad_reason.status_code == 400 and "不支持的售后原因" in bad_reason.json()["detail"]

    too_many = client.post("/api/v1/after-sales", headers=FULL,
                           json={"order_id": order["order_id"], "reason": "质量问题",
                                 "qty": int(order["qty"]) + 10})
    assert too_many.status_code == 400 and "超过订单购买数量" in too_many.json()["detail"]

    missing = client.post("/api/v1/after-sales", headers=FULL,
                          json={"order_id": "ORD-NOPE", "reason": "质量问题"})
    assert missing.status_code == 404


def test_after_sales_list_is_department_filtered(client):
    from data_platform.db import MartSkuMetrics, RawAfterSalesTicket, session_scope

    with session_scope() as session:
        women_ticket = session.query(RawAfterSalesTicket).join(
            MartSkuMetrics, MartSkuMetrics.sku_id == RawAfterSalesTicket.sku_id
        ).filter(MartSkuMetrics.department == "女装").first()
        assert women_ticket is not None
        ticket_id = women_ticket.ticket_id
    listing = client.get("/api/v1/after-sales?status=all", headers=MEN).json()
    assert listing["scope_filtered"] is True
    assert ticket_id not in {i["ticket_id"] for i in listing["items"]}
    assert all(i["department"] == "男装" for i in listing["items"])
    # 越权审批必须被中台挡住
    denied = client.post(f"/api/v1/after-sales/{ticket_id}/approve", headers=MEN, json={})
    assert denied.status_code in (403, 404)


# ---------------------------------------------------------------- 供应链 / 竞品 / 大促
def test_supplier_transfer_and_activity_endpoints(client):
    supplier = client.get("/api/v1/suppliers/SUP-01", headers=FULL).json()
    assert supplier["found"] is True and supplier["on_time_rate"] > 0

    activities = client.get("/api/v1/platform-activities?platform=淘系", headers=FULL).json()
    assert activities["count"] >= 1
    assert all(a["platform"] in ("淘系", "全部") for a in activities["items"])

    from data_platform.db import RawTransferOrder, session_scope

    with session_scope() as session:
        transfer = session.query(RawTransferOrder).first()
        sku_id = transfer.sku_id if transfer is not None else None
    if sku_id:
        items = client.get(f"/api/v1/skus/{sku_id}/transfers", headers=FULL).json()["items"]
        assert items and items[0]["from_warehouse_id"]


def test_competitor_endpoints_are_style_scoped(client):
    body = client.get("/api/v1/styles/ST-01/competitors", headers=FULL).json()
    assert body["found"] is True and body["price_min"] <= body["price_max"]
    band = client.get("/api/v1/styles/ST-01/price-band", headers=FULL).json()
    assert band["price_median"] is not None
    heat = client.get("/api/v1/styles/ST-01/heat", headers=FULL).json()
    assert heat["heat_index"] >= 0
    # 女装款对男装 key 不可见
    assert client.get("/api/v1/styles/ST-01/competitors", headers=MEN).status_code == 403
    assert client.get("/api/v1/styles/ST-05/competitors", headers=MEN).status_code == 200


# ---------------------------------------------------------------- 服装口径（手算复算）
def test_size_curve_and_gap_are_reproducible(platform, client):
    """断码款 ST-02：主销码 M/L 无库存 —— 用手算的 mart 数据复算一遍接口输出。"""
    from data_platform.db import MartSkuMetrics, session_scope

    with session_scope() as session:
        rows = session.scalars(
            select(MartSkuMetrics).where(MartSkuMetrics.style_id == "ST-02")
        ).all()
        by_size: dict[str, dict] = {}
        for r in rows:
            slot = by_size.setdefault(r.size, {"qty": 0, "available": 0})
            slot["qty"] += int(r.qty_30d or 0)
            slot["available"] += int(r.available or 0)
        total_qty = sum(v["qty"] for v in by_size.values())

    body = client.get("/api/v1/styles/ST-02/metrics", headers=FULL).json()
    curve = {c["size"]: c for c in body["size_curve"]}
    assert set(curve) == set(by_size)
    for size, slot in by_size.items():
        expected_share = round(slot["qty"] / total_qty, 4) if total_qty else 0.0
        assert curve[size]["available"] == slot["available"]
        assert abs(curve[size]["size_share"] - expected_share) < 1e-6

    main_sizes = [s for s, c in curve.items() if c["main_size_flag"]]
    gaps = [s for s, c in curve.items() if c["gap"]]
    assert gaps == ["M", "L"], f"ST-02 的主销码应断 M/L，实际 {gaps}"
    assert all(s in main_sizes for s in gaps), "断码只能发生在主销码上"
    assert body["full_size_flag"] is False
    assert body["size_gap"]["broken_size_count"] == len(gaps)
    assert body["size_gap"]["missing_sizes"] == gaps
    assert body["size_gap"]["size_gap_rate"] == round(len(gaps) / len(curve), 4)


def test_apparel_overview_matches_style_rows(platform, client):
    rows = _style_rows(platform)
    total = len(rows)
    full = sum(r["full_size_flag"] for r in rows)
    broken = sum(1 for r in rows if r["broken_size_count"] > 0)
    turnovers = [r["turnover_days"] for r in rows if r["turnover_days"] is not None]
    expected = {
        "style_count": total,
        "full_size_styles": full,
        "broken_size_styles": broken,
        "full_size_rate": round(full / total, 4),
        "broken_size_rate": round(broken / total, 4),
        "avg_turnover_days": round(sum(turnovers) / len(turnovers), 2),
    }

    body = client.get("/api/v1/metrics/apparel-overview", headers=FULL).json()
    for key, value in expected.items():
        assert body[key] == value, f"{key}: 接口 {body[key]} ≠ 手算 {value}"

    gap_body = client.get("/api/v1/metrics/size-gap", headers=FULL).json()
    assert gap_body["count"] == broken
    assert gap_body["full_size_rate"] == expected["full_size_rate"]
    assert {i["style_id"] for i in gap_body["items"]} == {
        r["style_id"] for r in rows if r["broken_size_count"] > 0
    }


def _style_rows(platform) -> list[dict]:
    """款级 mart 快照（出事务就 detach，所以取成普通 dict）。"""
    from data_platform.db import MartStyleMetrics, session_scope

    with session_scope() as session:
        return [
            {"style_id": r.style_id, "full_size_flag": int(r.full_size_flag or 0),
             "broken_size_count": int(r.broken_size_count or 0),
             "turnover_days": r.turnover_days,
             "days_since_launch": r.days_since_launch,
             "sell_through_rate": float(r.sell_through_rate or 0),
             "lifecycle_stage": r.lifecycle_stage}
            for r in session.scalars(select(MartStyleMetrics))
        ]


def test_lifecycle_stage_rule(platform, client):
    """生命周期规则：上新(≤14天) / 清仓(售罄率≥85% 或 >120天) / 长尾(>180天且售罄率<20%)。

    mart 里的阶段必须与规则函数逐行一致（指标与口径不能各算一套）；
    边界值用纯函数单测固化，避免依赖"今天是几号"。
    """
    from data_platform.dbt_like.models import lifecycle_of

    rows = _style_rows(platform)
    assert rows
    for row in rows:
        assert row["lifecycle_stage"] == lifecycle_of(
            row["days_since_launch"], row["sell_through_rate"]
        ), row["style_id"]
        assert row["lifecycle_stage"] in ("上新", "主推", "清仓", "长尾")

    # 边界（与 governance.METRIC_DEFINITIONS 的公式一致）
    assert lifecycle_of(10, 0.3) == "上新"
    assert lifecycle_of(15, 0.3) == "主推"
    assert lifecycle_of(60, 0.9) == "清仓"          # 售罄率 ≥85% → 清尾货
    assert lifecycle_of(200, 0.1) == "长尾"         # 上架久且卖不动
    assert lifecycle_of(200, 0.5) == "清仓"         # 上架太久
    assert lifecycle_of(None, 0.5) == ""


def test_sales_series_fills_zero_days(client):
    from data_platform.db import RawOrder, session_scope

    with session_scope() as session:
        sku_id = session.query(RawOrder).first().sku_id
    body = client.get(f"/api/v1/skus/{sku_id}/sales-series?days=7", headers=FULL).json()
    assert len(body["daily"]) == 7
    assert body["daily"] == sorted(body["daily"], key=lambda d: d["date"])


def test_batch_endpoint_filters_scope(client):
    from data_platform.db import MartSkuMetrics, session_scope

    with session_scope() as session:
        men_sku = session.scalars(
            select(MartSkuMetrics.sku_id).where(MartSkuMetrics.department == "男装").limit(1)
        ).first()
        women_sku = session.scalars(
            select(MartSkuMetrics.sku_id).where(MartSkuMetrics.department == "女装").limit(1)
        ).first()

    body = client.get(f"/api/v1/skus/batch?sku_ids={men_sku},{women_sku},ST-99-X",
                      headers=MEN).json()
    assert [i["sku_id"] for i in body["items"]] == [men_sku]
    assert body["scope_dropped"] == 1 and body["missing"] == ["ST-99-X"]
    assert body["count"] == 1


def test_etl_run_log_records_new_stages(client):
    """新业务域的接入任务必须留痕（否则"物流同步挂了"只能靠猜）。"""
    runs = client.get("/api/v1/etl/runs?limit=100", headers=FULL).json()
    tasks = {item["task_id"] for item in runs["items"]}
    assert {"sync_logistics", "sync_after_sales", "sync_market", "sync_supply",
            "quality_check", "dbt_run"} <= tasks
    assert all(item["status"] in ("success", "running", "failed") for item in runs["items"])
