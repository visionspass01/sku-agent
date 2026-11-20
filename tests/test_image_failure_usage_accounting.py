"""第七轮 P1-2：provider 中途失败时，已生成张数与成本不能被丢掉。

上一版只在异常上带 `provider_calls`（发起量），`handler.run` 已产出的图片与成本、
graph 前几轮的花费全部蒸发：dispatcher 走失败结算时记成「attempted=0、成本 0」，
使用量/成本口径被系统性低估（第三方已经计费）。
现在 handler 挂 `generated`/`cost_cny`，graph 把前几轮的量合并进来，dispatcher 按
真实值结算并回写任务。
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.generator import handler as generator_handler
from app.agents_image.providers.image_provider import ImageResult
from app.agents_image.state import PromptDraft
from app.agents_image.store import quota as quota_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store import usage as usage_store
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.core.errors import ImageError, raise_image
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("IMAGE_PROVIDER_RETRY_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("IMAGE_PROVIDER_RETRIES", "0")
    monkeypatch.delenv("IMAGE_PRODUCT_SOURCE", raising=False)
    reset_image_settings()
    reset_throttle()
    from app.agents_image.sources import batch_context
    from app.agents_image.sources import product_source as ps

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


def _payload(slots: int = 2) -> dict:
    from app.agents_image.png import placeholder_png

    return {"provider": "fake",
            "refs": [{"data": placeholder_png("ref", 64, 64), "mime": "image/png"}],
            "slots": [{"slot": index + 1, "prompt": "白底主图", "n": 1}
                      for index in range(slots)],
            "width": 1024, "height": 1024, "preserve": []}


def _ok_result(cost: float = 0.4) -> ImageResult:
    return ImageResult(images=[b"img-bytes"], mime="image/png", provider="fake",
                       model="fake-placeholder", cost_cny=cost, latency_ms=1)


# ---------------------------------------------------------------- handler 层
def test_handler_carries_generated_and_cost_on_failure(env, monkeypatch):
    """第 1 个图位成功、第 2 个失败：异常上要带「1 张 / ¥0.4 / 2 次发起（含失败位）」。"""
    from app.agents_image.providers.fake import FakeProvider

    state = {"slot": 0}

    async def flaky(self, req):
        state["slot"] += 1
        if state["slot"] >= 2:
            raise_image("IMG_PROVIDER_UNAVAILABLE", "5xx")
        return _ok_result(0.4)

    monkeypatch.setattr(FakeProvider, "generate", flaky)
    with pytest.raises(ImageError) as caught:
        asyncio.run(generator_handler.run(_payload(slots=2)))
    stats = generator_handler.failure_stats_of(caught.value)
    assert stats == {"provider_calls": 2, "generated": 1, "cost_cny": pytest.approx(0.4)}


# ---------------------------------------------------------------- graph 层
def test_graph_merges_prior_attempts_into_failure_stats(env, monkeypatch):
    """重试轮次失败时，前一轮的张数/成本/发起量必须合并进异常。"""
    from app.agents_image import graph

    class _Settings:
        provider = "fake"

    def _boom(payload):
        exc = ImageError(code="IMG_PROVIDER_UNAVAILABLE", detail="5xx",
                         extra={"provider_calls": 1, "generated": 1, "cost_cny": 0.4})
        raise exc

    async def fake_run(payload):
        _boom(payload)

    monkeypatch.setattr(graph.generator_handler, "run", fake_run)
    drafts = [PromptDraft(slot=1, prompt="白底主图"), PromptDraft(slot=2, prompt="细节图")]
    with pytest.raises(ImageError) as caught:
        asyncio.run(graph._generate_or_attach_partial(
            _Settings(), [], drafts, attempted=2, spent=0.3, provider_calls=2))
    stats = graph.generator_handler.failure_stats_of(caught.value)
    assert stats == {"provider_calls": 3, "generated": 3, "cost_cny": pytest.approx(0.7)}


# ---------------------------------------------------------------- 端到端
def _login(client, username="admin", password="hhw123") -> dict:
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


def test_failed_task_keeps_generated_images_and_cost(env, monkeypatch):
    """端到端：2 张的图位第 2 张失败 → 任务/使用量里仍记 1 张与 ¥0.4。"""
    from app.agents_image.providers.fake import FakeProvider

    state = {"slot": 0}

    async def flaky(self, req):
        state["slot"] += 1
        if state["slot"] >= 2:
            raise_image("IMG_PROVIDER_UNAVAILABLE", "5xx")
        return _ok_result(0.4)

    monkeypatch.setattr(FakeProvider, "generate", flaky)
    user = f"u-p12-{uuid.uuid4().hex[:8]}"
    password = "pw-p12-123"
    with session_scope() as session:
        user_store.create_user(session, username=user, password=password, role="user",
                               department="女装")
        quota_store.grant(session, user, period_type="day", period_limit=10)
    headers = _login(env, user, password)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 2})
    assert created.status_code == 201, created.text
    task_id = created.json()["tasks"][0]["task_id"]
    detail = _wait(env, headers, task_id)
    assert detail["status"] == "failed", detail

    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        attempted = int(task.attempted_images or 0)
        cost = float(task.external_cost_cny or 0.0)
        mine = usage_store.totals(session, username=user)
    assert attempted == 1, attempted
    assert cost == pytest.approx(0.4), cost
    assert mine["images"] == 1, mine
    assert mine["cost_cny"] == pytest.approx(0.4), mine
    # 发起量仍然要记（可用率分母）
    assert mine["provider_calls"] >= 1, mine
