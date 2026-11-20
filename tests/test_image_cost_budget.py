"""第五轮 W3：预算闸（P0-2）。

问题：`IMAGE_COST_PER_IMAGE_CNY` 默认 0.0，`IMG_COST_BUDGET_EXCEEDED` 用「单价 × 计划张数」估算，
单价为 0 时这个闸**永远不触发**——真 provider 接上后等于没有成本护栏。
修法：① 真实 provider + 单价≤0 → 创建期 422 `IMG_COST_CONFIG_MISSING`；
      ② 事后按 provider 真实计费对账，超预算落 `IMG_COST_OVERRUN`；
      ③ `/health` 与五张卡暴露 `cost_budget_enabled`。
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


def _wait(env, headers, task_id, timeout=30.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail.get("status") in {"done", "failed", "canceled"}:
            return detail
        time.sleep(0.05)
    return detail


def test_real_provider_with_zero_price_is_rejected(env, monkeypatch):
    """真实 provider + 单价 0 → 创建期直接 422，而不是出完图才发现没有预算闸。"""
    monkeypatch.setenv("IMAGE_PROVIDER", "volcengine")
    monkeypatch.setenv("IMAGE_COST_PER_IMAGE_CNY", "0")
    reset_image_settings()
    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers,
                    json={"product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1})
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error_code"] == "IMG_COST_CONFIG_MISSING"
    assert "IMAGE_COST_PER_IMAGE_CNY" in body["detail"]


def test_real_provider_with_price_passes_gate(env, monkeypatch):
    monkeypatch.setenv("IMAGE_PROVIDER", "volcengine")
    monkeypatch.setenv("IMAGE_COST_PER_IMAGE_CNY", "0.4")
    monkeypatch.setenv("IMAGE_COST_BUDGET_CNY", "10")
    reset_image_settings()
    headers = login(env)

    class _Stub:
        async def submit(self, task_id: str) -> str:
            return task_id

    monkeypatch.setattr("app.agents_image.api.routes.get_dispatcher", lambda: _Stub())
    resp = env.post("/api/image/tasks", headers=headers,
                    json={"product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1})
    assert resp.status_code == 201, resp.text


def test_fake_provider_does_not_require_price(env):
    """fake（零成本）不受此闸影响：默认配置必须能直接建任务。"""
    headers = login(env)

    class _Stub:
        async def submit(self, task_id: str) -> str:
            return task_id

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("app.agents_image.api.routes.get_dispatcher", lambda: _Stub())
    try:
        resp = env.post("/api/image/tasks", headers=headers,
                        json={"product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1})
        assert resp.status_code == 201, resp.text
    finally:
        monkeypatch.undo()


def test_health_and_cards_expose_budget_flag(env, monkeypatch):
    monkeypatch.setenv("IMAGE_COST_PER_IMAGE_CNY", "0.4")
    monkeypatch.setenv("IMAGE_COST_BUDGET_CNY", "5")
    reset_image_settings()
    headers = login(env)
    health = env.get("/api/image/health").json()
    assert health["cost_budget_enabled"] is True
    assert health["cost_price_cny"] == 0.4

    cards = env.get("/api/image/admin/metrics?granularity=day&count=7&refresh=1",
                    headers=headers).json()["cards"]
    assert cards["cost_budget_enabled"] is True


def test_overrun_warning_uses_real_provider_cost(env, monkeypatch):
    """provider 真实计费高于预算上限 → 任务落 IMG_COST_OVERRUN（创建期估算救不了这种情况）。"""
    from app.agents_image.providers.fake import FakeProvider
    from app.agents_image.providers.image_provider import ImageResult

    monkeypatch.setenv("IMAGE_COST_BUDGET_CNY", "0.5")
    reset_image_settings()
    original = FakeProvider.generate

    async def pricey(self, req):
        result = await original(self, req)
        return ImageResult(images=result.images, mime=result.mime, provider=result.provider,
                           model=result.model, cost_cny=2.0,
                           latency_ms=result.latency_ms, meta=result.meta)

    monkeypatch.setattr(FakeProvider, "generate", pricey)
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图",
                             "slot_count": 2, "image_count": 1})
    assert created.status_code == 201, created.text
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert detail["status"] == "done"
    assert any(str(item).startswith("IMG_COST_OVERRUN") for item in detail["warnings"]), \
        detail["warnings"]
