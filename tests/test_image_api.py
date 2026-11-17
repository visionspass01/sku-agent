"""生图 API 端到端（本地调度 + Fake Provider）：上传 → 建任务 → 出图 → 取图。"""

from __future__ import annotations

import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.config import reset_image_settings
from app.agents_image.registry import reset_registry
from app.agents_image.store.schema import ensure_image_schema
from tests.test_image_storage import make_png


@pytest.fixture()
def image_env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.delenv("IMAGE_STORAGE", raising=False)
    monkeypatch.delenv("IMAGE_PROVIDER", raising=False)
    reset_image_settings()
    reset_registry()
    image_metrics.reset()
    ensure_image_schema()
    yield app_client
    reset_image_settings()
    reset_registry()
    image_metrics.reset()


def _upload(client, data: bytes | None = None) -> dict:
    resp = client.post("/api/image/upload", content=data or make_png(),
                       headers={"Content-Type": "image/png"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _wait_done(client, task_id: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/image/tasks/{task_id}").json()
        if payload.get("status") in {"done", "failed", "canceled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"任务未在 {timeout}s 内完成：{payload}")


def test_upload_then_task_then_fetch_asset(image_env):
    client = image_env
    upload = _upload(client)
    assert upload["bytes"] > 0 and upload["sha256"]

    created = client.post("/api/image/tasks", json={
        "ref_asset_ids": [upload["asset_id"]],
        "prompt": "白底主图，正面全身像上装突出",
        "aspect": "1:1",
        "n": 2,
    })
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]
    assert task_id.startswith(tuple("0123456789")) and len(task_id) == 18

    task = _wait_done(client, task_id)
    assert task["status"] == "done", task
    assert task["delivered_images"] == 2
    assert task["attempted_images"] == 2
    assert len(task["assets"]) == 2
    first = task["assets"][0]
    got = client.get(first["url"])
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("image/png")
    assert got.content.startswith(b"\x89PNG")


def test_task_requires_reference(image_env):
    resp = image_env.post("/api/image/tasks", json={"ref_asset_ids": [], "prompt": "白底主图"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "IMG_REFERENCE_REQUIRED"
    assert resp.headers["X-Error-Code"] == "IMG_REFERENCE_REQUIRED"
    assert resp.headers["X-Trace-Id"]


def test_task_rejects_more_than_six_refs(image_env):
    refs = [_upload(image_env, make_png(w, w))["asset_id"] for w in (16, 20, 24, 28, 32, 36, 40)]
    resp = image_env.post("/api/image/tasks", json={"ref_asset_ids": refs, "prompt": "白底主图"})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "IMG_TOO_MANY_REFS"


def test_upload_rejects_non_image_body(image_env):
    resp = image_env.post("/api/image/upload", content=b"hello",
                          headers={"Content-Type": "text/plain"})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "IMG_REFERENCE_INVALID"


def test_unknown_task_and_asset_are_404(image_env):
    assert image_env.get("/api/image/tasks/20261117000000abcd").status_code == 404
    assert image_env.get("/api/image/assets/nope").status_code == 404


def test_health_and_stats(image_env):
    health = image_env.get("/api/image/health").json()
    assert health["status"] == "ok"
    assert health["provider"] == "fake"
    assert health["max_refs"] == 6
    stats = image_env.get("/api/image/stats").json()
    assert "tasks_created" in stats and "in_flight_images" in stats
