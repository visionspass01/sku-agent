"""B2（上架标记）与 C2（iterate 图位复用）的接口/编排用例。"""

from __future__ import annotations

import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.sources import product_source as ps
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


def _done(env, headers, body: dict, timeout=45.0) -> dict:
    created = env.post("/api/image/tasks", headers=headers, json=body)
    assert created.status_code == 201, created.text
    task_id = created.json()["tasks"][0]["task_id"]
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail["status"] in {"done", "failed"} and detail.get("backfill_status") != "pending":
            break
        time.sleep(0.05)
    detail["task_id"] = task_id
    return detail


# ---------------------------------------------------------------- B2 上架标记
def test_listing_mark_updates_task_and_metrics(env):
    headers = login(env)
    detail = _done(env, headers, {"product_ids": ["P-1002"], "prompt": "白底主图",
                                 "image_count": 1})
    task_id = detail["task_id"]
    asset = detail["assets"][0]["asset_id"]
    env.post(f"/api/image/tasks/{task_id}/assets/{asset}/adopt", headers=headers)

    # listed 必须带链接
    bad = env.post(f"/api/image/tasks/{task_id}/listing", headers=headers,
                   json={"status": "listed"})
    assert bad.status_code == 422

    ok = env.post(f"/api/image/tasks/{task_id}/listing", headers=headers,
                  json={"status": "listed", "listing_url": "https://shop.example.com/item/1"})
    assert ok.status_code == 200, ok.text
    fetched = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
    assert fetched["listing_status"] == "listed"
    assert fetched["listing_url"].startswith("https://shop.example.com")
    assert fetched["listed_at"]

    metrics = env.get("/api/image/admin/metrics?granularity=day&count=3&refresh=1",
                      headers=headers).json()
    assert 0.0 <= metrics["cards"]["listing_rate"] <= 1.0


def test_listing_rejects_unknown_status_and_other_users(env):
    headers = login(env)
    detail = _done(env, headers, {"product_ids": ["P-3003"], "prompt": "白底主图",
                                 "image_count": 1})
    task_id = detail["task_id"]
    bad = env.post(f"/api/image/tasks/{task_id}/listing", headers=headers,
                   json={"status": "sold"})
    assert bad.status_code == 422

    worker = login(env, "worker2", "hhw222")
    other = env.post(f"/api/image/tasks/{task_id}/listing", headers=worker,
                     json={"status": "pending"})
    assert other.status_code == 403


def test_listing_not_listed_clears_timestamp(env):
    headers = login(env)
    detail = _done(env, headers, {"product_ids": ["P-2001"], "prompt": "白底主图",
                                 "image_count": 1})
    task_id = detail["task_id"]
    env.post(f"/api/image/tasks/{task_id}/listing", headers=headers,
             json={"status": "listed", "listing_url": "https://shop.example.com/x"})
    env.post(f"/api/image/tasks/{task_id}/listing", headers=headers,
             json={"status": "not_listed"})
    fetched = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
    assert fetched["listing_status"] == "not_listed"
    assert fetched["listed_at"] == ""


# ---------------------------------------------------------------- C2 iterate 复用
def test_iterate_reuses_other_slots(env):
    headers = login(env)
    base = _done(env, headers, {"product_ids": ["P-1002"], "prompt": "白底主图",
                                "image_count": 3, "slot_count": 3})
    assert base["status"] == "done"
    base_outputs = {asset["slot"] for asset in base["assets"]}
    assert base_outputs == {1, 2, 3}

    detail = _done(env, headers, {
        "product_ids": ["P-1002"], "prompt": "主图点击率低，只改主图",
        "mode": "iterate", "base_task_id": base["task_id"],
        "image_count": 3, "slot_count": 3,
    })
    assert detail["status"] == "done", detail
    assert detail["base_task_id"] == base["task_id"]
    assert detail["reused_images"] == 2                    # slot 2/3 复用
    slots = {asset["slot"] for asset in detail["assets"]}
    assert slots == {1, 2, 3}
    reused_slots = {asset["slot"] for asset in detail["assets"] if asset.get("reused")}
    assert reused_slots == {2, 3}
    # 配额只算新出的 1 张
    assert detail["delivered_images"] == 1
    assert detail["image_count"] == 1


def test_iterate_requires_base_task_and_owner(env):
    headers = login(env)
    base = _done(env, headers, {"product_ids": ["P-3001"], "prompt": "白底主图",
                                "image_count": 2, "slot_count": 2})

    missing = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "只改主图", "mode": "iterate",
        "base_task_id": "20261118000000eeee",
    })
    assert missing.status_code == 404

    worker = login(env, "worker2", "hhw222")
    other = env.post("/api/image/tasks", headers=worker, json={
        "product_ids": ["P-3001"], "prompt": "只改主图", "mode": "iterate",
        "base_task_id": base["task_id"],
    })
    # 越权复用：403（优先）或先被部门/配额前置拦下（422/429）都算"拒绝"
    assert other.status_code in {403, 422, 429}


def test_iterate_rejects_mismatched_color(env):
    headers = login(env)
    base = _done(env, headers, {"product_ids": ["P-1001"],
                                "product_colors": [{"product_id": "P-1001", "colors": ["米色"]}],
                                "prompt": "白底主图", "image_count": 2, "slot_count": 2})
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1001"],
        "product_colors": [{"product_id": "P-1001", "colors": ["黑色"]}],
        "prompt": "只改主图", "mode": "iterate", "base_task_id": base["task_id"],
    })
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "IMG_INPUT_INVALID"


def test_iterate_without_base_falls_back_to_standard(env):
    headers = login(env)
    detail = _done(env, headers, {"product_ids": ["P-3002"], "prompt": "白底主图",
                                  "mode": "iterate", "image_count": 1})
    assert detail["status"] == "done"
    assert detail["reused_images"] == 0


# ---------------------------------------------------------------- W1：复用精确匹配 / 平台 / 计划张数
def test_iterate_only_reuses_same_color_target(env):
    """多色 iterate：只有与基础任务同 (颜色, 平台, 变体) 的目标复用，其它颜色必须正常出图。"""
    headers = login(env)
    base = _done(env, headers, {
        "product_ids": ["P-1001"],
        "product_colors": [{"product_id": "P-1001", "colors": ["米色"]}],
        "prompt": "白底主图", "image_count": 3, "slot_count": 3,
    })
    assert base["status"] == "done"

    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1001"],
        "product_colors": [{"product_id": "P-1001", "colors": ["米色", "黑色"]}],
        "prompt": "主图点击率低，只改主图", "mode": "iterate",
        "base_task_id": base["task_id"], "image_count": 3, "slot_count": 3,
    })
    assert created.status_code == 201, created.text
    payload = created.json()
    by_color = {item["color"]: item for item in payload["tasks"]}
    # 米色：复用 slot2/3，只出新主图 1 张
    assert by_color["米色"]["reused_images"] == 2
    assert by_color["米色"]["image_count"] == 1
    # 黑色：不能串米色的成品，正常出 3 张
    assert by_color["黑色"]["reused_images"] == 0
    assert by_color["黑色"]["image_count"] == 3
    assert by_color["黑色"]["base_task_id"] is None
    # 计划张数按复用扣减（1 + 3），不是 3×2
    assert payload["planned_images"] == 4
    # 复用口径（P2-8）：reused_images=图片张数，reused_tasks=被复用的任务数
    assert payload["reused_images"] == 2
    assert payload["reused_tasks"] == 1


def test_platform_variant_forces_both_platforms(env):
    """`platform_variant` 语义固定为双平台：即使同时给了单平台，也要出淘系+抖系两套。"""
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白底主图", "platform": "淘系",
        "platform_variant": True, "image_count": 1,
    })
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["platforms"] == ["淘系", "抖系"]
    assert {item["platform"] for item in payload["tasks"]} == {"淘系", "抖系"}
