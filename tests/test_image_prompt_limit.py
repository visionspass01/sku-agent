"""第四轮 W6：需求描述长度上限（P2-6）。

超长不报错也不静默丢，而是截断到 `IMAGE_MAX_PROMPT_CHARS` 并给任务落
`IMG_PROMPT_TRUNCATED` 告警（运营能看见"我写的后半段被裁了"）。
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


def test_long_prompt_is_truncated_with_warning(env, monkeypatch):
    monkeypatch.setenv("IMAGE_MAX_PROMPT_CHARS", "60")
    reset_image_settings()
    headers = login(env)
    long_prompt = "白底主图，" + "强调版型与面料细节，" * 30
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": long_prompt, "image_count": 1,
    })
    assert created.status_code == 201, created.text
    task_id = created.json()["tasks"][0]["task_id"]
    detail = _wait(env, headers, task_id)
    assert detail["status"] == "done"
    assert len(detail["prompt"]) <= 60
    assert any(str(item).startswith("IMG_PROMPT_TRUNCATED") for item in detail["warnings"]), \
        detail["warnings"]


def test_short_prompt_is_not_touched(env):
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白底主图，正面全身", "image_count": 1,
    })
    assert created.status_code == 201, created.text
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert detail["prompt"] == "白底主图，正面全身"
    assert not any(str(item).startswith("IMG_PROMPT_TRUNCATED") for item in detail["warnings"])


def test_default_limit_is_500_chars(env, monkeypatch):
    monkeypatch.delenv("IMAGE_MAX_PROMPT_CHARS", raising=False)
    reset_image_settings()
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白" * 700, "image_count": 1,
    })
    assert created.status_code == 201, created.text
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert len(detail["prompt"]) == 500
    assert any(str(item).startswith("IMG_PROMPT_TRUNCATED") for item in detail["warnings"])
