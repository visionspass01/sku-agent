"""第四轮 W8：非终态不算"部分成功"（P1-9）。

过去排队中的任务也按 `delivered < image_count` 判 partial，于是刚建出来的任务就显示
「部分成功，预期 6 张，输出 0 张」——等于凭空诬告。现在只有终态才给结论，其余 `in_progress`。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

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


def _wait(env, headers, task_id, timeout=25.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail.get("status") in {"done", "failed", "canceled"}:
            return detail
        time.sleep(0.05)
    return detail


def _task_stub(**overrides):
    base = dict(task_id="t-1", batch_id="b-1", product_id="P-1001", platform="淘系",
                variant_id="", color_key="P-1001#米色", base_task_id=None,
                status="queued", attempts=0, slot_count=9, image_count=6,
                ref_source="mcp", retries=0, provider_calls=0, attempted_images=0,
                delivered_images=0, warnings_json=[], blocked_json=[],
                backfill_status="pending", error_code="", slots_json=[])
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------- 单元层
def test_snapshot_marks_in_progress_without_partial():
    from app.agents_image.api.stream_routes import _snapshot_payload

    snapshot = _snapshot_payload(_task_stub())
    assert snapshot["in_progress"] is True
    assert snapshot["partial"] is False
    assert snapshot["message"] == ""


def test_snapshot_reports_partial_only_when_terminal():
    from app.agents_image.api.stream_routes import _snapshot_payload

    done = _snapshot_payload(_task_stub(status="done", delivered_images=2, attempted_images=6,
                                        provider_calls=6))
    assert done["in_progress"] is False
    assert done["partial"] is True
    assert "部分成功" in done["message"]

    perfect = _snapshot_payload(_task_stub(status="done", delivered_images=6,
                                           attempted_images=6))
    assert perfect["partial"] is False and perfect["message"] == ""


def test_partial_fields_respect_status():
    from app.agents_image.api.routes import _partial_fields

    queued = _partial_fields(_task_stub())
    assert queued == {"expected_images": 6, "partial": False, "message": "",
                      "in_progress": True}

    running = _partial_fields(_task_stub(status="running", delivered_images=1))
    assert running["in_progress"] is True and running["partial"] is False

    failed = _partial_fields(_task_stub(status="failed", delivered_images=0,
                                        attempted_images=6))
    assert failed["in_progress"] is False and failed["partial"] is True


# ---------------------------------------------------------------- HTTP 层（排队中）
def test_queued_task_detail_does_not_claim_partial_success(env, monkeypatch):
    submitted: list[str] = []

    class _StubDispatcher:
        async def submit(self, task_id: str) -> str:
            submitted.append(task_id)          # 不执行，保持 queued
            return task_id

    monkeypatch.setattr("app.agents_image.api.routes.get_dispatcher",
                        lambda: _StubDispatcher())
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图",
                             "slot_count": 3, "image_count": 2})
    assert created.status_code == 201, created.text
    task_id = created.json()["tasks"][0]["task_id"]
    assert submitted == [task_id]

    detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
    assert detail["status"] == "queued"
    assert detail["partial"] is False
    assert detail["message"] == ""
    assert detail["in_progress"] is True


def test_done_task_still_reports_partial(env):
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图",
                             "slot_count": 2, "image_count": 2})
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert detail["status"] == "done"
    assert detail["in_progress"] is False
    if detail["delivered_images"] < detail["image_count"]:
        assert detail["partial"] is True and detail["message"]
    else:
        assert detail["partial"] is False and detail["message"] == ""
