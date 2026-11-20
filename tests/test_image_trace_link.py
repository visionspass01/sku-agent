"""第十轮 P1-1：生图侧必须真正建立 trace 链路（响应头/响应体/后台日志一致）。

上一版：`trace_id_of` 各自生成 uuid、中间件又生成一个（响应头≠响应体）；
format 里 trace 恒为 `-`（没人 set contextvar）；MCP/中台 X-Trace-Id 取不到值；
后台执行体也没有绑定任务的 trace_id。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.dispatch.local_generator import LocalGenerator
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import tasks as task_store
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope
from app.observability import trace as trace_mod


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


def _stub_dispatcher(monkeypatch) -> None:
    class _Stub:
        async def submit(self, task_id: str) -> str:
            return task_id

    monkeypatch.setattr("app.agents_image.api.routes.get_dispatcher", lambda: _Stub())


def test_response_header_body_and_task_share_one_trace(env, monkeypatch):
    """未带 X-Trace-Id 时：响应头、响应体、任务台账三处必须是**同一个** trace。"""
    _stub_dispatcher(monkeypatch)
    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers,
                    json={"product_ids": ["P-3001"], "prompt": "白底主图",
                          "image_count": 1})
    assert resp.status_code == 201, resp.text
    body_trace = resp.json()["trace_id"]
    header_trace = resp.headers.get("X-Trace-Id")
    assert body_trace and header_trace
    assert header_trace == body_trace, (header_trace, body_trace)

    task_id = resp.json()["tasks"][0]["task_id"]
    with session_scope() as session:
        task_trace = task_store.get_task(session, task_id).trace_id
    assert task_trace == body_trace, "任务台账里的 trace 必须与响应一致"


def test_incoming_trace_header_is_reused(env, monkeypatch):
    _stub_dispatcher(monkeypatch)
    headers = login(env)
    headers["X-Trace-Id"] = "trace-from-caller"
    resp = env.post("/api/image/tasks", headers=headers,
                    json={"product_ids": ["P-3001"], "prompt": "白底主图",
                          "image_count": 1})
    assert resp.status_code == 201, resp.text
    assert resp.json()["trace_id"] == "trace-from-caller"
    assert resp.headers["X-Trace-Id"] == "trace-from-caller"


def test_error_response_trace_matches_header():
    """错误响应：body.trace_id 与 X-Trace-Id 头一致（同一个请求只一个 trace）。"""
    from starlette.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.post("/api/image/tasks", json={"product_ids": ["P-3001"]})
    assert resp.status_code in {401, 422}
    assert resp.headers["X-Trace-Id"] == resp.json()["trace_id"]


def test_run_in_trace_carries_context_to_thread():
    """trace 上下文必须能跨线程池（`run_in_trace` 显式快照，不依赖实现细节）。"""

    async def scenario() -> tuple[str, str]:
        with trace_mod.trace_context("t-thread"):
            plain = await asyncio.to_thread(trace_mod.current_trace_id)
            carried = await trace_mod.run_in_trace(trace_mod.current_trace_id)
        return plain, carried

    plain, carried = asyncio.run(scenario())
    assert carried == "t-thread"
    # asyncio.to_thread 在 3.11+ 会 copy_context，同样能带过去；这里只保证两种写法都不丢 trace，
    # 生图侧统一用 run_in_trace 是为了语义显式、不依赖标准库实现细节。
    assert plain == "t-thread"


def test_dispatcher_binds_task_trace_for_background_logs(env, monkeypatch):
    """后台执行体必须绑定任务的 trace：执行期日志才能串回创建请求。"""
    seen: dict = {"trace": None}

    async def fake_execute(task_id: str) -> None:
        seen["trace"] = trace_mod.current_trace_id()

    monkeypatch.setattr("app.agents_image.graph.execute_task", fake_execute)
    task_id = f"t-trace-{uuid.uuid4().hex[:8]}"
    with session_scope() as session:
        task_store.create_task(session, task_id=task_id, username="admin",
                               department="女装", prompt="白底主图", platform="淘系",
                               aspect="1:1", n=1, ref_asset_ids=[],
                               product_id="P-3001", image_count=1,
                               trace_id="t-bg-1")

    async def run():
        generator = LocalGenerator()
        await generator.submit(task_id)
        for _ in range(200):
            with session_scope() as session:
                status = task_store.get_task(session, task_id).status
            if status in {"done", "failed"} or seen["trace"] is not None:
                return
            await asyncio.sleep(0.02)

    asyncio.run(run())
    assert seen["trace"] == "t-bg-1", seen
