"""生图 API 端到端（本地调度 + Fake Provider）：登录 → 上传 → 建任务 → 出图 → 取图。"""

from __future__ import annotations

import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.ids import is_valid_task_id
from app.agents_image.registry import reset_registry
from app.agents_image.store.schema import ensure_image_schema
from app.agents_image.store.users import ensure_seed_users
from app.db.engine import session_scope
from tests.test_image_storage import make_png


@pytest.fixture()
def image_env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.delenv("IMAGE_STORAGE", raising=False)
    monkeypatch.delenv("IMAGE_PROVIDER", raising=False)
    reset_image_settings()
    reset_registry()
    reset_throttle()
    image_metrics.reset()
    ensure_image_schema()
    with session_scope() as session:
        ensure_seed_users(session)
    yield app_client
    reset_image_settings()
    reset_registry()
    reset_throttle()
    image_metrics.reset()


def login_headers(client, username: str = "admin", password: str = "hhw123") -> dict:
    """登录拿 Bearer 头（预置账号见 `store/users.py:SEED_USERS`）。"""
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture()
def auth_headers(image_env):
    return login_headers(image_env)


def _upload(client, headers: dict, data: bytes | None = None) -> dict:
    resp = client.post("/api/image/upload", content=data or make_png(),
                       headers={**headers, "Content-Type": "image/png"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _wait_done(client, headers: dict, task_id: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        payload = client.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if payload.get("status") in {"done", "failed", "canceled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"任务未在 {timeout}s 内完成：{payload}")


def test_upload_then_task_then_fetch_asset(image_env, auth_headers):
    client = image_env
    upload = _upload(client, auth_headers)
    assert upload["bytes"] > 0 and upload["sha256"]

    created = client.post("/api/image/tasks", headers=auth_headers, json={
        "ref_asset_ids": [upload["asset_id"]],
        "prompt": "白底主图，正面全身像上装突出",
        "aspect": "1:1",
        "n": 2,
    })
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]
    assert is_valid_task_id(task_id)

    task = _wait_done(client, auth_headers, task_id)
    assert task["status"] == "done", task
    assert task["delivered_images"] == 2
    assert task["attempted_images"] == 2
    assert len(task["assets"]) == 2
    first = task["assets"][0]
    got = client.get(first["url"], headers=auth_headers)
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("image/png")
    assert got.content.startswith(b"\x89PNG")


def test_task_requires_reference(image_env, auth_headers):
    resp = image_env.post("/api/image/tasks", headers=auth_headers,
                          json={"ref_asset_ids": [], "prompt": "白底主图"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "IMG_REFERENCE_REQUIRED"
    assert resp.headers["X-Error-Code"] == "IMG_REFERENCE_REQUIRED"
    assert resp.headers["X-Trace-Id"]


def test_task_rejects_more_than_six_refs(image_env, auth_headers):
    refs = [_upload(image_env, auth_headers, make_png(w, w))["asset_id"]
            for w in (64, 68, 72, 76, 80, 84, 88)]
    resp = image_env.post("/api/image/tasks", headers=auth_headers,
                          json={"ref_asset_ids": refs, "prompt": "白底主图"})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "IMG_TOO_MANY_REFS"


def test_upload_rejects_non_image_body(image_env, auth_headers):
    resp = image_env.post("/api/image/upload", content=b"hello",
                          headers={**auth_headers, "Content-Type": "text/plain"})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "IMG_REFERENCE_INVALID"


def test_unknown_task_and_asset_are_404(image_env, auth_headers):
    assert image_env.get("/api/image/tasks/20261117000000abcd",
                         headers=auth_headers).status_code == 404
    assert image_env.get("/api/image/assets/nope", headers=auth_headers).status_code == 404


def test_health_and_stats(image_env, auth_headers):
    health = image_env.get("/api/image/health")
    assert health.json()["status"] == "ok"
    assert health.json()["provider"] == "fake"
    assert health.json()["max_refs"] == 6
    assert health.headers["X-Trace-Id"]                       # 成功响应也回传 trace
    stats = image_env.get("/api/image/stats", headers=auth_headers).json()
    assert "tasks_created" in stats and "in_flight_images" in stats


def test_endpoints_require_login(image_env):
    """除 /health 外都必须登录：不带 Authorization → 401 IMG_UNAUTHORIZED。"""
    assert image_env.get("/api/image/health").status_code == 200
    for path in ("/api/image/stats", "/api/image/tasks", "/api/image/tasks/abc",
                 "/api/image/assets/abc"):
        resp = image_env.get(path)
        assert resp.status_code == 401, path
        assert resp.json()["error_code"] == "IMG_UNAUTHORIZED"
    resp = image_env.post("/api/image/tasks", json={"ref_asset_ids": [], "prompt": "x"})
    assert resp.status_code == 401
