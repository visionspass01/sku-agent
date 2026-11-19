"""第三轮 P1-1：交付张数少于规划时必须说清"部分成功，预期 X 张，输出 Y 张"。

背景（评审证据）：provider 少返回几个图位时，任务仍 `done`、`warnings/blocked` 都是空的，
用户以为 6 张全出，实际只拿到 3 张（静默少交付）。
"""

from __future__ import annotations

import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.api import stream_routes
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.generator import handler as generator_handler
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import tasks as task_store
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


def _wait(env, headers, task_id, timeout=25.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail.get("status") in {"done", "failed", "canceled"} and \
                detail.get("backfill_status") != "pending":
            return detail
        time.sleep(0.05)
    return detail


def _short_provider(monkeypatch, drop: int = 1) -> None:
    """让 provider 少返回 `drop` 个图位（模拟第三方部分失败）。"""
    original = generator_handler.run

    async def short(payload):
        result = await original(payload)
        slots = result.get("slots") or []
        for item in slots[-drop:]:
            item["images"] = []
        return result

    monkeypatch.setattr(generator_handler, "run", short)


def test_short_delivery_is_reported_as_partial(env, monkeypatch):
    headers = login(env)
    _short_provider(monkeypatch, drop=1)
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白底主图", "slot_count": 2, "image_count": 2,
    })
    assert created.status_code == 201, created.text
    task_id = created.json()["tasks"][0]["task_id"]
    detail = _wait(env, headers, task_id)

    assert detail["status"] == "done"              # 部分成功仍是 done（有图交付）
    assert detail["expected_images"] == 2
    assert detail["delivered_images"] == 1
    assert detail["partial"] is True
    assert detail["message"] == "部分成功，预期 2 张，输出 1 张"
    assert any("IMG_SLOT_MISSING" in item for item in detail["warnings"]), detail["warnings"]
    assert image_metrics.snapshot()["partial_deliveries"] >= 1

    # 同一个口径要能出现在 SSE 的 done 快照里
    with session_scope() as session:
        snapshot = stream_routes._snapshot_payload(
            task_store.get_task(session, task_id))
    assert snapshot["partial"] is True
    assert snapshot["expected_images"] == 2
    assert snapshot["message"] == "部分成功，预期 2 张，输出 1 张"


def test_full_delivery_is_not_partial(env):
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白底主图", "slot_count": 2, "image_count": 2,
    })
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert detail["status"] == "done"
    assert detail["partial"] is False
    assert detail["message"] == ""
    assert not any("IMG_SLOT_MISSING" in item for item in detail["warnings"])


def test_blocked_slot_counts_as_partial(env, monkeypatch):
    """被像素门禁拦下的图位也算"少交付"：预期 2 张 / 输出 1 张 要报出来。"""
    headers = login(env)
    from app.agents_image.nodes import compliance

    original_gate = compliance.gate
    calls = {"n": 0}

    def flaky_gate(data, *, slot, role, platform_rules, gate_mode, autofix,
                   aspect=None, nonpng_policy="warn"):
        calls["n"] += 1
        if calls["n"] == 2:                          # 第二个图位故意判不过
            return compliance.Verdict(passed=False, reasons=["测试注入：主体占比过大"],
                                      data=data)
        return original_gate(data, slot=slot, role=role, platform_rules=platform_rules,
                             gate_mode=gate_mode, autofix=autofix, aspect=aspect,
                             nonpng_policy=nonpng_policy)

    monkeypatch.setattr(compliance, "gate", flaky_gate)
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白底主图", "slot_count": 2, "image_count": 2,
    })
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert detail["status"] == "done"
    assert detail["delivered_images"] == 1
    assert detail["partial"] is True
    assert detail["message"] == "部分成功，预期 2 张，输出 1 张"
    assert detail["blocked"], "被拦下的图位要能解释为什么少了"
    assert any("IMG_CONTENT_BLOCKED" in item for item in detail["warnings"])
