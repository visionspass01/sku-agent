"""第四轮 W8：批次汇总的补充口径（P2-10）与 ref_source 一致性（P2-2）。

批次里参考图来源可能不一致（同一批次内任务来自不同 ref_source），过去直接取 `rows[0]`
当整批口径；现在不一致返回 `mixed` 并列出实际来源集合。
"""

from __future__ import annotations

import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
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


def _wait(env, headers, task_id, timeout=45.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail.get("status") in {"done", "failed", "canceled"} and \
                detail.get("backfill_status") != "pending":
            return detail
        time.sleep(0.05)
    return detail


def _create_batch(env, headers, products=("P-3001", "P-2001")) -> dict:
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": list(products), "prompt": "白底主图",
                             "slot_count": 2, "image_count": 2})
    assert created.status_code == 201, created.text
    body = created.json()
    for task in body["tasks"]:
        _wait(env, headers, task["task_id"])
    return body


def test_batch_summary_has_v10_fields(env):
    headers = login(env)
    batch = _create_batch(env, headers)
    detail = env.get(f"/api/image/batches/{batch['batch_id']}", headers=headers).json()
    for key in ("reused_images", "partial_tasks", "warnings_count", "blocked_count",
                "ref_sources", "note", "delivered_images", "attempted_images"):
        assert key in detail, key
    assert detail["ref_source"] == "mcp"
    assert detail["ref_sources"] == ["mcp"]
    assert detail["count"] == 2
    assert detail["delivered_images"] <= detail["attempted_images"]
    assert "交付量" in detail["note"]
    for task in detail["tasks"]:
        assert "warnings_count" in task and "blocked_count" in task
        assert "partial" in task and "image_count" in task


def test_batch_summary_marks_mixed_ref_source(env):
    headers = login(env)
    batch = _create_batch(env, headers)
    task_id = batch["tasks"][0]["task_id"]
    with session_scope() as session:
        task_store.update_task(session, task_id, ref_source="library")
    detail = env.get(f"/api/image/batches/{batch['batch_id']}", headers=headers).json()
    assert detail["ref_source"] == "mixed"
    assert set(detail["ref_sources"]) == {"mcp", "library"}


def test_batch_summary_cost_has_three_columns(env):
    """P2-3（第八轮）：批次成本要给「生图 / LLM / 合计」三列，与卡片口径一致。"""
    headers = login(env)
    batch = _create_batch(env, headers)
    task_id = batch["tasks"][0]["task_id"]
    with session_scope() as session:
        task_store.update_task(session, task_id, external_cost_cny=0.4, llm_cost_cny=0.06)
    detail = env.get(f"/api/image/batches/{batch['batch_id']}", headers=headers).json()
    assert detail["cost_cny"] == pytest.approx(0.4), detail
    assert detail["llm_cost_cny"] == pytest.approx(0.06), detail
    assert detail["cost_total_cny"] == pytest.approx(0.46), detail


def test_batch_summary_counts_partial_tasks(env):
    headers = login(env)
    batch = _create_batch(env, headers)
    with session_scope() as session:
        task = task_store.get_task(session, batch["tasks"][0]["task_id"])
        expected = int(task.image_count or 0)
        # 模拟"少交付一张"的终态
        task_store.update_task(session, batch["tasks"][0]["task_id"],
                               status="done", delivered_images=max(0, expected - 1))
    detail = env.get(f"/api/image/batches/{batch['batch_id']}", headers=headers).json()
    assert detail["partial_tasks"] >= 1
    assert any(task["partial"] for task in detail["tasks"])
