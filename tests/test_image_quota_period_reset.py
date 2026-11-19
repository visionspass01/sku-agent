"""第四轮 W8：配额周期切换必须同时清 `used` 与 `reserved`（P2-4）。

过去只清 `used`：上一周期没结算完的预留继续占着额度，新周期 `used + reserved <= limit`
凭空少一截（用户会以为"额度莫名其妙没了"）。
"""

from __future__ import annotations

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import quota as quota_store
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    monkeypatch.delenv("IMAGE_PRODUCT_SOURCE", raising=False)
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()
    image_metrics.reset()
    ensure_image_schema()
    with session_scope() as session:
        user_store.ensure_seed_users(session)
    yield app_client
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()


def login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def test_grant_period_switch_clears_used_and_reserved(env):
    with session_scope() as session:
        quota_store.grant(session, "worker2", period_type="day", period_limit=10)
        quota_store.reserve(session, "worker2", 4)
        quota_store.consume(session, "worker2", 2)
        before = quota_store.check(session, "worker2", 0)
        assert before["remaining"] == 10 - 2 - 4

        quota_store.grant(session, "worker2", period_type="week", period_limit=30)
        after = quota_store.check(session, "worker2", 0)
    assert after["period_type"] == "week"
    assert after["remaining"] == 30, "换周期必须把 used 与 reserved 一起归零"


def test_rollover_clears_reserved(env):
    with session_scope() as session:
        row = quota_store.get_or_create(session, "worker1")
        row.period_key = "2000-01-01"          # 模拟上一周期
        row.used = 3
        row.reserved = 5
        session.flush()
        state = quota_store.check(session, "worker1", 0)
    assert state["period_key"] != "2000-01-01"
    assert state["remaining"] == state["limit"], "滚周期后预留不能继续占额度"


def test_admin_grant_api_reports_full_remaining(env):
    headers = login(env)
    with session_scope() as session:
        quota_store.grant(session, "worker1", period_type="day", period_limit=10)
        quota_store.reserve(session, "worker1", 6)
    resp = env.put("/api/image/admin/quota/worker1", headers=headers,
                   json={"period_type": "month", "period_limit": 100})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["period_type"] == "month"
    assert body["remaining"] == 100, "换周期后剩余额度必须是完整上限（预留已清）"
