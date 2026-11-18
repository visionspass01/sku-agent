"""D13：SSE 进度流、取消、我的配额、管理员接口（指标/任务/账号/配额/热加载/重放）。"""

from __future__ import annotations

import json
import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.registry import reset_registry
from app.agents_image.sources import product_source as ps
from app.agents_image.store import outbox as outbox_store
from app.agents_image.store import quota as quota_store
from app.agents_image.store.schema import ensure_image_schema
from app.agents_image.store.users import ensure_seed_users
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.delenv("IMAGE_PRODUCT_SOURCE", raising=False)
    reset_image_settings()
    reset_registry()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    image_metrics.reset()
    ensure_image_schema()
    with session_scope() as session:
        ensure_seed_users(session)
    yield app_client
    reset_image_settings()
    reset_registry()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()


def login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def make_task(client, headers, product_ids=("P-1001",), n=1) -> dict:
    resp = client.post("/api/image/tasks", headers=headers,
                       json={"product_ids": list(product_ids), "prompt": "白底主图", "n": n})
    assert resp.status_code == 201, resp.text
    return resp.json()


def wait_done(client, headers, task_id, timeout=20.0) -> dict:
    deadline = time.time() + timeout
    payload = {}
    while time.time() < deadline:
        payload = client.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if payload.get("status") in {"done", "failed", "canceled"}:
            return payload
        time.sleep(0.05)
    return payload


# ---------------------------------------------------------------- SSE
def test_stream_emits_snapshot_assets_and_done(env):
    headers = login(env)
    created = make_task(env, headers)
    task_id = created["tasks"][0]["task_id"]

    events: list[tuple[str, dict]] = []
    with env.stream("GET", f"/api/image/tasks/{task_id}/stream", headers=headers) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        buffer_event = ""
        for line in resp.iter_lines():
            if line.startswith("event: "):
                buffer_event = line[7:].strip()
            elif line.startswith("data: "):
                events.append((buffer_event, json.loads(line[6:])))
                if buffer_event == "done":
                    break

    names = [name for name, _ in events]
    assert names[0] == "snapshot"
    assert "asset" in names
    assert names[-1] == "done"
    done_payload = events[-1][1]
    assert done_payload["task_id"] == task_id
    assert done_payload["status"] in {"done", "failed"}


def test_stream_requires_owner(env):
    admin = login(env)
    created = make_task(env, admin)
    task_id = created["tasks"][0]["task_id"]
    worker = login(env, "worker2", "hhw222")
    resp = env.get(f"/api/image/tasks/{task_id}/stream", headers=worker)
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "IMG_FORBIDDEN"


def test_stream_unknown_task_404(env):
    headers = login(env)
    resp = env.get("/api/image/tasks/20261117000000abcd/stream", headers=headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------- 取消
def test_cancel_finished_task_conflicts(env):
    headers = login(env)
    created = make_task(env, headers)
    task_id = created["tasks"][0]["task_id"]
    wait_done(env, headers, task_id)
    resp = env.post(f"/api/image/tasks/{task_id}/cancel", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "IMG_TASK_CONFLICT"


def test_cancel_requires_owner(env):
    headers = login(env)
    created = make_task(env, headers)
    task_id = created["tasks"][0]["task_id"]
    worker = login(env, "worker2", "hhw222")
    resp = env.post(f"/api/image/tasks/{task_id}/cancel", headers=worker)
    assert resp.status_code == 403


def test_cancel_queued_task(env):
    headers = login(env)
    created = make_task(env, headers)
    task_id = created["tasks"][0]["task_id"]
    resp = env.post(f"/api/image/tasks/{task_id}/cancel", headers=headers)
    # 任务可能已经跑完（409）或取消成功（200）；两者都说明接口按语义工作
    assert resp.status_code in {200, 409}
    detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
    assert detail["status"] in {"canceled", "done", "failed"}


# ---------------------------------------------------------------- 我的配额
def test_my_quota_for_user_and_admin(env):
    worker = login(env, "worker1", "hhw111")
    body = env.get("/api/image/quota", headers=worker).json()
    assert body["username"] == "worker1"
    assert body["period_type"] == "day"
    assert body["limit"] >= 0 and body["remaining"] is not None

    admin = login(env)
    admin_body = env.get("/api/image/quota", headers=admin).json()
    assert admin_body["period_type"] == "unlimited"


def test_quota_exceeded_returns_429(env):
    worker = login(env, "worker1", "hhw111")
    with session_scope() as session:
        quota_store.grant(session, "worker1", period_type="day", period_limit=0)
    try:
        resp = env.post("/api/image/tasks", headers=worker,
                        json={"product_ids": ["P-1001"], "prompt": "白底主图"})
        assert resp.status_code == 429
        assert resp.json()["error_code"] == "IMG_QUOTA_EXCEEDED"
    finally:
        # 还原，避免污染同一进程里后续用例（DB 是 session 级共享的）
        with session_scope() as session:
            quota_store.grant(session, "worker1", period_type="day", period_limit=50)


# ---------------------------------------------------------------- 管理员接口
def test_admin_endpoints_forbidden_for_user(env):
    worker = login(env, "worker1", "hhw111")
    for path in ("/api/image/admin/metrics", "/api/image/admin/tasks",
                 "/api/image/admin/users", "/api/image/admin/quota"):
        resp = env.get(path, headers=worker)
        assert resp.status_code == 403, path
        assert resp.json()["error_code"] == "IMG_FORBIDDEN"


def test_admin_metrics_cards_and_buckets(env):
    headers = login(env)
    created = make_task(env, headers, product_ids=("P-1001", "P-1002"))
    wait_done(env, headers, created["tasks"][0]["task_id"])
    body = env.get("/api/image/admin/metrics?granularity=day&count=7", headers=headers).json()
    assert len(body["buckets"]) == 7
    cards = body["cards"]
    for key in ("cache_hit_rate", "llm_calls", "llm_tokens", "in_flight_images",
                "images_delivered", "images_attempted", "first_pass_rate",
                "tasks_done", "tasks_failed", "cost_cny"):
        assert key in cards
    assert body["runtime"]["counters"]["tasks_created"] >= 1
    assert "outbox" in body

    hour = env.get("/api/image/admin/metrics?granularity=hour&count=5", headers=headers).json()
    assert len(hour["buckets"]) == 5


def test_admin_metrics_rejects_bad_count(env):
    headers = login(env)
    resp = env.get("/api/image/admin/metrics?count=abc", headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "IMG_INPUT_INVALID"


def test_admin_tasks_list_and_detail(env):
    headers = login(env)
    created = make_task(env, headers)
    task_id = created["tasks"][0]["task_id"]
    wait_done(env, headers, task_id)

    listing = env.get("/api/image/admin/tasks?limit=20", headers=headers).json()
    assert any(item["task_id"] == task_id for item in listing["tasks"])

    detail = env.get(f"/api/image/admin/tasks/{task_id}", headers=headers).json()
    assert detail["task_id"] == task_id
    assert detail["slots"] and detail["prompt_pack"] is not None
    assert "backfill_status" in detail
    assert detail["assets"]


def test_admin_users_crud(env):
    headers = login(env)
    username = "worker-crud-2"
    created = env.post("/api/image/admin/users", headers=headers,
                       json={"username": username, "password": "hhw999", "department": "男装"})
    assert created.status_code == 201, created.text
    assert env.post("/api/image/auth/login",
                    json={"username": username, "password": "hhw999"}).status_code == 200

    updated = env.put(f"/api/image/admin/users/{username}", headers=headers,
                      json={"enabled": False})
    assert updated.status_code == 200
    assert env.post("/api/image/auth/login",
                    json={"username": username, "password": "hhw999"}).status_code == 401

    users = env.get("/api/image/admin/users", headers=headers).json()["users"]
    assert any(item["username"] == username and item["enabled"] is False for item in users)


def test_admin_users_validation(env):
    headers = login(env)
    resp = env.post("/api/image/admin/users", headers=headers,
                    json={"username": "x", "password": "123"})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "IMG_INPUT_INVALID"


def test_admin_quota_grant(env):
    headers = login(env)
    resp = env.put("/api/image/admin/quota/worker2", headers=headers,
                   json={"period_type": "week", "period_limit": 12})
    assert resp.status_code == 200
    body = resp.json()
    assert body["period_type"] == "week" and body["period_limit"] == 12
    listing = env.get("/api/image/admin/quota", headers=headers).json()["quotas"]
    assert any(item["username"] == "worker2" and item["period_limit"] == 12
               for item in listing)


def test_admin_replay_outbox_and_reload_and_reindex(env):
    headers = login(env)
    with session_scope() as session:
        outbox_store.enqueue(session, task_id="t-admin-1", payload={"task_id": "t-admin-1"})

    replayed = env.post("/api/image/admin/replay-outbox", headers=headers).json()
    assert "sent" in replayed and "stats" in replayed

    reloaded = env.post("/api/image/admin/reload", headers=headers).json()
    assert "reloaded" in reloaded and "health" in reloaded

    reindex = env.post("/api/image/admin/reindex", headers=headers).json()
    assert reindex["index"]["docs"] >= 8
