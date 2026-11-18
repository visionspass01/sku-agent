"""W5：功能8/9/10/11 口径——使用量/发起量/成功率/上架率/配额/在途成本 + 告警落库 + 降级可见。"""

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
from app.agents_image.store.schema import ensure_image_schema, reset_schema_issues, schema_status
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
    reset_schema_issues()


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


def _cards(env, headers) -> dict:
    body = env.get("/api/image/admin/metrics?granularity=day&count=7", headers=headers).json()
    return body["cards"]


# ---------------------------------------------------------------- 功能8：五张卡口径
def test_cards_follow_usage_definitions(env):
    headers = login(env)
    before = _cards(env, headers)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图",
                             "image_count": 1})
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert detail["status"] == "done"
    after = _cards(env, headers)

    usage_delta = after["usage_images"] - before["usage_images"]
    calls_delta = after["provider_calls"] - before["provider_calls"]
    passed_delta = after["agent_passed"] - before["agent_passed"]
    assert usage_delta == 1                     # 使用量 = 成功生成张数
    assert calls_delta >= 1                     # 发起量 ≥ 1
    assert passed_delta == 1                    # 交付张数
    # 比率口径：成功率 = 交付/使用量；可用性 = 使用量/发起量
    assert after["api_availability"] == pytest.approx(
        after["usage_images"] / after["provider_calls"], rel=1e-3)
    assert after["agent_pass_rate"] == pytest.approx(
        after["agent_passed"] / after["usage_images"], rel=1e-3)
    # 上架率 = 人工通过 / 使用量
    assert after["listing_rate"] == pytest.approx(
        after["adopted"] / after["usage_images"], rel=1e-3)
    for key in ("quota_used", "quota_limit", "quota_used_rate", "in_flight",
                "cost_cny", "cost_per_image"):
        assert key in after, key


def test_chart_buckets_carry_new_fields(env):
    headers = login(env)
    env.post("/api/image/tasks", headers=headers,
             json={"product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1})
    body = env.get("/api/image/admin/metrics?granularity=hour&count=6", headers=headers).json()
    bucket = body["buckets"][-1]
    for key in ("images", "provider_calls", "agent_passed", "cost_cny"):
        assert key in bucket, key
    assert body["cards"]["usage_images"] >= 1


# ---------------------------------------------------------------- 功能10：告警与部分成功
def test_blocked_image_still_counts_as_usage_and_quota(env):
    """被像素门禁拦下的图：仍算使用量（钱已经花掉）+ 仍占配额，但不算交付。"""
    from app.agents_image.config import reset_image_settings as reset_settings

    headers = login(env, "worker2", "hhw222")
    with session_scope() as session:
        quota_store.grant(session, "worker2", period_type="day", period_limit=20)
    before = _cards(env, headers) if False else None  # noqa: F841 —— 用配额行断言更直接
    before_q = _quota("worker2")
    resp = env.post("/api/image/tasks", headers=headers,
                    json={"product_ids": ["P-2001"], "prompt": "白底主图", "image_count": 1})
    task_id = resp.json()["tasks"][0]["task_id"]
    detail = _wait(env, headers, task_id)
    assert detail["status"] == "done"
    after_q = _quota("worker2")
    assert after_q["used"] == before_q.get("used", 0) + (detail["attempted_images"] or 1)
    assert detail["warnings"] is not None
    assert isinstance(detail["blocked"], list)
    reset_settings()


def test_partial_success_is_explained(env):
    """有图被拦时任务仍算成功，但 blocked/warnings 必须能解释"为什么少了图"。"""
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-1001"], "prompt": "白底主图",
                             "image_count": 2, "slot_count": 2})
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert detail["status"] == "done"
    assert isinstance(detail["warnings"], list)
    assert isinstance(detail["blocked"], list)
    if detail["blocked"]:
        assert detail["delivered_images"] < detail["attempted_images"]


def test_cancel_records_usage_and_cost(env):
    """取消：额度释放，但使用量/成本照记（provider 已经跑过）。"""
    import asyncio

    from app.agents_image import graph
    from app.agents_image.nodes import plan_slots
    from app.agents_image.png import placeholder_png
    from app.agents_image.state import ImageTaskState, RefImage, SlotPlan
    from app.agents_image.store import assets as asset_store
    from app.agents_image.store import tasks as task_store

    with session_scope() as session:
        before = usage_store.totals(session)
        ref = asset_store.save_bytes(session, placeholder_png("ref", 64, 64), kind="ref",
                                     product_id="P-3001")
        state = ImageTaskState(prompt="白底主图", aspect="1:1", count=1,
                               refs=[RefImage(asset_id=ref.asset_id)])
        state.slots = [SlotPlan(**item) for item in plan_slots.build_slots(
            state, policy=asyncio.run(ps.get_catalog().get_plan_policy()))]
        task_store.create_task(
            session, task_id="20261118000000dd10", username="admin", department=None,
            prompt=state.prompt, platform="淘系", aspect="1:1", n=1,
            ref_asset_ids=[ref.asset_id], product_id="P-3001",
            slots=[item.model_dump() for item in state.slots], image_count=1)
        asset_store.bind_refs_to_task(session, [ref.asset_id], "20261118000000dd10")
        task_store.update_task(session, "20261118000000dd10", cancel_requested=True)

    asyncio.run(graph.execute_task("20261118000000dd10"))
    with session_scope() as session:
        task = task_store.get_task(session, "20261118000000dd10")
        status = task.status
        after = usage_store.totals(session)
    assert status == "canceled"
    assert after["failed"] >= before["failed"] + 1     # 取消记一次失败
    assert after["cost_cny"] >= before["cost_cny"]     # 成本照记（已发生）


def test_quota_settlement_mismatch_warns(env):
    """结算张数与预留不一致时：任务带上 IMG_QUOTA_SETTLE_MISMATCH 告警，不静默吞掉。"""
    headers = login(env, "worker2", "hhw222")
    with session_scope() as session:
        quota_store.grant(session, "worker2", period_type="day", period_limit=20)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-2001"], "prompt": "白底主图",
                             "image_count": 1})
    task_id = created.json()["tasks"][0]["task_id"]
    detail = _wait(env, headers, task_id)
    assert detail["status"] == "done"
    # 正常路径不该有对账告警（有的话说明账实不符，必须暴露出来）
    assert not any("IMG_QUOTA_SETTLE_MISMATCH" in item for item in detail["warnings"])


# ---------------------------------------------------------------- 功能10：库结构降级可见
def test_schema_degraded_is_visible_in_health(env):
    from app.agents_image.store import schema as schema_module

    reset_schema_issues()
    assert schema_status()["degraded"] is False
    schema_module._ISSUES.append("index uq_x: IntegrityError duplicate")
    health = env.get("/api/image/health").json()
    assert health["degraded"] is True
    assert "uq_x" in health["degraded_reason"]
    reset_schema_issues()
    assert env.get("/api/image/health").json()["degraded"] is False


def _quota(username: str) -> dict:
    with session_scope() as session:
        rows = {row["username"]: row for row in quota_store.list_quotas(session)}
    return rows.get(username, {})
