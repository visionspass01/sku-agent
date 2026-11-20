"""第十一轮 P2：小问题合集（P2-1/P2-2/P2-3/P2-4/P2-6/P2-7）。

P2-1 发起量口径：截止时间在"下一次重试前"命中时，那次**没发出**的调用不能记账。
P2-2 前端建任务必须带 `request_id`（= 服务端幂等键）。
P2-3 老记录（无 `payload_hash`）重放时补写指纹，否则永远挡不住"同 key 换参数"。
P2-4 线程池被同步 IO 打满时必须有指标（调用数/在飞/占用时长）。
P2-6 SSE `timed_out` 后前端要续看，不能停在"等待超时"。
P2-7 SSE 也要带 `X-Trace-Id`。
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.api import routes
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.generator import handler as generator_handler
from app.agents_image.providers.image_provider import ImageRequest
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.core.errors import ImageError
from app.db.engine import session_scope

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


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
    generator_handler.reset_provider_semaphore()
    ensure_image_schema()
    with session_scope() as session:
        user_store.ensure_seed_users(session)
    yield app_client
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()
    generator_handler.reset_provider_semaphore()


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
def test_deadline_before_first_call_counts_zero(env, monkeypatch):
    """截止时间在第一次调用前就命中：一次都没发出去，发起量必须是 0（不是 1）。"""
    monkeypatch.setenv("IMAGE_PROVIDER_RETRIES", "2")
    reset_image_settings()

    calls = {"n": 0}

    class _Never:
        name = "never"

        async def generate(self, req: ImageRequest):
            calls["n"] += 1
            raise AssertionError("截止时间已过，不该再发调用")

    with pytest.raises(ImageError) as info:
        asyncio.run(generator_handler._generate_with_retry(
            _Never(), ImageRequest(prompt="A", n=1), deadline_at=time.time() - 1))
    assert info.value.code == "IMG_TIMEOUT"
    assert calls["n"] == 0
    assert generator_handler._provider_calls_of(info.value) == 0


def test_deadline_between_retries_counts_only_real_calls(env, monkeypatch):
    """第 1 次真实调用失败（可重试），退避后到点：只应记 1 次（旧实现记 2 次）。"""
    monkeypatch.setenv("IMAGE_PROVIDER_RETRIES", "3")
    monkeypatch.setenv("IMAGE_PROVIDER_RETRY_BACKOFF_SECONDS", "0.05")
    reset_image_settings()

    calls = {"n": 0}

    class _SlowFail:
        name = "slow"

        async def generate(self, req: ImageRequest):
            calls["n"] += 1
            raise ImageError("IMG_PROVIDER_RATE_LIMITED", "限流", retryable=True)

    with pytest.raises(ImageError) as info:
        asyncio.run(generator_handler._generate_with_retry(
            _SlowFail(), ImageRequest(prompt="A", n=1), deadline_at=time.time() + 0.02))
    assert calls["n"] == 1
    assert generator_handler._provider_calls_of(info.value) == 1


def test_retry_success_counts_every_real_call(env, monkeypatch):
    monkeypatch.setenv("IMAGE_PROVIDER_RETRIES", "2")
    monkeypatch.setenv("IMAGE_PROVIDER_RETRY_BACKOFF_SECONDS", "0")
    reset_image_settings()

    class _Flaky:
        name = "flaky"

        def __init__(self) -> None:
            self.failed = False

        async def generate(self, req: ImageRequest):
            from app.agents_image.providers.image_provider import ImageResult

            if not self.failed:
                self.failed = True
                raise ImageError("IMG_PROVIDER_RATE_LIMITED", "限流", retryable=True)
            return ImageResult(images=[b"x"], mime="image/png", provider="fake",
                               model="m", cost_cny=0.0, latency_ms=1)

    result, calls = asyncio.run(generator_handler._generate_with_retry(
        _Flaky(), ImageRequest(prompt="A", n=1)))
    assert calls == 2
    assert result.images


# ---------------------------------------------------------------- P2-3
def test_legacy_idempotency_row_backfills_fingerprint(env, monkeypatch):
    """老记录（没有 payload_hash）先兼容重放，同时补写指纹；换参数再发就 409。"""
    _stub_dispatcher(monkeypatch)
    from app.agents_image.store.models import ImageRequest

    legacy_id = "legacy-req-0001"
    with session_scope() as session:
        session.add(ImageRequest(request_id=legacy_id, status="done", batch_id="b-legacy",
                                 username="admin", payload_hash="",
                                 response_json={"batch_id": "b-legacy", "tasks": [],
                                                "ref_source": "mcp"}))
    headers = login(env)
    body = {"request_id": legacy_id, "product_ids": ["P-3001"], "prompt": "白底主图",
            "image_count": 1}
    first = env.post("/api/image/tasks", headers=headers, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["idempotent"] is True
    assert first.json()["batch_id"] == "b-legacy"

    with session_scope() as session:
        row = session.get(ImageRequest, legacy_id)
        backfilled = str(row.payload_hash or "")
    assert backfilled == routes._request_fingerprint(body), "老记录必须补写本次请求的指纹"

    changed = dict(body, prompt="换成白底 + 场景图")
    second = env.post("/api/image/tasks", headers=headers, json=changed)
    assert second.status_code == 409, second.text
    assert second.json()["error_code"] == "IMG_TASK_CONFLICT"


# ---------------------------------------------------------------- P2-4
def test_threadpool_stats_exported_to_metrics(env):
    from app.agents_image import observability
    from app.observability import trace as trace_mod

    trace_mod.reset_threadpool_stats()
    assert asyncio.run(trace_mod.run_in_trace(lambda: 1)) == 1
    stats = trace_mod.threadpool_stats()
    assert stats["calls"] == 1
    assert stats["in_flight"] == 0
    assert stats["busy_seconds"] >= 0.0

    body = observability._render_builtin()
    assert "image_threadpool_calls_total 1" in body
    assert "image_threadpool_in_flight 0" in body
    assert "image_threadpool_busy_seconds_max" in body


def test_threadpool_in_flight_counts_concurrent_tasks(env):
    from app.observability import trace as trace_mod

    trace_mod.reset_threadpool_stats()
    started = threading.Barrier(2)

    def _block() -> int:
        started.wait(timeout=5)
        time.sleep(0.05)
        return 0

    async def scenario():
        first = asyncio.create_task(trace_mod.run_in_trace(_block))
        second = asyncio.create_task(trace_mod.run_in_trace(_block))
        await asyncio.sleep(0.02)
        snapshot = trace_mod.threadpool_stats()
        await asyncio.gather(first, second)
        return snapshot

    snapshot = asyncio.run(scenario())
    assert snapshot["in_flight"] >= 1, "并发卸载必须体现在在飞计数上"
    assert snapshot["calls"] == 2


# ---------------------------------------------------------------- P2-2 / P2-6 / P2-7（前端契约）
def test_frontend_sends_request_id_for_idempotency(env):
    js = (WEB / "image.js").read_text(encoding="utf-8")
    assert "function requestIdFor(" in js
    assert "payload.request_id = requestIdFor(payload)" in js
    # 成功后才换新键：失败重试复用同一个 request_id
    assert "state.pendingRequest = null" in js
    assert "pendingRequest:" in js


def test_frontend_stream_carries_trace_and_resumes_after_timeout(env):
    js = (WEB / "image.js").read_text(encoding="utf-8")
    # P2-7：SSE 也带 trace
    assert '"X-Trace-Id": streamTrace' in js
    # P2-6：timed_out 之后重开流续看（最多 3 次）
    assert "data.timed_out && timeouts < 3" in js
    assert "let timeouts = 0;" in js
