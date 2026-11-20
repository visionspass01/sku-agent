"""第九轮 P1-6：请求级限流 + SSE 并发连接上限。

- 配额是按周期（天/周/月）算的，挡不住"一分钟内狂点建任务"：建任务与素材上传都会
  触发中台 MCP 调用，必须单独限速（每账号每分钟 N 次，超限 429 IMG_RATE_LIMITED）。
- SSE 每个连接 3s 轮询一次 DB，不设并发上限时连接数会被放大；现在超限直接 429。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.api.ratelimit import check_rate_limit, reset_rate_limits
from app.agents_image.api.stream_routes import (
    _guard,
    _try_acquire,
    reset_sse_guard,
)
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.core.errors import ImageError
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    monkeypatch.delenv("IMAGE_PRODUCT_SOURCE", raising=False)
    reset_image_settings()
    reset_throttle()
    reset_rate_limits()
    reset_sse_guard()
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
    reset_rate_limits()
    reset_sse_guard()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()


def login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _stub_dispatcher(monkeypatch) -> None:
    class _Stub:
        async def submit(self, task_id: str) -> str:
            return task_id

    monkeypatch.setattr("app.agents_image.api.routes.get_dispatcher", lambda: _Stub())


def _create(env, headers):
    return env.post("/api/image/tasks", headers=headers,
                    json={"product_ids": ["P-3001"], "prompt": "白底主图",
                          "image_count": 1})


def test_create_tasks_is_rate_limited(env, monkeypatch):
    monkeypatch.setenv("IMAGE_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("IMAGE_RATE_LIMIT_PER_MINUTE", "2")
    reset_image_settings()
    _stub_dispatcher(monkeypatch)
    headers = login(env)

    assert _create(env, headers).status_code == 201
    assert _create(env, headers).status_code == 201
    limited = _create(env, headers)
    assert limited.status_code == 429, limited.text
    assert limited.json()["error_code"] == "IMG_RATE_LIMITED"
    assert limited.json()["retryable"] is True


def test_rate_limit_can_be_disabled(env, monkeypatch):
    monkeypatch.setenv("IMAGE_RATE_LIMIT_PER_MINUTE", "1")
    monkeypatch.setenv("IMAGE_RATE_LIMIT_ENABLED", "false")
    reset_image_settings()
    _stub_dispatcher(monkeypatch)
    headers = login(env)
    assert _create(env, headers).status_code == 201
    assert _create(env, headers).status_code == 201


def test_library_upload_is_rate_limited(env, monkeypatch):
    import base64

    from app.agents_image.png import placeholder_png

    monkeypatch.setenv("IMAGE_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("IMAGE_RATE_LIMIT_PER_MINUTE", "1")
    reset_image_settings()
    headers = login(env)
    data = base64.b64encode(placeholder_png(f"lib-{uuid.uuid4().hex[:6]}", 48, 48)).decode()
    assert env.post("/api/image/library", headers=headers,
                    json={"data_b64": data}).status_code == 201
    limited = env.post("/api/image/library", headers=headers,
                       json={"data_b64": data})
    assert limited.status_code == 429, limited.text
    assert limited.json()["error_code"] == "IMG_RATE_LIMITED"


def test_check_rate_limit_raises_with_retryable(monkeypatch):
    from app.agents_image.config import get_image_settings

    monkeypatch.setenv("IMAGE_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("IMAGE_RATE_LIMIT_PER_MINUTE", "2")
    get_image_settings.cache_clear()
    reset_rate_limits()
    user = f"u-limit-{uuid.uuid4().hex[:8]}"
    limit = int(get_image_settings().rate_limit_per_minute)
    for _ in range(limit):
        check_rate_limit(user, "unit")
    with pytest.raises(ImageError) as caught:
        check_rate_limit(user, "unit")
    assert caught.value.code == "IMG_RATE_LIMITED"
    assert caught.value.retryable is True
    reset_rate_limits()


def test_sse_connection_slot_is_capped(env, monkeypatch):
    monkeypatch.setenv("IMAGE_SSE_MAX_CONNECTIONS", "1")
    reset_image_settings()
    reset_sse_guard()

    async def scenario():
        sem = _guard()
        assert await _try_acquire(sem) is True
        assert await _try_acquire(sem) is False      # 超限：连接必须被拒
        sem.release()
        assert await _try_acquire(sem) is True       # 释放后可以再连
        sem.release()

    asyncio.run(scenario())
