"""W6：功能12（批次取消/重试/汇总 + 管理端部门筛选）与功能13（画廊懒加载分页）。"""

from __future__ import annotations

import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import quota as quota_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
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


def _seed_batch(batch_id: str, *, username="admin", statuses=("queued", "queued"),
                department=None, platform="淘系") -> list[str]:
    """造一批任务（同批次内 product_id 必须不同：有 `uq_image_task_batch_target` 唯一索引）。"""
    products = ["P-3001", "P-2001", "P-1001", "P-3003"]
    ids = []
    with session_scope() as session:
        for index, status in enumerate(statuses):
            task_id = f"20261118{batch_id[-6:]}{index:02d}"
            task_store.create_task(session, task_id=task_id, username=username,
                                   department=department, batch_id=batch_id,
                                   prompt="白底主图", platform=platform, aspect="1:1",
                                   n=1, ref_asset_ids=[],
                                   product_id=products[index % len(products)],
                                   image_count=1)
            if status != "queued":
                task_store.update_task(session, task_id, status=status)
            ids.append(task_id)
    return ids


# ---------------------------------------------------------------- 批次取消
def test_cancel_batch_cancels_queued_tasks(env):
    headers = login(env)
    batch_id = "202611182300000abc"
    ids = _seed_batch(batch_id)
    resp = env.post("/api/image/tasks/cancel-batch", headers=headers,
                    json={"batch_id": batch_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["canceled"] == len(ids)
    with session_scope() as session:
        for task_id in ids:
            assert task_store.get_task(session, task_id).status == "canceled"


def test_cancel_batch_two_branch_semantics(env):
    """已提交模型的图位不能撤回：结果丢弃并明确告知，不谎称"已取消"。"""
    headers = login(env)
    batch_id = "202611182300000abd"
    ids = _seed_batch(batch_id, statuses=("running",))
    with session_scope() as session:
        task_store.update_task(session, ids[0], provider_started_at=__import__("datetime").datetime.now())
    resp = env.post("/api/image/tasks/cancel-batch", headers=headers,
                    json={"batch_id": batch_id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["discarded"] == 1
    with session_scope() as session:
        assert task_store.get_task(session, ids[0]).cancel_requested is True


def test_cancel_batch_requires_batch_id(env):
    headers = login(env)
    resp = env.post("/api/image/tasks/cancel-batch", headers=headers, json={})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "IMG_INPUT_INVALID"


# ---------------------------------------------------------------- 批次重试
def test_retry_batch_requeues_failed_and_skips_done(env, monkeypatch):
    headers = login(env, "worker2", "hhw222")
    with session_scope() as session:
        quota_store.grant(session, "worker2", period_type="day", period_limit=30)
    batch_id = "202611182300000abe"
    ids = _seed_batch(batch_id, username="worker2", department="女装",
                      statuses=("failed", "done", "queued"))
    submitted: list[str] = []

    class _StubDispatcher:
        async def submit(self, task_id: str) -> str:
            submitted.append(task_id)
            return task_id

    monkeypatch.setattr("app.agents_image.api.batch_routes.get_dispatcher",
                        lambda: _StubDispatcher())
    resp = env.post("/api/image/tasks/retry-batch", headers=headers,
                    json={"batch_id": batch_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retried"] == 1
    assert body["skipped"] == 2
    assert submitted == [ids[0]]


# ---------------------------------------------------------------- 批次汇总
def test_batch_detail_summarises(env):
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图",
                             "image_count": 1})
    assert created.status_code == 201, created.text
    batch_id = created.json()["batch_id"]
    task_id = created.json()["tasks"][0]["task_id"]
    assert _wait(env, headers, task_id)["status"] == "done"
    body = env.get(f"/api/image/batches/{batch_id}", headers=headers).json()
    assert body["batch_id"] == batch_id
    assert body["count"] == 1
    assert body["statuses"].get("done") == 1
    assert body["delivered_images"] >= 1
    assert body["ref_source"] == "mcp"
    assert body["products"] == ["P-3001"]
    assert body["tasks"][0]["task_id"] == task_id


def test_batch_detail_forbidden_for_others(env):
    _seed_batch("202611182300000abf", username="admin")
    worker = login(env, "worker1", "hhw111")
    resp = env.get("/api/image/batches/202611182300000abf", headers=worker)
    assert resp.status_code == 403


# ---------------------------------------------------------------- 管理端部门筛选
def test_admin_tasks_filter_by_department(env):
    admin = login(env)
    worker1 = login(env, "worker1", "hhw111")
    worker2 = login(env, "worker2", "hhw222")
    with session_scope() as session:
        quota_store.grant(session, "worker1", period_type="day", period_limit=10)
        quota_store.grant(session, "worker2", period_type="day", period_limit=10)
    male = env.post("/api/image/tasks", headers=worker1,
                    json={"product_ids": ["P-1002"], "prompt": "白底主图", "image_count": 1})
    female = env.post("/api/image/tasks", headers=worker2,
                      json={"product_ids": ["P-1001"], "prompt": "白底主图", "image_count": 1})
    assert male.status_code == 201 and female.status_code == 201
    female_id = female.json()["tasks"][0]["task_id"]

    male_rows = env.get("/api/image/admin/tasks?department=%E7%94%B7%E8%A3%85&limit=100",
                        headers=admin).json()["tasks"]
    assert all(row["department"] == "男装" for row in male_rows)
    female_rows = env.get("/api/image/admin/tasks?department=%E5%A5%B3%E8%A3%85&limit=100",
                          headers=admin).json()["tasks"]
    assert all(row["department"] == "女装" for row in female_rows)
    assert female_id in {row["task_id"] for row in female_rows}


# ---------------------------------------------------------------- 前端懒加载契约
def test_gallery_uses_lazy_pagination():
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "web" / "image.js").read_text(encoding="utf-8")
    assert "IntersectionObserver" in js
    assert "GALLERY_PAGE" in js and "galleryQueue" in js
    assert "loading=\"lazy\"" in js and "decoding=\"async\"" in js
    for endpoint in ("/api/image/tasks/cancel-batch", "/api/image/tasks/retry-batch",
                     "/api/image/batches/"):
        assert endpoint in js, endpoint
    html = (Path(__file__).resolve().parents[1] / "web" / "image.html").read_text(encoding="utf-8")
    for element_id in ("btn-cancel-batch", "btn-retry-batch", "btn-batch-summary",
                       "admin-department", "batch-hint"):
        assert f'id="{element_id}"' in html, element_id
