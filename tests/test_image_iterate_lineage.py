"""第四轮 W3：iterate 复用链（P1-3 变体/平台/颜色继承、P1-4 驳回图不复用、IMG_REUSE_SKIPPED）。

复用 key 是 `(颜色, 平台, 变体)` 三元组；过去 iterate 固定返回变体 `""`、缺平台时默认淘系、
缺颜色时取第一个颜色，导致变体基座/抖系基座/非首色基座永远命中不了复用。
"""

from __future__ import annotations

import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.sources import batch_context
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


def _create(env, headers, body: dict) -> dict:
    resp = env.post("/api/image/tasks", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _done(env, headers, body: dict) -> dict:
    created = _create(env, headers, body)
    task_id = created["tasks"][0]["task_id"]
    detail = _wait(env, headers, task_id)
    assert detail["status"] == "done", detail
    detail["task_id"] = task_id
    return detail


# ---------------------------------------------------------------- P1-3：变体基座
def test_iterate_inherits_variant_from_base(env):
    """variant(A/B) 基座上做 iterate：必须继承 variant_id，否则永远命中不了复用。"""
    headers = login(env)
    base = _create(env, headers, {
        "product_ids": ["P-3001"], "prompt": "白底主图",
        "mode": "variant", "slot_count": 3, "image_count": 2,
    })
    assert len(base["tasks"]) == 2
    variant_a = next(item for item in base["tasks"] if item["variant_id"] == "A")
    base_detail = _wait(env, headers, variant_a["task_id"])
    assert base_detail["status"] == "done"
    assert base_detail["assets"], "变体基座至少要出一张图"

    # 只给 base_task_id + mode：平台/颜色/变体都靠继承
    created = _create(env, headers, {
        "product_ids": ["P-3001"], "prompt": "主图点击率低，只改主图",
        "mode": "iterate", "base_task_id": variant_a["task_id"],
        "slot_count": 3, "image_count": 1,
    })
    task = created["tasks"][0]
    assert task["variant_id"] == "A", "iterate 必须继承基座变体，不能回落成空变体"
    assert created["variants"] == ["A"]
    assert created["reused_images"] >= 1, "同一 (颜色, 平台, 变体) 的成品应当被复用"
    detail = _wait(env, headers, task["task_id"])
    assert detail["status"] == "done"
    assert detail["base_task_id"] == variant_a["task_id"]


def test_iterate_inherits_platform_from_base(env):
    """抖系基座做 iterate 且未指定平台：不能被默认淘系顶掉。"""
    headers = login(env)
    base = _done(env, headers, {
        "product_ids": ["P-3001"], "prompt": "白底主图", "platform": "抖系",
        "slot_count": 3, "image_count": 2,
    })
    created = _create(env, headers, {
        "product_ids": ["P-3001"], "prompt": "只改主图",
        "mode": "iterate", "base_task_id": base["task_id"],
        "slot_count": 3, "image_count": 1,
    })
    task = created["tasks"][0]
    assert created["platforms"] == ["抖系"]
    assert task["platform"] == "抖系"
    assert created["reused_images"] >= 1
    _wait(env, headers, task["task_id"])


def test_iterate_inherits_color_from_base(env):
    """非首色基座（藏蓝）做 iterate：缺颜色时应继承基座，而不是回落第一个颜色（米色）。"""
    headers = login(env)
    base = _done(env, headers, {
        "product_ids": ["P-1001"],
        "product_colors": [{"product_id": "P-1001", "colors": ["藏蓝"]}],
        "prompt": "白底主图", "slot_count": 3, "image_count": 2,
    })
    created = _create(env, headers, {
        "product_ids": ["P-1001"], "prompt": "只改主图",
        "mode": "iterate", "base_task_id": base["task_id"],
        "slot_count": 3, "image_count": 1,
    })
    task = created["tasks"][0]
    assert task["color"] == "藏蓝"
    assert created["reused_images"] >= 1
    _wait(env, headers, task["task_id"])


# ---------------------------------------------------------------- P1-4：驳回图不复用
def test_rejected_base_asset_is_not_reused(env):
    """基座非主图成品被驳回 → 不能再当复用源；复用不上要落 IMG_REUSE_SKIPPED 说明原因。"""
    headers = login(env)
    base = _done(env, headers, {
        "product_ids": ["P-3001"], "prompt": "白底主图",
        "slot_count": 2, "image_count": 2,
    })
    outputs = [asset for asset in base["assets"] if int(asset["slot"]) != 1]
    assert outputs, "基座需要有可复用的非主图图位"
    for asset in outputs:
        resp = env.post(f"/api/image/tasks/{base['task_id']}/assets/{asset['asset_id']}/reject",
                        headers=headers, json={"reason": "颜色偏暗"})
        assert resp.status_code == 200, resp.text

    created = _create(env, headers, {
        "product_ids": ["P-3001"], "prompt": "只改主图",
        "mode": "iterate", "base_task_id": base["task_id"],
        "slot_count": 2, "image_count": 1,
    })
    assert created["reused_images"] == 0
    detail = _wait(env, headers, created["tasks"][0]["task_id"])
    assert detail["status"] == "done"
    assert detail["reused_images"] == 0
    skipped = [item for item in detail["warnings"] if item.startswith("IMG_REUSE_SKIPPED")]
    assert skipped, detail["warnings"]
    message = skipped[0]
    assert base["task_id"] in message
    assert "P-3001" in message


def test_graph_skips_rejected_source_at_execution_time(env):
    """建任务之后、执行之前被驳回：执行期二次校验仍要拦下（IMG_REUSE_REJECTED）。

    这里不走 API 派发（后台线程会立刻把任务跑完），直接落库再手动执行，时序可控。
    """
    import asyncio

    from app.agents_image import graph
    from app.agents_image.nodes import plan_slots
    from app.agents_image.png import placeholder_png
    from app.agents_image.state import ImageTaskState
    from app.agents_image.store import assets as asset_store
    from app.agents_image.store import feedback as feedback_store
    from app.agents_image.store import tasks as task_store

    headers = login(env)
    base = _done(env, headers, {
        "product_ids": ["P-3001"], "prompt": "白底主图",
        "slot_count": 2, "image_count": 2,
    })
    reuse_source = next(asset for asset in base["assets"] if int(asset["slot"]) != 1)
    task_id = "20261118000000w3001"

    state = ImageTaskState(prompt="只改主图", aspect="1:1", count=1,
                           refs=[], attrs={"product_type": "半身裙（长款）"})
    slots = plan_slots.build_slots(state, policy=asyncio.run(ps.get_catalog().get_plan_policy()))
    with session_scope() as session:
        # 复用源被驳回（模拟"建任务之后才被人工改判"）
        feedback_store.record(session, task_id=base["task_id"],
                              asset_id=reuse_source["asset_id"], verdict="rejected",
                              username="admin", reason="改判")
        ref = asset_store.save_bytes(session, placeholder_png("ref", 64, 64), kind="ref",
                                     product_id="P-3001")
        task_store.create_task(
            session, task_id=task_id, username="admin", department=None,
            prompt="只改主图", platform="淘系", aspect="1:1", n=1,
            ref_asset_ids=[ref.asset_id], product_id="P-3001",
            slots=[item.model_dump() if hasattr(item, "model_dump") else dict(item)
                   for item in slots],
            image_count=1, color_key=base["color_key"], base_task_id=base["task_id"],
            reuse_asset_ids={str(reuse_source["slot"]): reuse_source["asset_id"]},
        )
        asset_store.bind_refs_to_task(session, [ref.asset_id], task_id)

    asyncio.run(graph.execute_task(task_id))
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        warnings = [str(item) for item in (task.warnings_json or [])]
        reused = int(task.reused_images or 0)
    assert any(item.startswith("IMG_REUSE_REJECTED") for item in warnings), warnings
    assert reused == 0
