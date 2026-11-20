"""第六轮 P1-6：`/metrics` 的发起量/成本必须与使用量流水同源。

上一版只在 graph 的结算路径累加 `record_provider_calls`，而 provider 抛异常时任务由
调度器直接 `settlement.settle_cancel` 收尾（没进 graph 结算）→ Prometheus 的发起量少计，
与 `image_usage.provider_calls` 打架（"API 可用率"两套口径）。
现在统一放在结算里（`_record_provider_metrics`），由 `settled_at` CAS 保证只记一次。
"""

from __future__ import annotations

import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import quota as quota_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store import usage as usage_store
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("IMAGE_PROVIDER_RETRY_BACKOFF_SECONDS", "0")
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
        if detail.get("status") in {"done", "failed", "canceled"}:
            return detail
        time.sleep(0.05)
    return detail


def _usage_provider_calls() -> int:
    with session_scope() as session:
        return int(usage_store.totals(session)["provider_calls"])


def test_success_path_counts_provider_calls_once(env):
    headers = login(env)
    before = image_metrics.snapshot()["provider_calls"]
    usage_before = _usage_provider_calls()
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图",
                             "slot_count": 2, "image_count": 1})
    assert created.status_code == 201, created.text
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert detail["status"] == "done"
    task_calls = int(detail["provider_calls"] or 1)
    after = image_metrics.snapshot()["provider_calls"]
    usage_after = _usage_provider_calls()
    assert after - before >= task_calls
    # /metrics 与使用量流水口径一致（并发别的任务也会在两边同时增长，差值必须相等）
    assert (after - before) == (usage_after - usage_before), (
        after - before, usage_after - usage_before)


def test_provider_failure_path_counts_provider_calls(env, monkeypatch):
    """provider 抛异常（不进 graph 结算）：调度器 settle_cancel 也必须计入发起量。"""
    from app.agents_image.providers.fake import FakeProvider
    from app.core.errors import raise_image

    monkeypatch.setenv("IMAGE_PROVIDER_RETRIES", "1")
    reset_image_settings()

    async def down(self, req):
        raise_image("IMG_PROVIDER_UNAVAILABLE", "5xx")

    monkeypatch.setattr(FakeProvider, "generate", down)
    headers = login(env)
    before = image_metrics.snapshot()["provider_calls"]
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图",
                             "slot_count": 2, "image_count": 1})
    assert created.status_code == 201, created.text
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert detail["status"] == "failed"
    after = image_metrics.snapshot()["provider_calls"]
    assert after - before >= 2, (before, after)


def test_cancel_path_counts_provider_calls(env):
    headers = login(env)

    class _Stub:
        async def submit(self, task_id: str) -> str:
            return task_id

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("app.agents_image.api.routes.get_dispatcher", lambda: _Stub())
    try:
        created = env.post("/api/image/tasks", headers=headers,
                           json={"product_ids": ["P-3001"], "prompt": "白底主图",
                                 "slot_count": 2, "image_count": 1})
        assert created.status_code == 201, created.text
        task_id = created.json()["tasks"][0]["task_id"]
        before = image_metrics.snapshot()["provider_calls"]
        usage_before = _usage_provider_calls()
        resp = env.post(f"/api/image/tasks/{task_id}/cancel", headers=headers)
        assert resp.status_code == 200, resp.text
        after = image_metrics.snapshot()["provider_calls"]
        usage_after = _usage_provider_calls()
    finally:
        monkeypatch.undo()
    # 未调模型就取消：本任务发起量=0；并发别的任务也会在两边同时增长，差值必须相等
    assert (after - before) == (usage_after - usage_before), (after - before, usage_after - usage_before)

    # 重复结算不会重复计数（settled_at CAS）
    before2 = image_metrics.snapshot()["provider_calls"]
    env.post(f"/api/image/tasks/{task_id}/cancel", headers=headers)
    assert image_metrics.snapshot()["provider_calls"] == before2


def test_settle_counts_once_even_if_called_twice(env):
    """直接调两次 settle_cancel：只有第一次记账。"""
    from app.agents_image import settlement

    with session_scope() as session:
        task_store.create_task(session, task_id="20261140000000k1", username="admin",
                               department=None, prompt="白底主图", platform="淘系",
                               aspect="1:1", n=1, ref_asset_ids=[], product_id="P-3001",
                               image_count=1)
        quota_store.grant(session, "admin", period_type="day", period_limit=10)
    before = image_metrics.snapshot()["provider_calls"]
    with session_scope() as session:
        settlement.settle_cancel(session, "20261140000000k1", provider_calls=3, cost=0.3)
    with session_scope() as session:
        settlement.settle_cancel(session, "20261140000000k1", provider_calls=3, cost=0.3)
    after = image_metrics.snapshot()
    assert after["provider_calls"] - before == 3, after
