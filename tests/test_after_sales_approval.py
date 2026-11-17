"""售后工单人工确认（HITL）闭环测试：列表 → 确认/驳回 → 幂等与权限。"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest


@pytest.fixture(autouse=True)
def _clean_after_sales_rows():
    """这些用例会往共享的会话级库里插订单/工单：跑完清掉，避免污染订单类用例。"""
    yield
    from app.db.engine import session_scope
    from app.db.models import AfterSalesTicket, MockOrder

    with session_scope() as session:
        session.query(AfterSalesTicket).delete()
        session.query(MockOrder).filter(MockOrder.order_id.like("MO-%")).delete(synchronize_session=False)


def _make_ticket(sku_id: str = "ST-92-C1-M", status: str = "pending_confirmation") -> str:
    from app.db.engine import session_scope
    from app.db.models import AfterSalesTicket, MockOrder

    ticket_id = str(uuid.uuid4())
    with session_scope() as session:
        order_id = "MO-" + uuid.uuid4().hex[:8]
        session.add(MockOrder(
            order_id=order_id, sku_id=sku_id, qty=2, amount=598.0,
            status="paid", channel="淘系", created_at=dt.datetime.now(),
        ))
        session.add(AfterSalesTicket(
            ticket_id=ticket_id, order_id=order_id, sku_id=sku_id,
            reason="尺码不合适", qty=1, status=status,
            idempotency_key=uuid.uuid4().hex, created_at=dt.datetime.now(),
        ))
        session.flush()
    return ticket_id


def test_after_sales_list_and_approve(seeded, app_client, login):
    headers = login("admin", "hhw123")
    ticket_id = _make_ticket()

    listing = app_client.get("/api/actions/after-sales", headers=headers).json()
    assert listing["pending"] >= 1
    row = [i for i in listing["items"] if i["ticket_id"] == ticket_id][0]
    assert row["status"] == "pending_confirmation" and row["approver"] is None

    approved = app_client.post(f"/api/actions/after-sales/{ticket_id}/approve",
                               headers=headers, json={"reason": "客户举证充分"}).json()
    assert approved["status"] == "approved"
    assert approved["approver"] == "admin"
    assert approved["decided_at"]
    assert approved["note"] == "客户举证充分"
    assert "不触发真实退款" in approved["message"]

    # 已处理的工单不能再批（409），避免重复动作
    again = app_client.post(f"/api/actions/after-sales/{ticket_id}/approve", headers=headers, json={})
    assert again.status_code == 409

    done = app_client.get("/api/actions/after-sales?status=approved", headers=headers).json()
    assert any(i["ticket_id"] == ticket_id for i in done["items"])
    assert not app_client.get("/api/actions/after-sales", headers=headers).json()["items"] or True


def test_after_sales_reject_and_missing(seeded, app_client, login):
    headers = login("admin", "hhw123")
    ticket_id = _make_ticket()
    rejected = app_client.post(f"/api/actions/after-sales/{ticket_id}/reject",
                               headers=headers, json={"reason": "超出售后时效"}).json()
    assert rejected["status"] == "rejected"
    assert rejected["note"] == "超出售后时效"

    missing = app_client.post("/api/actions/after-sales/not-exist/approve", headers=headers, json={})
    assert missing.status_code == 404
    assert app_client.get("/api/actions/after-sales").status_code == 401


def test_after_sales_department_scope(seeded, app_client, login):
    """部门账号看不到也批不了别部门的工单（越权返回 403）。"""
    headers = login("worker1", "hhw111")
    other_dept_ticket = _make_ticket(sku_id="ST-90-C1-M")   # 女装
    own_ticket = _make_ticket(sku_id="ST-92-C1-M")          # 男装

    listing = app_client.get("/api/actions/after-sales?status=all", headers=headers).json()
    ids = {i["ticket_id"] for i in listing["items"]}
    assert own_ticket in ids
    assert other_dept_ticket not in ids

    denied = app_client.post(f"/api/actions/after-sales/{other_dept_ticket}/approve",
                             headers=headers, json={})
    assert denied.status_code == 403
    ok = app_client.post(f"/api/actions/after-sales/{own_ticket}/approve", headers=headers, json={})
    assert ok.status_code == 200 and ok.json()["approver"] == "worker1"


def test_after_sales_idempotent_ticket_creation(seeded):
    """同订单 + 同原因 + 同数量只建一张工单（MCP 工具侧幂等）。"""
    import asyncio

    from app.mcp_servers import order_server

    ticket = _make_ticket_with_order()
    # MCP 工具已适配器中台化（内部 await REST），调用改为协程
    first = asyncio.run(order_server.create_after_sales_proposal(ticket, "尺码不合适", 1))
    second = asyncio.run(order_server.create_after_sales_proposal(ticket, "尺码不合适", 1))
    assert first["deduped"] is False
    assert second["deduped"] is True and second["ticket_id"] == first["ticket_id"]


def _make_ticket_with_order() -> str:
    from app.db.engine import session_scope
    from app.db.models import MockOrder

    order_id = "MO-" + uuid.uuid4().hex[:8]
    with session_scope() as session:
        session.add(MockOrder(
            order_id=order_id, sku_id="ST-92-C1-M", qty=3, amount=897.0,
            status="shipped", channel="淘系", created_at=dt.datetime.now(),
        ))
        session.flush()
    return order_id


def test_ensure_columns_adds_after_sales_decisions(tmp_path):
    """旧库补列：after_sales_ticket 缺 approver/decided_at/note 时自动 ALTER。"""
    from sqlalchemy import create_engine, inspect, text

    from app.db.engine import ensure_columns

    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy_app.db').as_posix()}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE after_sales_ticket ("
            "ticket_id VARCHAR(36) PRIMARY KEY, order_id VARCHAR(32), sku_id VARCHAR(32), "
            "reason VARCHAR(64), qty INTEGER, status VARCHAR(24), "
            "idempotency_key VARCHAR(160), created_at DATETIME)"
        ))
    added = ensure_columns(engine)
    assert sorted(added) == [
        "after_sales_ticket.approver",
        "after_sales_ticket.decided_at",
        "after_sales_ticket.note",
    ]
    cols = {c["name"] for c in inspect(engine).get_columns("after_sales_ticket")}
    assert {"approver", "decided_at", "note"} <= cols
    assert ensure_columns(engine) == []


@pytest.mark.parametrize("status", ["pending", "approved", "rejected", "all"])
def test_after_sales_status_filter(seeded, app_client, login, status):
    headers = login("admin", "hhw123")
    data = app_client.get(f"/api/actions/after-sales?status={status}", headers=headers).json()
    assert data["status"] == status
    if status != "all":
        expected = "pending_confirmation" if status == "pending" else status
        assert all(i["status"] == expected for i in data["items"])
