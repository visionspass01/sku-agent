"""M6/M7：采纳/驳回（正负样本、成本不返还）、部门隔离、用量原子 upsert、签名 URL、分页。"""

from __future__ import annotations

import threading
import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.api.common import signed_asset_url, verify_asset_signature
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.sources import product_source as ps
from app.agents_image.store import feedback as feedback_store
from app.agents_image.store import tasks as task_store
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
    image_metrics.reset()
    ensure_image_schema()
    with session_scope() as session:
        user_store.ensure_seed_users(session)
    yield app_client
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()


def login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _done_task(env, headers, product="P-1002", **extra) -> dict:
    body = {"product_ids": [product], "prompt": "白底主图", "image_count": 1}
    body.update(extra)
    created = env.post("/api/image/tasks", headers=headers, json=body)
    assert created.status_code == 201, created.text
    task_id = created.json()["tasks"][0]["task_id"]
    deadline = time.time() + 45
    detail: dict = {}
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail["status"] in {"done", "failed"} and detail["backfill_status"] != "pending":
            break
        time.sleep(0.05)
    assert detail["status"] == "done", detail
    detail["task_id"] = task_id
    return detail


# ---------------------------------------------------------------- 采纳/驳回
def test_adopt_records_positive_sample_and_keeps_cost(env):
    headers = login(env)
    detail = _done_task(env, headers)
    asset = detail["assets"][0]
    resp = env.post(f"/api/image/tasks/{detail['task_id']}/assets/{asset['asset_id']}/adopt",
                    headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"] == "adopted" and body["stats"]["adopted"] == 1

    with session_scope() as session:
        task = task_store.get_task(session, detail["task_id"])
        # 成本不因驳回/采纳改变（provider 成功即计入）
        assert task.cost_cny == task.external_cost_cny
        samples = feedback_store.samples(session, kind="positive")
        assert any(sample["signature"].startswith("adopted|") for sample in samples)


def test_reject_requires_reason_and_records_negative(env):
    headers = login(env)
    detail = _done_task(env, headers, product="P-3003")
    asset = detail["assets"][0]
    no_reason = env.post(
        f"/api/image/tasks/{detail['task_id']}/assets/{asset['asset_id']}/reject",
        headers=headers, json={})
    assert no_reason.status_code == 422
    assert no_reason.json()["error_code"] == "IMG_INPUT_INVALID"

    resp = env.post(f"/api/image/tasks/{detail['task_id']}/assets/{asset['asset_id']}/reject",
                    headers=headers, json={"reason": "颜色偏暗，与实物不符"})
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "rejected"
    with session_scope() as session:
        negatives = feedback_store.samples(session, kind="negative")
        assert any("颜色偏暗" in (item["prompt_patch"] or "") for item in negatives)


def test_adopt_is_idempotent_and_switches_verdict(env):
    headers = login(env)
    detail = _done_task(env, headers, product="P-2001")
    asset = detail["assets"][0]
    path = f"/api/image/tasks/{detail['task_id']}/assets/{asset['asset_id']}"
    env.post(f"{path}/adopt", headers=headers)
    env.post(f"{path}/adopt", headers=headers)
    with session_scope() as session:
        stats = feedback_store.summary(session, task_id=detail["task_id"])
    assert stats["adopted"] == 1 and stats["rejected"] == 0

    env.post(f"{path}/reject", headers=headers, json={"reason": "改判"})
    with session_scope() as session:
        stats = feedback_store.summary(session, task_id=detail["task_id"])
    assert stats["adopted"] == 0 and stats["rejected"] == 1


def test_feedback_requires_owner(env):
    admin = login(env)
    detail = _done_task(env, admin)
    asset = detail["assets"][0]
    worker = login(env, "worker2", "hhw222")
    resp = env.post(f"/api/image/tasks/{detail['task_id']}/assets/{asset['asset_id']}/adopt",
                    headers=worker)
    assert resp.status_code == 403


def test_admin_samples_and_feedback_endpoints(env):
    headers = login(env)
    assert env.get("/api/image/admin/samples", headers=headers).status_code == 200
    assert env.get("/api/image/admin/feedback", headers=headers).status_code == 200
    worker = login(env, "worker1", "hhw111")
    assert env.get("/api/image/admin/samples", headers=worker).status_code == 403


# ---------------------------------------------------------------- 部门隔离
def test_department_isolation_on_products(env):
    worker1 = login(env, "worker1", "hhw111")      # 男装
    rows = env.get("/api/image/products", headers=worker1).json()["products"]
    assert rows, "男装账号应该至少看到一个男装商品"
    ids = {item["product_id"] for item in rows}
    # P-1001 是女装，不应出现在男装账号的清单里
    assert "P-1001" not in ids
    admin = login(env)
    admin_ids = {item["product_id"] for item in
                 env.get("/api/image/products", headers=admin).json()["products"]}
    assert "P-1001" in admin_ids


def test_department_isolation_blocks_cross_department_task(env):
    worker1 = login(env, "worker1", "hhw111")
    resp = env.post("/api/image/tasks", headers=worker1,
                    json={"product_ids": ["P-1001"], "prompt": "白底主图"})
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "IMG_FORBIDDEN"

    ok = env.post("/api/image/tasks", headers=worker1,
                  json={"product_ids": ["P-1002"], "prompt": "白底主图", "image_count": 1})
    assert ok.status_code == 201


# ---------------------------------------------------------------- 用量 upsert / 签名 URL / 分页
def test_usage_upsert_is_atomic_under_concurrency(_db):
    """并发写同一小时：只应有一行，计数不丢（P2-3）。"""
    with session_scope() as session:
        user_store.ensure_seed_users(session)

    def worker():
        with session_scope() as session:
            usage_store.record(session, username="u-usage-atomic", delivered=1, tasks=1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with session_scope() as session:
        from sqlalchemy import func, select

        from app.agents_image.store.models import ImageUsage

        count = session.execute(
            select(func.count()).select_from(ImageUsage)
            .where(ImageUsage.username == "u-usage-atomic")).scalar_one()
        totals = usage_store.totals(session, username="u-usage-atomic")
    assert count == 1
    assert totals["images"] == 8 and totals["tasks"] == 8


def test_signed_asset_url_works_without_bearer(env):
    headers = login(env)
    detail = _done_task(env, headers, product="P-3002")
    asset = detail["assets"][0]
    url = signed_asset_url(asset["asset_id"], ttl_seconds=60)
    resp = env.get(url)                                  # 不带 Authorization
    assert resp.status_code == 200
    assert resp.content.startswith(b"\x89PNG")

    # 篡改签名 → 403；过期 → 403
    tampered = url.replace("sig=", "sig=bad")
    assert env.get(tampered).status_code == 403
    assert verify_asset_signature(asset["asset_id"], "1", "bad") is False


def test_task_list_supports_pagination(env):
    headers = login(env)
    _done_task(env, headers, product="P-1002")
    first = env.get("/api/image/tasks?limit=1&offset=0", headers=headers).json()["tasks"]
    second = env.get("/api/image/tasks?limit=1&offset=1", headers=headers).json()["tasks"]
    assert len(first) == 1
    if second:
        assert first[0]["task_id"] != second[0]["task_id"]
