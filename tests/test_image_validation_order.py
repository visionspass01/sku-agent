"""第九轮 P1-1：Prompt 最终校验必须在**发给模型之前**，且失败时不能丢已发生的成本。

上一版把 `validate.validate` 放在生成循环之后：校验失败时图已生成、钱已花，
而异常不带失败统计 → dispatcher 记 attempted=0 / 成本 0，图和账一起蒸发。
现在：每轮生成前校验；万一校验失败（例如上一轮已出过图），把已发生成本挂到异常上。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.agents_image.dispatch.local_generator import LocalGenerator
from app.agents_image.graph import execute_task
from app.agents_image.ids import new_task_id
from app.agents_image.nodes import plan_slots
from app.agents_image.png import placeholder_png
from app.agents_image.providers.image_provider import ImageResult
from app.agents_image.sources.fake_catalog import _PRODUCTS, FakeCatalog
from app.agents_image.state import ImageTaskState, SlotPlan
from app.agents_image.store import assets as asset_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    from app.agents_image.auth import reset_throttle
    from app.agents_image.config import reset_image_settings

    reset_image_settings()
    reset_throttle()
    ensure_image_schema()
    yield tmp_path
    reset_image_settings()
    reset_throttle()


def _seed_task(tmp_path, monkeypatch, *, username: str | None = None) -> str:
    from app.agents_image.config import reset_image_settings

    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    reset_image_settings()
    ensure_image_schema()
    user = username or f"u-valid-{uuid.uuid4().hex[:8]}"
    with session_scope() as session:
        ref = asset_store.save_bytes(session, placeholder_png("valid-ref", 64, 64),
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


def _run_until_terminal(task_id: str, generator: LocalGenerator) -> str | None:
    async def _run():
        await generator.submit(task_id)
        for _ in range(400):
            with session_scope() as session:
                task = task_store.get_task(session, task_id)
                if task is not None and task.status in {"done", "failed", "canceled"}:
                    return task.status
            await asyncio.sleep(0.05)
        return None

    return asyncio.run(_run())


def test_validation_runs_before_generation(env, monkeypatch):
    """首轮校验失败：**一次 provider 都不能调**（旧实现会先出图再校验）。"""
    from app.agents_image.providers.fake import FakeProvider

    calls = {"n": 0}

    async def spy(self, req):
        calls["n"] += 1
        return ImageResult(images=[placeholder_png("x", 32, 32)], mime="image/png",
                           provider="fake", model="m", cost_cny=0.0, latency_ms=1)

    monkeypatch.setattr(FakeProvider, "generate", spy)
    real_compose = plan_slots.compose

    def compose_without_lock_words(state, registry, **kwargs):
        drafts = real_compose(state, registry, **kwargs)
        for draft in drafts:
            draft.prompt = "白底主图"          # 正文丢掉锁定词 → 校验必失败
        return drafts

    monkeypatch.setattr(plan_slots, "compose", compose_without_lock_words)
    task_id = _seed_task(env, monkeypatch)

    with pytest.raises(Exception) as caught:
        asyncio.run(execute_task(task_id))
    assert "IMG_SCHEMA_INVALID" in str(caught.value) or "身份锁定" in str(caught.value)
    assert calls["n"] == 0, "校验失败时不得调用 provider"

    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        assert int(task.attempted_images or 0) == 0
        assert int(task.provider_calls or 0) == 0


def test_validation_failure_after_generation_keeps_attempted_and_cost(env, monkeypatch):
    """第二轮（Reflexion 之后）校验失败：上一轮已生成的张数与发起量必须记账。

    第十二轮：provider 换成**函数级注入**（`handler.get_provider` 直接返回一个实例），
    不再改 `FakeProvider` 的类方法——类方法补丁在整套用例里会被别的用例交叉影响，
    表现为"第一轮成了 done"的偶发失败。
    """
    from app.agents_image.generator import handler as generator_handler
    from app.agents_image.providers.fake import FakeProvider

    state = {"calls": 0}

    class _AspectProvider:
        name = "fake"
        caps = FakeProvider.caps

        async def generate(self, req):  # noqa: ANN001
            state["calls"] += 1
            if state["calls"] == 1:
                bad = placeholder_png("bad", 640, 360)      # 16:9，与 1:1 不符 → Critic 失败
                return ImageResult(images=[bad], mime="image/png", provider="fake",
                                   model="bad-aspect", cost_cny=0.4, latency_ms=1)
            return await FakeProvider().generate(req)

        async def health(self) -> dict:
            return {"ok": True, "provider": "fake"}

    monkeypatch.setattr(generator_handler, "get_provider",
                        lambda name=None: _AspectProvider())

    from app.agents_image import graph as graph_mod

    real_apply = graph_mod.reflexion.apply_patches

    def apply_without_lock_words(drafts, reflection):
        patched = real_apply(drafts, reflection)
        for draft in patched:
            draft.prompt = "白底主图"
        return patched

    monkeypatch.setattr(graph_mod.reflexion, "apply_patches", apply_without_lock_words)

    task_id = _seed_task(env, monkeypatch)
    with session_scope() as session:
        username = task_store.get_task(session, task_id).username

    assert _run_until_terminal(task_id, LocalGenerator()) == "failed"
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        assert task.error_code in {"IMG_SCHEMA_INVALID", "IMG_INTERNAL"}, task.error_code
        assert int(task.attempted_images or 0) == 1, "上一轮已生成的 1 张必须记账"
        assert int(task.provider_calls or 0) == 1
        assert float(task.external_cost_cny or 0.0) == pytest.approx(0.4)
        from app.agents_image.store import usage as usage_store

        mine = usage_store.totals(session, username=username)
    assert mine["images"] == 1, mine
    assert mine["cost_cny"] == pytest.approx(0.4), mine
