"""第十一轮 P1-1：失败/崩溃路径的"已发生成本"必须增量落库。

上一版只有结算/失败落库时才写 provider_calls/attempted/cost：进程被 kill、租约被抢时
这些值全丢，看门狗重投再花一遍钱，而账一直是 0。
现在：每完成一个图位就增量写任务台账；看门狗接管"租约过期的 running"时先把已发生成本
结算进使用量流水、再把台账清零给新一次执行。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.agents_image.config import reset_image_settings
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
from app.db.engine import session_scope


class _Crash(BaseException):
    """模拟进程级崩溃：dispatcher 只 catch `Exception`，所以它会一路抛出（任务停在 running）。"""


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


def _seed_task(tmp_path, monkeypatch, *, slots: int = 2) -> str:
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    reset_image_settings()
    ensure_image_schema()
    user = f"u-partial-{uuid.uuid4().hex[:8]}"
    with session_scope() as session:
        ref = asset_store.save_bytes(session, placeholder_png("pf-ref", 64, 64),
                                     kind="ref", product_id="P-1001", username=user)
        state = ImageTaskState(prompt="白底主图", aspect="1:1", count=slots, refs=[])
        state.slots = [SlotPlan(**item) for item in plan_slots.build_slots(
            state, policy=asyncio.run(FakeCatalog().get_plan_policy()),
            slot_count=slots)]
        task = task_store.create_task(
            session, task_id=new_task_id(), username=user, department="女装",
            prompt="白底主图", platform="淘系", aspect="1:1", n=slots,
            ref_asset_ids=[ref.asset_id], product_id="P-1001",
            attrs=dict(_PRODUCTS["P-1001"]["attrs"]),
            slots=[item.model_dump() for item in state.slots], image_count=slots,
        )
        asset_store.bind_refs_to_task(session, [ref.asset_id], task.task_id)
        return task.task_id


def test_handler_reports_cumulative_stats_per_slot(env, monkeypatch):
    """handler.run 每完成一个图位回调一次（累计发起量/张数/成本）。"""
    from app.agents_image.providers.fake import FakeProvider

    monkeypatch.setenv("IMAGE_PROVIDER_RETRIES", "0")
    reset_image_settings()
    real_generate = FakeProvider.generate

    async def priced(self, req):  # noqa: ANN001
        result = await real_generate(self, req)
        return ImageResult(images=result.images, mime=result.mime, provider=result.provider,
                           model=result.model, cost_cny=0.2, latency_ms=result.latency_ms,
                           meta=result.meta)

    monkeypatch.setattr(FakeProvider, "generate", priced)
    calls: list[dict] = []

    async def on_slot(stats):
        calls.append(dict(stats))

    payload = {
        "provider": "fake",
        "refs": [{"data": placeholder_png("p", 16, 16), "mime": "image/png"}],
        "slots": [{"slot": 1, "prompt": "白底主图", "n": 1},
                  {"slot": 2, "prompt": "细节图", "n": 1}],
        "width": 64, "height": 64, "preserve": [],
    }
    asyncio.run(generator_handler.run(payload, on_slot=on_slot))
    assert len(calls) == 2
    assert calls[0] == {"provider_calls": 1, "generated": 1, "cost_cny": pytest.approx(0.2)}
    assert calls[1] == {"provider_calls": 2, "generated": 2, "cost_cny": pytest.approx(0.4)}


def test_crash_keeps_incremental_cost_on_task(env, tmp_path, monkeypatch):
    """模拟进程崩溃（BaseException 逃出执行体）：任务台账里必须已经有已发生成本。"""
    from app.agents_image.providers.fake import FakeProvider

    monkeypatch.setenv("IMAGE_PROVIDER_RETRIES", "0")
    reset_image_settings()
    real_generate = FakeProvider.generate
    state = {"calls": 0}

    async def flaky(self, req):  # noqa: ANN001
        state["calls"] += 1
        if state["calls"] >= 2:
            raise _Crash("模拟进程崩溃")
        result = await real_generate(self, req)
        return ImageResult(images=result.images, mime=result.mime, provider=result.provider,
                           model=result.model, cost_cny=0.4, latency_ms=result.latency_ms,
                           meta=result.meta)

    monkeypatch.setattr(FakeProvider, "generate", flaky)
    task_id = _seed_task(tmp_path, monkeypatch, slots=2)

    with pytest.raises(_Crash):
        asyncio.run(execute_task(task_id))

    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        attempted = int(task.attempted_images or 0)
        cost = float(task.external_cost_cny or 0.0)
        calls = int(task.provider_calls or 0)
    assert attempted == 1, "第 1 张已生成，必须已经落库"
    assert cost == pytest.approx(0.4)
    assert calls >= 1


def test_watchdog_flushes_partial_cost_before_reclaiming(env, tmp_path, monkeypatch):
    """租约过期的 running 被接管时：先把已发生成本结算进使用量，再清零台账。"""
    import datetime as dt

    task_id = _seed_task(tmp_path, monkeypatch, slots=2)
    stale = dt.datetime.now() - dt.timedelta(hours=1)
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        username = task.username
        task_store.update_task(session, task_id, status="running", started_at=stale,
                               lease_until=stale, attempted_images=1,
                               external_cost_cny=0.4, provider_calls=1,
                               llm_cost_cny=0.02)
        before = usage_store.totals(session, username=username)

    monkeypatch.setattr("app.agents_image.graph.execute_task",
                        lambda task_id: asyncio.sleep(0))
    generator = LocalGenerator()

    async def run_once():
        await generator.resume_pending()
        # 接管后的任务由 no-op 执行体跑（不会改动台账），这里直接结束
        return None

    asyncio.run(run_once())
    with session_scope() as session:
        after = usage_store.totals(session, username=username)
        task = task_store.get_task(session, task_id)
        attempted_after = int(task.attempted_images or 0)
        cost_after = float(task.external_cost_cny or 0.0)
    assert after["images"] - before["images"] == 1, after
    assert after["cost_cny"] - before["cost_cny"] == pytest.approx(0.4)
    assert after["provider_calls"] - before["provider_calls"] == 1
    assert attempted_after == 0 and cost_after == 0.0, "台账清零，避免新一次执行重复记账"
