"""第十轮：生图 API 最大等待 360s 的落地（任务级截止时间 + 配套配置对齐）。

调用方契约：单次生图 API 最多等 360s，超时按失败处理。因此服务端必须：
- 到点后不再发起新的付费调用（`IMAGE_TASK_MAX_SECONDS`）；
- 超时失败时把**已发生**的发起量/张数/成本按真实值记账（不能因超时丢账）；
- 任务租约必须 ≥ 最大任务时长（否则看门狗会在截止前把活任务判"租约过期"重投）；
- SSE 单连接时长必须覆盖最大等待（否则任务没结束 SSE 先断）。
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from app.agents_image.config import get_image_settings, reset_image_settings
from app.agents_image.dispatch.local_generator import LocalGenerator
from app.agents_image.generator import handler as generator_handler
from app.agents_image.graph import execute_task
from app.agents_image.ids import new_task_id
from app.agents_image.nodes import plan_slots
from app.agents_image.png import placeholder_png
from app.agents_image.providers.image_provider import ImageResult
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.sources.fake_catalog import _PRODUCTS, FakeCatalog
from app.agents_image.state import ImageTaskState, SlotPlan
from app.agents_image.store import assets as asset_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store import usage as usage_store
from app.agents_image.store.schema import ensure_image_schema
from app.core.errors import ImageError
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    monkeypatch.delenv("IMAGE_PRODUCT_SOURCE", raising=False)
    reset_image_settings()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()
    ensure_image_schema()
    yield
    reset_image_settings()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()


def test_default_windows_follow_360s_contract(env):
    """默认值必须自洽：任务最长 360s → 租约 ≥ 360s、SSE ≥ 360s。"""
    settings = get_image_settings()
    assert settings.task_max_seconds == 360
    assert settings.task_lease_seconds >= settings.task_max_seconds
    assert settings.sse_max_seconds >= settings.task_max_seconds


def test_handler_refuses_to_spend_after_deadline(env):
    """已经过截止时间：不得再调用 provider，直接 IMG_TIMEOUT。"""
    calls = {"n": 0}

    class _Provider:
        name = "stub"

        async def generate(self, req):  # noqa: ANN001
            calls["n"] += 1
            return ImageResult(images=[b"x"], mime="image/png", provider="stub",
                               model="m", cost_cny=0.0, latency_ms=1)

    payload = {
        "provider": "fake",
        "refs": [{"data": placeholder_png("d", 16, 16), "mime": "image/png"}],
        "slots": [{"slot": 1, "prompt": "白底主图", "n": 1}],
        "width": 64, "height": 64, "preserve": [],
        "deadline_at": time.time() - 1,          # 已经超时
    }
    real_get_provider = generator_handler.get_provider
    generator_handler.get_provider = lambda name: _Provider()
    try:
        with pytest.raises(ImageError) as caught:
            asyncio.run(generator_handler.run(payload))
    finally:
        generator_handler.get_provider = real_get_provider
    assert caught.value.code == "IMG_TIMEOUT"
    assert calls["n"] == 0, "超时后不得再花钱"


def _seed_task(tmp_path, monkeypatch) -> str:
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    reset_image_settings()
    ensure_image_schema()
    user = f"u-deadline-{uuid.uuid4().hex[:8]}"
    with session_scope() as session:
        ref = asset_store.save_bytes(session, placeholder_png("dl-ref", 64, 64),
                                     kind="ref", product_id="P-1001", username=user)
        state = ImageTaskState(prompt="白底主图", aspect="1:1", count=1, refs=[])
        state.slots = [SlotPlan(**item) for item in plan_slots.build_slots(
            state, policy=asyncio.run(FakeCatalog().get_plan_policy()))]
        task = task_store.create_task(
            session, task_id=new_task_id(), username=user, department="女装",
            prompt="白底主图", platform="淘系", aspect="1:1", n=1,
            ref_asset_ids=[ref.asset_id], product_id="P-1001",
            attrs=dict(_PRODUCTS["P-1001"]["attrs"]),
            slots=[item.model_dump() for item in state.slots], image_count=1,
        )
        asset_store.bind_refs_to_task(session, [ref.asset_id], task.task_id)
        return task.task_id


def test_task_times_out_as_failed_but_keeps_spent_cost(env, tmp_path, monkeypatch):
    """超时 → 任务 failed(IMG_TIMEOUT)，但已生成的 1 张与 ¥0.4 必须记账。"""
    from app.agents_image.providers.fake import FakeProvider

    monkeypatch.setenv("IMAGE_TASK_MAX_SECONDS", "1")
    reset_image_settings()

    async def slow_generate(self, req):  # noqa: ANN001, ARG001
        await asyncio.sleep(1.2)                     # 单次调用就超过 1s 预算
        return ImageResult(images=[placeholder_png("slow", 64, 64)], mime="image/png",
                           provider="fake", model="slow", cost_cny=0.4, latency_ms=1200)

    monkeypatch.setattr(FakeProvider, "generate", slow_generate)
    task_id = _seed_task(tmp_path, monkeypatch)

    async def run_until_terminal():
        generator = LocalGenerator()
        await generator.submit(task_id)
        for _ in range(400):
            with session_scope() as session:
                task = task_store.get_task(session, task_id)
                if task is not None and task.status in {"done", "failed"}:
                    return task.status
            await asyncio.sleep(0.05)
        return None

    assert asyncio.run(run_until_terminal()) == "failed"
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        status, code = task.status, task.error_code
        attempted = int(task.attempted_images or 0)
        spent = float(task.external_cost_cny or 0.0)
        username = task.username
        mine = usage_store.totals(session, username=username)
    assert status == "failed"
    assert code == "IMG_TIMEOUT", code
    assert attempted == 1, "已生成张数必须记账"
    assert spent == pytest.approx(0.4)
    assert mine["images"] == 1 and mine["cost_cny"] == pytest.approx(0.4)


def test_execute_task_raises_timeout_with_stats(env, tmp_path, monkeypatch):
    """直接跑 execute_task：超时异常上必须带已发生的 generated/cost（供 dispatcher 记账）。"""
    from app.agents_image.providers.fake import FakeProvider

    monkeypatch.setenv("IMAGE_TASK_MAX_SECONDS", "1")
    reset_image_settings()
    async def slow_generate(self, req):  # noqa: ANN001, ARG001
        await asyncio.sleep(1.2)
        return ImageResult(images=[placeholder_png("slow2", 64, 64)], mime="image/png",
                           provider="fake", model="slow", cost_cny=0.3, latency_ms=1200)

    monkeypatch.setattr(FakeProvider, "generate", slow_generate)
    task_id = _seed_task(tmp_path, monkeypatch)
    with pytest.raises(ImageError) as caught:
        asyncio.run(execute_task(task_id))
    assert caught.value.code == "IMG_TIMEOUT"
    stats = generator_handler.failure_stats_of(caught.value)
    assert stats["generated"] == 1
    assert stats["cost_cny"] == pytest.approx(0.3)
