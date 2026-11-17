"""中台侧身份与作用域（2.2-1 / 2.2-2 / 2.3 的验收测试）。

关注点：**中台自己**能不能挡住越权，而不是依赖 Agent 侧自觉。
- 令牌 → 主体（agent_id / scopes / departments）
- 缺 scope → 403（而不是靠 Agent 不调用）
- 跨部门 SKU → 403；审批列表按部门过滤；动销率按可见范围聚合
- 兼容：单一 `PLATFORM_INTERNAL_TOKEN` 仍是全权主体
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

LEGACY = {"X-Internal-Token": "demo-internal-token"}
MEN = {"X-Internal-Token": "k-mens"}
FULL = {"X-Internal-Token": "k-sku"}
OPS = {"X-Internal-Token": "k-ops"}

MEN_SKU = "ST-05-C1-M"      # 男装
WOMEN_SKU = "ST-01-C1-M"    # 女装
KIDS_SKU = "ST-15-C1-M"     # 童装（归女装部门）


@pytest.fixture(scope="module")
def platform_db(tmp_path_factory):
    import datetime as dt

    from data_platform.api import cache as pcache
    from data_platform.db import init_schema, make_engine, set_engine
    from data_platform.ingest.airflow_mock import run_dag
    from data_platform.mock_sources import generate_all

    tmp = tmp_path_factory.mktemp("platform_authz")
    engine = make_engine(f"sqlite:///{(tmp / 'platform.db').as_posix()}")
    set_engine(engine)
    init_schema(engine)
    pcache.reset_client()
    generate_all(snapshot_date=dt.date(2026, 11, 3), seed=7)
    result = run_dag("daily_sync")
    assert all(t["status"] == "success" for t in result["tasks"].values()), result
    yield engine
    set_engine(None)


@pytest.fixture(autouse=True)
def _principals(monkeypatch):
    """每个用例都用同一套 key 配置（含一个只读男装 Agent 与一个运维 Agent）。"""
    from data_platform import config
    from data_platform.api import authz, quota

    keys = {
        "sku-agent": {"key": "k-sku", "scopes": ["read", "write", "approve"], "departments": None},
        "analysis-agent": {"key": "k-mens", "scopes": ["read"], "departments": ["男装"]},
        # 有写权限但只限男装：用来验证「scope 过了，部门还要再挡一层」
        "mens-operator": {"key": "k-mens-op", "scopes": ["read", "write", "approve"],
                          "departments": ["男装"]},
        "ops-agent": {"key": "k-ops", "scopes": ["read", "etl"], "departments": None},
    }
    monkeypatch.setattr(config, "API_KEYS_RAW", json.dumps(keys, ensure_ascii=False))
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 600)
    authz.reset_principals()
    quota.reset()
    yield
    authz.reset_principals()
    quota.reset()


@pytest.fixture()
def client(platform_db):
    from data_platform.api.main import app

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------- 认证
def test_health_is_public_but_api_needs_token(client):
    assert client.get("/api/v1/health").status_code == 200
    assert client.get(f"/api/v1/skus/{MEN_SKU}/metrics").status_code == 401


def test_unknown_token_is_401(client):
    bad = {"X-Internal-Token": "not-a-key"}
    assert client.get(f"/api/v1/skus/{MEN_SKU}/metrics", headers=bad).status_code == 401


def test_legacy_internal_token_still_works(client):
    """向后兼容：单一内部令牌 = 全权主体，既有部署/演示零改动。"""
    for path in (f"/api/v1/skus/{MEN_SKU}/metrics", f"/api/v1/skus/{WOMEN_SKU}/metrics",
                 "/api/v1/approvals?status=all"):
        assert client.get(path, headers=LEGACY).status_code == 200


def test_bearer_header_also_accepted(client):
    resp = client.get(f"/api/v1/skus/{MEN_SKU}/metrics",
                      headers={"Authorization": "Bearer k-mens"})
    assert resp.status_code == 200


# ---------------------------------------------------------------- scope
def test_read_only_agent_cannot_write_or_approve(client):
    created = client.post("/api/v1/erp/replenish-orders", headers=FULL,
                          json={"sku_id": MEN_SKU, "qty": 500, "reason": "scope 测试"}).json()
    assert created["status"] == "pending_confirmation"

    resp = client.post("/api/v1/erp/replenish-orders", headers=MEN,
                       json={"sku_id": MEN_SKU, "qty": 10, "reason": "越权写"})
    assert resp.status_code == 403
    assert "scope=write" in resp.json()["detail"]

    resp = client.post(f"/api/v1/approvals/{created['approval_id']}/approve",
                       headers=MEN, json={"approver": "worker1"})
    assert resp.status_code == 403
    assert "scope=approve" in resp.json()["detail"]


def test_etl_endpoints_require_etl_scope(client):
    assert client.get("/api/v1/etl/runs", headers=FULL).status_code == 403
    assert client.get("/api/v1/etl/runs", headers=OPS).status_code == 200
    assert client.get("/api/v1/audit", headers=FULL).status_code == 403
    assert client.get("/api/v1/audit", headers=OPS).status_code == 200


def test_metadata_departments_is_single_source_of_truth(client):
    body = client.get("/api/v1/metadata/departments", headers=FULL).json()
    assert body["departments"]["男装"] == ["男装"]
    assert set(body["departments"]["女装"]) == {"女装", "童装"}


# ---------------------------------------------------------------- 部门作用域
def test_department_scope_blocks_cross_department_reads(client):
    assert client.get(f"/api/v1/skus/{MEN_SKU}/inventory", headers=MEN).status_code == 200
    for sku in (WOMEN_SKU, KIDS_SKU):
        resp = client.get(f"/api/v1/skus/{sku}/inventory", headers=MEN)
        assert resp.status_code == 403, (sku, resp.status_code, resp.text)
        assert "只能访问" in resp.json()["detail"]


def test_all_sku_endpoints_enforce_department(client):
    for path in ("inventory", "sales", "in-transit", "reservations", "orders",
                 "metrics", "sell-through"):
        assert client.get(f"/api/v1/skus/{WOMEN_SKU}/{path}", headers=MEN).status_code == 403, path


def test_unknown_sku_is_404_not_403(client):
    """部门校验发生在"取到指标行"之后：未知 SKU 应该是 404，别把存在性藏成越权。"""
    assert client.get("/api/v1/skus/ST-99-C9-X/inventory", headers=MEN).status_code == 404


def test_approvals_are_filtered_by_department(client):
    client.post("/api/v1/erp/replenish-orders", headers=FULL,
                json={"sku_id": MEN_SKU, "qty": 500, "reason": "男装待批"})
    client.post("/api/v1/erp/replenish-orders", headers=FULL,
                json={"sku_id": WOMEN_SKU, "qty": 500, "reason": "女装待批"})

    mine = client.get("/api/v1/approvals?status=all", headers=MEN).json()
    assert mine["scope_filtered"] is True and mine["scope_dropped"] >= 1
    assert all(item["department"] == "男装" for item in mine["items"])

    everything = client.get("/api/v1/approvals?status=all", headers=FULL).json()
    assert everything["scope_filtered"] is False
    assert {i["sku_id"] for i in everything["items"]} >= {MEN_SKU, WOMEN_SKU}


def test_sales_rate_respects_scope(client):
    resp = client.get("/api/v1/metrics/sales-rate?department=女装", headers=MEN)
    assert resp.status_code == 403

    mine = client.get("/api/v1/metrics/sales-rate", headers=MEN).json()
    assert mine["effective_departments"] == ["男装"]
    assert mine["total_skus"] > 0

    full = client.get("/api/v1/metrics/sales-rate", headers=FULL).json()
    assert full["effective_departments"] is None
    assert full["total_skus"] > mine["total_skus"]   # 全量应该比单部门多


def test_scope_scoped_cache_does_not_leak(client):
    """缓存 key 必须带作用域：两个主体查同一接口不能互相污染。"""
    from data_platform.api import cache as pcache

    pcache.reset_client()
    mine = client.get("/api/v1/metrics/sales-rate", headers=MEN).json()
    full = client.get("/api/v1/metrics/sales-rate", headers=FULL).json()
    assert mine["total_skus"] != full["total_skus"]
    again = client.get("/api/v1/metrics/sales-rate", headers=MEN).json()
    assert again["total_skus"] == mine["total_skus"]
    assert again["cached"] is True


def test_write_is_scoped_to_department(client):
    """scope=write 通过之后，部门作用域仍要再挡一层（两个维度是独立的）。"""
    ok = client.post("/api/v1/erp/replenish-orders", headers=FULL,
                     json={"sku_id": WOMEN_SKU, "qty": 10, "reason": "全权写入"})
    assert ok.status_code == 200

    denied = client.post("/api/v1/erp/replenish-orders",
                         headers={"X-Internal-Token": "k-mens-op"},
                         json={"sku_id": WOMEN_SKU, "qty": 10, "reason": "越权写"})
    assert denied.status_code == 403
    assert "只能访问" in denied.json()["detail"]

    mine = client.post("/api/v1/erp/replenish-orders",
                       headers={"X-Internal-Token": "k-mens-op"},
                       json={"sku_id": MEN_SKU, "qty": 10, "reason": "本部门写入"})
    assert mine.status_code == 200


def test_approve_is_scoped_to_department(client):
    created = client.post("/api/v1/erp/replenish-orders", headers=FULL,
                          json={"sku_id": WOMEN_SKU, "qty": 500, "reason": "女装待批"}).json()
    resp = client.post(f"/api/v1/approvals/{created['approval_id']}/approve",
                       headers={"X-Internal-Token": "k-mens-op"},
                       json={"approver": "worker1"})
    assert resp.status_code == 403
    assert "只能访问" in resp.json()["detail"]


def test_transfer_suggestion_is_scoped(client):
    assert client.post("/api/v1/transfer-suggestions", headers=MEN,
                       json={"sku_id": WOMEN_SKU}).status_code == 403
    assert client.post("/api/v1/transfer-suggestions", headers=MEN,
                       json={"sku_id": MEN_SKU}).status_code == 200
