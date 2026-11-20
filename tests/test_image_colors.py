"""M3：按「商品+颜色」出图、slot_count/image_count 分离、refs 预算校验。"""

from __future__ import annotations

import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.sources import product_source as ps
from app.agents_image.sources import refs as refs_source
from app.agents_image.store import assets as asset_store
from app.agents_image.store import quota as quota_store
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.core.errors import ImageError
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


def _wait(env, headers, task_id, timeout=45.0) -> dict:
    """等任务到终态（默认 45s，给并发跑/带 coverage 的慢环境留足余量）。"""
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail.get("status") in {"done", "failed", "canceled"} and \
                detail.get("backfill_status") != "pending":
            return detail
        time.sleep(0.05)
    return detail


# ---------------------------------------------------------------- 颜色粒度
def test_one_task_per_color(env):
    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1001"], "all_colors": True, "prompt": "白底主图", "image_count": 1,
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["tasks"]) == 3                     # P-1001 有米色/黑色/藏蓝
    colors = {item["color"] for item in body["tasks"]}
    assert colors == {"米色", "黑色", "藏蓝"}
    assert len({item["color_key"] for item in body["tasks"]}) == 3

    detail = _wait(env, headers, body["tasks"][0]["task_id"])
    # 失败时把真实原因带出来（错误码/告警），不要只留一句 "assert 'failed' == 'done'"
    problem = detail.get("error_code") or "; ".join(detail.get("warnings") or []) or "未到终态"
    assert detail["status"] == "done", problem
    assert detail["refs"], "每个颜色都要有参考图"


def test_color_is_locked_into_attrs(env):
    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1001"],
        "product_colors": [{"product_id": "P-1001", "colors": ["黑色"]}],
        "prompt": "白底主图", "image_count": 1,
    })
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["tasks"][0]["task_id"]
    with session_scope() as session:
        from app.agents_image.store import tasks as task_store

        task = task_store.get_task(session, task_id)
        assert task.color_key == "P-1001#黑色"
        assert task.attrs_json["color_hex"] == "#111111"
        assert task.attrs_json["color_names"] == ["黑色"]


def test_unknown_color_is_rejected(env):
    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1001"],
        "product_colors": [{"product_id": "P-1001", "colors": ["紫色"]}],
        "prompt": "白底主图",
    })
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "IMG_INPUT_INVALID"
    assert "紫色" in resp.json()["detail"]


def test_default_takes_first_color_only(env):
    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1001"], "prompt": "白底主图", "image_count": 1,
    })
    assert resp.status_code == 201
    assert len(resp.json()["tasks"]) == 1
    assert resp.json()["tasks"][0]["color"] == "米色"


# ---------------------------------------------------------------- 两个张数
def test_slot_count_and_image_count_are_separate(env):
    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3003"], "prompt": "白底主图", "slot_count": 9,
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["slot_count"] == 9
    assert body["image_count"] == 6                    # 只有 6 个 generate 图位
    assert len(body["slots"]) == 9
    assert body["tasks"][0]["image_count"] == 6
    assert body["tasks"][0]["slot_count"] == 9


def test_image_count_cannot_exceed_generate_slots(env):
    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3003"], "prompt": "白底主图", "slot_count": 3, "image_count": 6,
    })
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "IMG_INPUT_INVALID"


def test_quota_counts_image_count_times_colors(env):
    headers = login(env, "worker2", "hhw222")
    with session_scope() as session:
        quota_store.grant(session, "worker2", period_type="day", period_limit=10)
    # 3 色 × 6 张 = 18 > 10 → 直接 429
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1001"], "all_colors": True, "prompt": "白底主图",
    })
    assert resp.status_code == 429
    assert resp.json()["error_code"] == "IMG_QUOTA_EXCEEDED"
    assert "18" in resp.json()["detail"]


# ---------------------------------------------------------------- refs 预算
def test_refs_budget_error_when_colors_exceed_max(env):
    catalog = ps.get_catalog()
    targets = [refs_source.RefTarget("P-1001", color, hexcode) for color, hexcode in
               (("米色", "#C8B7A6"), ("黑色", "#111111"), ("藏蓝", "#2C5D8F"))]

    async def run():
        with session_scope() as session:
            return await refs_source.resolve_refs(
                catalog, targets, session=session, username="worker1", department=None,
                refs_per_item=2, max_refs=4)

    with pytest.raises(ImageError) as exc:
        import asyncio

        asyncio.run(run())
    assert exc.value.code == "IMG_REFS_BUDGET"


def test_refs_are_color_scoped(env):
    catalog = ps.get_catalog()
    targets = [refs_source.RefTarget("P-1002", "藏蓝", "#4A6B8A")]

    async def run():
        with session_scope() as session:
            return await refs_source.resolve_refs(
                catalog, targets, session=session, username="worker1", department=None,
                refs_per_item=2, max_refs=4)

    import asyncio

    resolution = asyncio.run(run())
    refs = resolution.refs_of("P-1002#藏蓝")
    assert len(refs) == 2
    with session_scope() as session:
        for ref in refs:
            asset = asset_store.get_asset(session, ref.asset_id)
            assert asset.color_key == "P-1002#藏蓝"
            assert asset.product_id == "P-1002"
