"""第十轮 P2-1/P2-3/P2-4/P2-5/P2-6/P2-8/P2-9 的回归用例（一批小修）。"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.api import admin_routes
from app.agents_image.api.ratelimit import reset_rate_limits
from app.agents_image.api.stream_routes import _guard, _try_acquire, reset_sse_guard
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import outbox as outbox_store
from app.agents_image.store import usage as usage_store
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
    reset_rate_limits()
    reset_sse_guard()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()
    image_metrics.reset()
    admin_routes.reset_metrics_cache()
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


# ---------------------------------------------------------------- P2-1
def test_idempotency_key_equivalence_between_body_and_header(env, monkeypatch):
    """同参数用 body.request_id 与 Idempotency-Key 头传入，属于同一个请求（不能误判 409）。"""
    _stub_dispatcher(monkeypatch)
    headers = login(env)
    rid = f"rid-{uuid.uuid4().hex[:10]}"
    body = {"product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1}

    first = env.post("/api/image/tasks", headers=headers, json={**body, "request_id": rid})
    assert first.status_code == 201, first.text
    second = env.post("/api/image/tasks",
                      headers={**headers, "Idempotency-Key": rid}, json=body)
    assert second.status_code == 200, second.text
    assert second.json()["idempotent"] is True
    assert second.json()["batch_id"] == first.json()["batch_id"]


# ---------------------------------------------------------------- P2-3
def test_sse_guard_rebinds_when_event_loop_changes(env, monkeypatch):
    """信号量跨事件循环会被重建（否则 `acquire` 会因 loop 绑定不一致 RuntimeError）。"""
    monkeypatch.setenv("IMAGE_SSE_MAX_CONNECTIONS", "1")
    reset_image_settings()
    reset_sse_guard()

    async def first():
        sem = _guard()
        assert await _try_acquire(sem) is True
        sem.release()
        return id(sem)

    async def second():
        sem = _guard()
        ok = await _try_acquire(sem)
        sem.release()
        return id(sem), ok

    first_id = asyncio.run(first())
    second_id, ok = asyncio.run(second())
    assert ok is True, "换 loop 后必须能重新拿到槽位"
    assert first_id != second_id, "换 loop 必须重建信号量"


# ---------------------------------------------------------------- P2-4
def test_rate_limited_response_has_retry_after(env, monkeypatch):
    monkeypatch.setenv("IMAGE_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("IMAGE_RATE_LIMIT_PER_MINUTE", "1")
    reset_image_settings()
    _stub_dispatcher(monkeypatch)
    headers = login(env)
    body = {"product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1}
    assert env.post("/api/image/tasks", headers=headers, json=body).status_code == 201
    limited = env.post("/api/image/tasks", headers=headers, json=body)
    assert limited.status_code == 429
    assert limited.headers.get("Retry-After") == "60"


# ---------------------------------------------------------------- P2-5
def test_metrics_single_flight_dedupes_concurrent_misses(env):
    calls = {"n": 0}

    async def scenario():
        async def factory():
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return (["bucket"], {"cards": 1})

        results = await asyncio.gather(*[
            admin_routes._metrics_single_flight(("k",), factory) for _ in range(4)])
        return results

    results = asyncio.run(scenario())
    assert calls["n"] == 1, "并发冷缓存只能回源一次"
    assert all(item == (["bucket"], {"cards": 1}) for item in results)


# ---------------------------------------------------------------- P2-6
def test_health_exposes_limit_sse_and_registry_errors(env):
    body = env.get("/api/image/health").json()
    for key in ("rate_limit_enabled", "rate_limit_per_minute", "sse_max_seconds",
                "sse_max_connections", "task_max_seconds", "registry_errors"):
        assert key in body, key
    assert isinstance(body["registry_errors"], list)


# ---------------------------------------------------------------- P2-8
def test_cards_cost_comes_from_usage_flow(env):
    """失败任务的成本只在使用量流水里；卡片必须能算到（双源时这里会是 0）。"""
    user = f"u-cost-{uuid.uuid4().hex[:8]}"
    with session_scope() as session:
        usage_store.record(session, username=user, failed=1, tasks=1,
                           attempted=2, cost=0.4, llm_cost=0.06)
    headers = login(env)
    cards = env.get(f"/api/image/admin/metrics?granularity=day&count=7&username={user}"
                    "&refresh=1", headers=headers).json()["cards"]
    assert cards["cost_cny"] == pytest.approx(0.4), cards
    assert cards["llm_cost_cny"] == pytest.approx(0.06)
    assert cards["cost_source"] == "usage"


def test_outbox_enqueue_dedupes_same_pending_payload(env):
    task_id = f"t-outbox-{uuid.uuid4().hex[:10]}"
    payload = {"task_id": task_id, "results": [{"slot": 1}]}
    with session_scope() as session:
        first = outbox_store.enqueue(session, task_id=task_id, payload=payload)
        second = outbox_store.enqueue(session, task_id=task_id, payload=dict(payload))
        first_id, second_id = int(first.id), int(second.id)
    assert first_id == second_id, "同 task 同 payload 不应重复入队"


# ---------------------------------------------------------------- P2-9
def test_cached_cards_still_refresh_runtime_fields(env):
    """指标缓存命中时，LLM 调用/在途等 runtime 字段必须现算（不能滞后 30s）。"""
    headers = login(env)
    first = env.get("/api/image/admin/metrics?granularity=day&count=7",
                    headers=headers).json()
    assert first["cached"] is False
    baseline = int(first["cards"]["llm_calls"])

    image_metrics.record_llm(calls=5, tokens=100)      # 模拟缓存期间又用了模型
    second = env.get("/api/image/admin/metrics?granularity=day&count=7",
                     headers=headers).json()
    assert second["cached"] is True, "第二次应命中缓存"
    assert int(second["cards"]["llm_calls"]) == baseline + 5, second["cards"]
