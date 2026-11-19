"""第四轮 W5：失败路径的发起量（P1-6）与取消竞态的成本漏记（P2-11）。

口径：
- 发起量 = 第三方真实发起次数（含限流重试）；失败也要记，否则 API 可用率虚高；
- 取消竞态：provider 已产出、任务先被取消 → 成本必须补记一次；已结算过则只留痕不重复记。
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
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
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


def _payload(slots: int = 1) -> dict:
    from app.agents_image.png import placeholder_png

    return {"provider": "fake",
            "refs": [{"data": placeholder_png("ref", 64, 64), "mime": "image/png"}],
            "slots": [{"slot": index + 1, "prompt": "白底主图", "n": 1}
                      for index in range(slots)],
            "width": 1024, "height": 1024, "preserve": []}


# ---------------------------------------------------------------- P1-6：异常带上发起量
def test_retry_exhausted_error_carries_provider_calls(env, monkeypatch):
    from app.agents_image.providers.fake import FakeProvider

    monkeypatch.setenv("IMAGE_PROVIDER_RETRIES", "2")
    reset_image_settings()
    state = {"calls": 0}

    async def down(self, req):
        state["calls"] += 1
        raise_image("IMG_PROVIDER_UNAVAILABLE", "5xx")

    monkeypatch.setattr(FakeProvider, "generate", down)
    with pytest.raises(ImageError) as caught:
        asyncio.run(generator_handler.run(_payload()))
    assert state["calls"] == 3                     # 首调 + 2 次重试
    assert caught.value.extra["provider_calls"] == 3


def test_run_aggregates_calls_across_slots_before_failing(env, monkeypatch):
    """第 1 个图位成功、第 2 个图位失败：整轮发起量 = 1 + 失败位的重试次数。"""
    from app.agents_image.providers.fake import FakeProvider

    monkeypatch.setenv("IMAGE_PROVIDER_RETRIES", "1")
    reset_image_settings()
    original = FakeProvider.generate
    state = {"slot": 0}

    async def flaky(self, req):
        state["slot"] += 1
        if state["slot"] in (2, 3):        # 第 2 个图位：首调 + 1 次重试都失败
            raise_image("IMG_PROVIDER_RATE_LIMITED", "限流")
        return await original(self, req)

    monkeypatch.setattr(FakeProvider, "generate", flaky)
    with pytest.raises(ImageError) as caught:
        asyncio.run(generator_handler.run(_payload(slots=2)))
    assert state["slot"] == 3                          # 1 成功 + 失败位 2 次
    assert caught.value.extra["provider_calls"] == 3   # 1 成功 + 2 次重试


def test_failed_task_records_provider_calls(env, monkeypatch):
    """端到端：任务失败后 `task.provider_calls` 与使用量流水都要有发起量。"""
    from app.agents_image.providers.fake import FakeProvider

    monkeypatch.setenv("IMAGE_PROVIDER_RETRIES", "1")
    reset_image_settings()

    async def down(self, req):
        raise_image("IMG_PROVIDER_UNAVAILABLE", "5xx")

    monkeypatch.setattr(FakeProvider, "generate", down)
    # 用专属账号隔离计数（测试库是 session 级的，全局 totals 会被别的用例污染）
    user = f"u-pcf-{uuid.uuid4().hex[:8]}"
    password = "pw-pcf-123"
    with session_scope() as session:
        user_store.create_user(session, username=user, password=password, role="user")
        quota_store.grant(session, user, period_type="day", period_limit=10)
    headers = login(env, user, password)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1})
    assert created.status_code == 201, created.text
    task_id = created.json()["tasks"][0]["task_id"]
    detail = _wait(env, headers, task_id)
    assert detail["status"] == "failed"
    assert detail["provider_calls"] >= 2, detail

    with session_scope() as session:
        mine = usage_store.totals(session, username=user)
        task_calls = int(task_store.get_task(session, task_id).provider_calls or 0)
    assert task_calls >= 2
    assert mine["provider_calls"] >= 2
    # 失败没有成功出图 → 使用量 0，但发起量 > 0：可用率必须是 0
    assert mine["images"] == 0
    cards = env.get(f"/api/image/admin/metrics?granularity=day&count=7&username={user}",
                    headers=login(env)).json()["cards"]
    assert cards["api_availability"] == 0.0
