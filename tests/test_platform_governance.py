"""中台并发控制 / 配额 / 审计（2.2-3 / 2.2-5 / 2.2-8 + 修复表 1/2 的验收）。

三件事必须在中台侧成立：
1. **审批并发安全**：两个 Agent 同时确认同一单，只能有一个成功（条件 UPDATE，不是读-改-写）；
2. **幂等并发安全**：同一幂等键并发提交只建一单，且不出现 500；
3. **配额与审计**：按调用方限流、每次调用留痕（谁、要什么权限、结果、请求 id）。
"""

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

FULL = {"X-Internal-Token": "k-sku"}
OPS = {"X-Internal-Token": "k-ops"}
TINY = {"X-Internal-Token": "k-tiny"}
MEN_SKU = "ST-05-C1-M"
WOMEN_SKU = "ST-01-C1-M"


@pytest.fixture(scope="module")
def platform_db(tmp_path_factory):
    import datetime as dt

    from data_platform.api import cache as pcache
    from data_platform.db import init_schema, make_engine, set_engine
    from data_platform.ingest.airflow_mock import run_dag
    from data_platform.mock_sources import generate_all

    tmp = tmp_path_factory.mktemp("platform_gov")
    engine = make_engine(f"sqlite:///{(tmp / 'platform.db').as_posix()}")
    set_engine(engine)
    init_schema(engine)
    pcache.reset_client()
    generate_all(snapshot_date=dt.date(2026, 11, 3), seed=11)
    result = run_dag("daily_sync")
    assert all(t["status"] == "success" for t in result["tasks"].values()), result
    yield engine
    set_engine(None)


@pytest.fixture(autouse=True)
def _principals(monkeypatch):
    from data_platform import config
    from data_platform.api import authz, quota

    keys = {
        "sku-agent": {"key": "k-sku", "scopes": ["read", "write", "approve"], "departments": None},
        "ops-agent": {"key": "k-ops", "scopes": ["read", "etl"], "departments": None},
        # 配额很小，用来验证 429
        "tiny-agent": {"key": "k-tiny", "scopes": ["read"], "departments": None, "rate_limit": 2},
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


def _pending(client: TestClient, sku_id: str = MEN_SKU, qty: int = 500) -> str:
    body = client.post("/api/v1/erp/replenish-orders", headers=FULL,
                       json={"sku_id": sku_id, "qty": qty, "reason": "并发测试"}).json()
    assert body["status"] == "pending_confirmation", body
    return body["approval_id"]


# ---------------------------------------------------------------- 审批并发
def test_concurrent_approve_only_one_wins(client):
    approval_id = _pending(client)
    results: list[tuple[int, str]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker(approver: str) -> None:
        barrier.wait(timeout=5)
        resp = client.post(f"/api/v1/approvals/{approval_id}/approve", headers=FULL,
                           json={"approver": approver})
        with lock:
            results.append((resp.status_code, approver))

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("alice", "bob")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    codes = sorted(code for code, _ in results)
    assert codes == [200, 409], results

    from data_platform.db import ApprovalRequest, ErpReplenishOrder, session_scope

    with session_scope() as session:
        row = session.get(ApprovalRequest, approval_id)
        po = session.get(ErpReplenishOrder, row.erp_order_id)
        assert row.status == "approved"
        # 只被写一次：审批人就是那个 200 的调用方，另一方的 approver 不能覆盖它
        winner = [name for code, name in results if code == 200][0]
        assert row.approver == winner
        assert po.status == "approved" and po.approved_at is not None
        decided_once = po.approved_at

    # 再确认一次仍然 409，且不会改写已确认记录
    again = client.post(f"/api/v1/approvals/{approval_id}/approve", headers=FULL,
                        json={"approver": "carol"})
    assert again.status_code == 409
    with session_scope() as session:
        row = session.get(ApprovalRequest, approval_id)
        po = session.get(ErpReplenishOrder, row.erp_order_id)
        assert row.approver == winner and po.approved_at == decided_once


def test_reject_after_approve_is_conflict(client):
    approval_id = _pending(client, qty=600)
    assert client.post(f"/api/v1/approvals/{approval_id}/approve", headers=FULL,
                       json={"approver": "alice"}).status_code == 200
    resp = client.post(f"/api/v1/approvals/{approval_id}/reject", headers=FULL,
                       json={"approver": "bob", "reason": "改主意"})
    assert resp.status_code == 409
    assert "approved" in resp.json()["detail"]


def test_approve_unknown_approval_is_404(client):
    assert client.post("/api/v1/approvals/no-such-id/approve", headers=FULL,
                       json={"approver": "alice"}).status_code == 404


# ---------------------------------------------------------------- 幂等并发
def test_concurrent_idempotent_create_never_500(client):
    from sqlalchemy import func, select

    from data_platform.db import ApprovalRequest, session_scope

    key = "idem-concurrent-001"
    payload = {"sku_id": MEN_SKU, "qty": 500, "reason": "并发幂等", "idempotency_key": key}
    out: list[dict] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait(timeout=5)
        try:
            resp = client.post("/api/v1/erp/replenish-orders", headers=FULL, json=payload)
            with lock:
                out.append({"status": resp.status_code, "body": resp.json()})
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert all(item["status"] == 200 for item in out), out        # 不允许 500
    assert len({item["body"]["approval_id"] for item in out}) == 1
    deduped = sorted(item["body"]["deduped"] for item in out)
    assert deduped == [False, True], out

    with session_scope() as session:
        count = int(session.scalar(
            select(func.count()).select_from(ApprovalRequest)
            .where(ApprovalRequest.idempotency_key == key)
        ) or 0)
    assert count == 1


# ---------------------------------------------------------------- 配额
def test_quota_returns_429_with_retry_after(client):
    assert client.get("/api/v1/metrics/sales-rate", headers=TINY).status_code == 200
    assert client.get("/api/v1/metrics/sales-rate", headers=TINY).status_code == 200
    blocked = client.get("/api/v1/metrics/sales-rate", headers=TINY)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert blocked.headers["X-RateLimit-Remaining"] == "0"
    # 别的调用方不受影响
    assert client.get("/api/v1/metrics/sales-rate", headers=OPS).status_code == 200


# ---------------------------------------------------------------- 审计与关联 id
def test_request_id_is_echoed_and_audited(client):
    headers = {**FULL, "X-Request-Id": "req-test-123"}
    resp = client.get(f"/api/v1/skus/{MEN_SKU}/inventory", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["X-Request-Id"] == "req-test-123"
    assert resp.headers["X-Agent-Id"] == "sku-agent"
    assert resp.headers["X-RateLimit-Limit"] == "600"

    rows = client.get("/api/v1/audit?limit=50", headers=OPS).json()["items"]
    match = [r for r in rows if r["request_id"] == "req-test-123"]
    assert match, rows[:3]
    assert match[0]["agent_id"] == "sku-agent"
    assert match[0]["decision"] == "allow"
    assert match[0]["status"] == 200
    assert "read" in match[0]["scopes"]


def test_audit_records_denials(client):
    client.get(f"/api/v1/skus/{MEN_SKU}/metrics")                                  # 401 无令牌
    client.post("/api/v1/erp/replenish-orders", headers=OPS,
                json={"sku_id": MEN_SKU, "qty": 1, "reason": "x"})                # 403 缺 write

    rows = client.get("/api/v1/audit?limit=50", headers=OPS).json()
    decisions = {r["decision"] for r in rows["items"]}
    assert "unauthenticated" in decisions
    assert "denied_scope" in decisions
    assert rows["by_decision"]["unauthenticated"] >= 1
    unauth = [r for r in rows["items"] if r["decision"] == "unauthenticated"]
    assert all(r["agent_id"] == "" for r in unauth)


def test_audit_records_department_denial(client):
    created = client.post("/api/v1/erp/replenish-orders", headers=FULL,
                          json={"sku_id": WOMEN_SKU, "qty": 10, "reason": "女装"}).json()
    assert created["caller"] == "sku-agent"

    # 临时注入一个「有读权限但只限男装」的主体（不改 fixture 的 key 配置）
    from data_platform.api import authz

    authz.principals()["k-mens-reader"] = authz.Principal("mens-reader", {"read"}, ("男装",), 600)

    resp = client.get(f"/api/v1/skus/{WOMEN_SKU}/metrics",
                      headers={"X-Internal-Token": "k-mens-reader"})
    assert resp.status_code == 403

    rows = client.get("/api/v1/audit?limit=50", headers=OPS).json()["items"]
    denied = [r for r in rows if r["decision"] == "denied_department"]
    assert denied and denied[0]["agent_id"] == "mens-reader"
    assert WOMEN_SKU in denied[0]["detail"]
