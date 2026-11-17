"""数据中台测试：接入幂等 / 脱敏 / dbt 指标口径 / 缓存 / 审批流。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

TOKEN = {"X-Internal-Token": "demo-internal-token"}


@pytest.fixture(scope="module")
def platform_ready(tmp_path_factory):
    from data_platform.api import cache as pcache
    from data_platform.db import init_schema, make_engine, set_engine

    tmp = tmp_path_factory.mktemp("platform")
    engine = make_engine(f"sqlite:///{(tmp / 'platform.db').as_posix()}")
    set_engine(engine)
    init_schema(engine)
    pcache.reset_client()

    from data_platform.mock_sources import generate_all

    generate_all(snapshot_date=__import__("datetime").date(2026, 11, 3), seed=7)

    from data_platform.ingest.airflow_mock import run_dag

    first = run_dag("daily_sync")
    assert all(t["status"] == "success" for t in first["tasks"].values()), first
    yield first
    set_engine(None)


@pytest.fixture()
def client(platform_ready):
    from data_platform.api.main import app

    with TestClient(app) as c:
        yield c


def test_health_and_token(client):
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/skus/ST-01-C1-M/metrics").status_code == 401
    assert client.get("/api/v1/skus/ST-01-C1-M/metrics", headers=TOKEN).status_code == 200


def test_ingest_idempotent(platform_ready):
    from sqlalchemy import func, select

    from data_platform.db import RawErpInventory, RawOrder, session_scope

    with session_scope() as session:
        inv1 = session.scalar(select(func.count()).select_from(RawErpInventory))
        orders1 = session.scalar(select(func.count()).select_from(RawOrder))

    from data_platform.ingest.airflow_mock import run_dag

    run_dag("daily_sync")
    with session_scope() as session:
        inv2 = session.scalar(select(func.count()).select_from(RawErpInventory))
        orders2 = session.scalar(select(func.count()).select_from(RawOrder))
    assert inv1 == inv2 and orders1 == orders2


def test_pii_desensitized(client):
    from sqlalchemy import select

    from data_platform.db import DimCustomer, RawCrmCustomer, RawOrder, session_scope

    with session_scope() as session:
        customers = [(c.name_masked, c.phone_masked) for c in session.scalars(select(RawCrmCustomer))]
        orders = [o.receiver_masked for o in session.scalars(select(RawOrder).limit(20))]
        dims = [d.phone_masked for d in session.scalars(select(DimCustomer))]
    assert customers and orders and dims
    for name, phone in customers:
        assert "*" in name and "*" in phone
        assert phone[3:7] == "****"
    for receiver in orders:
        assert "*" in receiver
    for phone in dims:
        assert "*" in phone


def test_metrics_formulas(client):
    sku = "ST-01-C1-M"
    m = client.get(f"/api/v1/skus/{sku}/metrics", headers=TOKEN).json()
    assert m["available"] <= m["on_hand"]
    # 可用 = 现存 - 锁定 - CRM预订 - 订单占用
    assert m["available"] == max(0, m["on_hand"] - m["locked"] - m["reserved_crm"] - m["order_hold"])
    # 安全库存 = ceil(日均7d × 交期 × 0.3)
    import math
    assert m["safety_stock"] == math.ceil(m["daily_avg_7d"] * m["lead_time_days"] * 0.3)
    # 预计售罄 = (可用+7d在途)/日均
    if m["daily_avg_7d"] > 0:
        expected = round((m["available"] + m["in_transit_7d"]) / m["daily_avg_7d"], 2)
        assert m["sell_through_days"] == pytest.approx(expected, abs=0.05)


def test_metadata_sales_rate(client):
    meta = client.get("/api/v1/metadata/metrics", headers=TOKEN).json()
    assert "动销率" in [m["name"] for m in meta["metrics"]]
    assert meta["prompt_context"]
    sr = client.get("/api/v1/metrics/sales-rate", headers=TOKEN).json()
    assert 0 <= sr["sales_rate"] <= 1
    assert sr["total_skus"] > 0


def test_cache_hit(client):
    first = client.get("/api/v1/skus/ST-01-C1-M/inventory", headers=TOKEN).json()
    second = client.get("/api/v1/skus/ST-01-C1-M/inventory", headers=TOKEN).json()
    assert first["cached"] is False
    assert second["cached"] is True


def test_cache_ttl_jitter_bounds():
    from data_platform.api import cache

    samples = [cache.effective_ttl(300) for _ in range(100)]
    assert all(300 <= s <= 360 for s in samples)
    assert len(set(samples)) > 5


def test_approval_flow(client):
    high = client.post("/api/v1/erp/replenish-orders", headers=TOKEN,
                       json={"sku_id": "ST-01-C1-M", "qty": 500, "reason": "补货测试"}).json()
    assert high["status"] == "pending_confirmation" and high["high_risk"] is True

    pending = client.get("/api/v1/approvals?status=pending", headers=TOKEN).json()
    ids = [i["approval_id"] for i in pending["items"]]
    assert high["approval_id"] in ids

    approved = client.post(f"/api/v1/approvals/{high['approval_id']}/approve", headers=TOKEN,
                           json={"approver": "admin"}).json()
    assert approved["status"] == "approved"
    assert approved["erp_status"] == "approved"

    # 已确认后仍保留在 4 小时列表里，且带审批账号/确认时间/当前库存快照
    all_rows = client.get("/api/v1/approvals?status=all&within_hours=4", headers=TOKEN).json()
    assert all_rows["window_hours"] == 4
    row = [i for i in all_rows["items"] if i["approval_id"] == high["approval_id"]][0]
    assert row["status"] == "approved"
    assert row["approver"] == "admin"
    assert row["decided_at"]
    assert row["current_available"] is not None
    assert row["current_on_hand"] is not None
    assert row["erp_status"] == "approved"

    low = client.post("/api/v1/erp/replenish-orders", headers=TOKEN,
                      json={"sku_id": "ST-01-C1-M", "qty": 20, "reason": "小单"}).json()
    assert low["status"] == "auto_approved"

    rejected_case = client.post("/api/v1/erp/replenish-orders", headers=TOKEN,
                                json={"sku_id": "ST-01-C1-M", "qty": 600, "reason": "驳回测试"}).json()
    rejected = client.post(f"/api/v1/approvals/{rejected_case['approval_id']}/reject", headers=TOKEN,
                           json={"approver": "admin", "reason": "超预算"}).json()
    assert rejected["status"] == "rejected"


def test_replenish_idempotency(client):
    """同一 idempotency_key 重复提交只建一单；换 key 才会新建。"""
    from sqlalchemy import func, select

    from data_platform.db import ApprovalRequest, session_scope

    payload = {"sku_id": "ST-01-C1-M", "qty": 500, "reason": "网络重试",
               "idempotency_key": "idem-test-001"}

    def _count(key: str) -> int:
        with session_scope() as session:
            return session.scalar(
                select(func.count()).select_from(ApprovalRequest)
                .where(ApprovalRequest.idempotency_key == key)
            )

    first = client.post("/api/v1/erp/replenish-orders", headers=TOKEN, json=payload).json()
    second = client.post("/api/v1/erp/replenish-orders", headers=TOKEN, json=payload).json()
    assert first["deduped"] is False
    assert second["deduped"] is True
    assert second["approval_id"] == first["approval_id"]
    assert second["po_no"] == first["po_no"]
    assert second["status"] == first["status"]
    assert _count("idem-test-001") == 1

    other = client.post("/api/v1/erp/replenish-orders", headers=TOKEN,
                        json={**payload, "idempotency_key": "idem-test-002"}).json()
    assert other["deduped"] is False and other["approval_id"] != first["approval_id"]

    # 不带幂等键的调用保持原有行为（每次新建，便于人工演示）
    legacy = client.post("/api/v1/erp/replenish-orders", headers=TOKEN,
                         json={"sku_id": "ST-01-C1-M", "qty": 30, "reason": "无键"}).json()
    assert legacy["deduped"] is False


def test_ensure_columns_light_migration(tmp_path):
    """既有演示库（缺列）启动时补列，且重复执行无副作用。"""
    from sqlalchemy import inspect, text

    from data_platform.db import ensure_columns, make_engine

    engine = make_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE approval_request ("
            "id VARCHAR(36) PRIMARY KEY, type VARCHAR(16), sku_id VARCHAR(32), "
            "qty INTEGER, amount FLOAT, reason VARCHAR(256), status VARCHAR(24), "
            "requested_by VARCHAR(32), approver VARCHAR(32), created_at DATETIME, "
            "decided_at DATETIME, erp_order_id VARCHAR(32))"
        ))

    added = ensure_columns(engine)
    assert added == ["approval_request.idempotency_key"]
    cols = {c["name"] for c in inspect(engine).get_columns("approval_request")}
    assert "idempotency_key" in cols
    assert ensure_columns(engine) == []
