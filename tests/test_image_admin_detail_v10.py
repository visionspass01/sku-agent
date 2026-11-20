"""第四轮 W8：管理端详情/列表/卡片/配额/批次的口径一致性（P2-1/P2-2/P2-3/P2-4/P2-10）。"""

from __future__ import annotations

import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import quota as quota_store
from app.agents_image.store import usage as usage_store
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


def _wait(env, headers, task_id, timeout=30.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail.get("status") in {"done", "failed", "canceled"} and \
                detail.get("backfill_status") != "pending":
            return detail
        time.sleep(0.05)
    return detail


def _done(env, headers) -> dict:
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图",
                             "slot_count": 3, "image_count": 2})
    assert created.status_code == 201, created.text
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert detail["status"] == "done"
    detail["task_id"] = created.json()["tasks"][0]["task_id"]
    return detail


# ---------------------------------------------------------------- P2-1：详情补字段
def test_admin_task_detail_has_v10_fields(env):
    headers = login(env)
    detail = _done(env, headers)
    body = env.get(f"/api/image/admin/tasks/{detail['task_id']}", headers=headers).json()
    for key in ("warnings", "blocked", "copy", "expected_images", "partial",
                "ref_source", "retries", "provider_calls", "reused_images",
                "base_task_id", "in_progress", "slot_count", "image_count"):
        assert key in body, key
    assert body["ref_source"] == "mcp"
    assert body["provider_calls"] >= 1
    assert body["expected_images"] == body["image_count"]
    assert body["in_progress"] is False


# ---------------------------------------------------------------- P2-3：列表 limit 夹紧 + 补字段
def test_admin_tasks_clamps_limit_and_adds_fields(env):
    headers = login(env)
    _done(env, headers)
    resp = env.get("/api/image/admin/tasks?limit=99999", headers=headers)
    assert resp.status_code == 200
    rows = resp.json()["tasks"]
    assert len(rows) <= 200
    for key in ("image_count", "slot_count", "ref_source", "reused_images",
                "retries", "provider_calls"):
        assert key in rows[0], key


def test_admin_tasks_zero_limit_is_clamped_up(env):
    headers = login(env)
    resp = env.get("/api/image/admin/tasks?limit=0", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()["tasks"]) <= 1


# ---------------------------------------------------------------- P2-2：部门口径一致
def test_cards_and_buckets_respect_department(env):
    """用一个专属部门名 + 专属账号隔离计数（测试库是 session 级的，固定部门会被别的用例污染）。"""
    import uuid

    headers = login(env)
    department = f"测试部-{uuid.uuid4().hex[:6]}"
    first = f"u-dept-a-{uuid.uuid4().hex[:8]}"
    second = f"u-dept-b-{uuid.uuid4().hex[:8]}"
    with session_scope() as session:
        user_store.create_user(session, username=first, password="pw-dept-123",
                               department=department)
        user_store.create_user(session, username=second, password="pw-dept-123",
                               department=department)
        quota_store.grant(session, first, period_type="day", period_limit=10)
        quota_store.grant(session, second, period_type="day", period_limit=20)
        usage_store.record(session, username=first, department=department,
                           delivered=2, attempted=2, tasks=1, agent_passed=2)
        usage_store.record(session, username=second, department=department,
                           delivered=5, attempted=5, tasks=1, agent_passed=5)

    body = env.get(f"/api/image/admin/metrics?granularity=day&count=7&department={department}&refresh=1",
                   headers=headers).json()
    assert body["cards"]["usage_images"] == 7
    assert body["cards"]["quota_limit"] == 30, "配额上限必须按部门聚合"
    bucket_users = {name for bucket in body["buckets"] for name in bucket["by_user"]}
    assert bucket_users == {first, second}

    # 别的部门不能看到这两个账号的数据
    other = env.get("/api/image/admin/metrics?granularity=day&count=7&department=男装&refresh=1",
                    headers=headers).json()
    other_users = {name for bucket in other["buckets"] for name in bucket["by_user"]}
    assert first not in other_users and second not in other_users
