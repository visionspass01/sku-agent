"""第四轮 W5：取消与 provider 启动竞态（P2-11）。

场景：provider 已经产出（钱已花），任务在这期间被取消。
- 未结算 → 必须在这里补结算（成本/发起量不能漏记）；
- 已结算 → 不能再记一次，落 `IMG_CANCELED_COST_UNRECORDED` 留痕。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents_image import graph
from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.nodes import plan_slots
from app.agents_image.png import placeholder_png
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.state import ImageTaskState, SlotPlan
from app.agents_image.store import assets as asset_store
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


def _make_task(task_id: str, username: str) -> None:
    state = ImageTaskState(prompt="白底主图", aspect="1:1", count=1, refs=[])
    slots = [SlotPlan(**item) if isinstance(item, dict) else item
             for item in plan_slots.build_slots(state,
                                                policy=asyncio.run(ps.get_catalog().get_plan_policy()))]
    with session_scope() as session:
        ref = asset_store.save_bytes(session, placeholder_png("ref", 64, 64), kind="ref",
                                     product_id="P-3001")
        task_store.create_task(
            session, task_id=task_id, username=username, department=None,
            prompt="白底主图", platform="淘系", aspect="1:1", n=1,
            ref_asset_ids=[ref.asset_id], product_id="P-3001",
            slots=[item.model_dump() for item in slots], image_count=1,
        )
        asset_store.bind_refs_to_task(session, [ref.asset_id], task_id)


def _totals(username: str) -> dict:
    with session_scope() as session:
        return usage_store.totals(session, username=username)


def _cancel_mid_flight(monkeypatch, task_id: str, *, settled_first: bool) -> None:
    """provider 返回前把任务改成 canceled（模拟"已产生成本但任务先被取消"）。"""
    from app.agents_image.providers.fake import FakeProvider

    original = FakeProvider.generate
    state = {"calls": 0, "done": False}

    async def racing(self, req):
        state["calls"] += 1
        result = await original(self, req)
        if not state["done"]:
            state["done"] = True
            with session_scope() as session:
                if settled_first:
                    task_store.mark_settled(session, task_id)
                task_store.update_task(session, task_id, status="canceled",
                                       error_code="IMG_TASK_CANCELED")
        return result

    monkeypatch.setattr(FakeProvider, "generate", racing)
    return state


def test_cancel_race_records_cost_once(env, monkeypatch):
    user = "u-cancel-race"
    task_id = "20261118000000cc21"
    _make_task(task_id, user)
    state = _cancel_mid_flight(monkeypatch, task_id, settled_first=False)

    asyncio.run(graph.execute_task(task_id))

    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        assert task.status == "canceled"
        assert task.settled_at is not None, "取消竞态必须补结算，否则成本/额度漏记"
        calls = int(task.provider_calls or 0)
        warnings = [str(item) for item in (task.warnings_json or [])]
    assert state["calls"] >= 1
    assert calls >= 1, "取消也要记发起量"
    assert not any("IMG_CANCELED_COST_UNRECORDED" in item for item in warnings)

    totals = _totals(user)
    assert totals["provider_calls"] >= 1
    assert totals["failed"] == 1

    # 再执行一次：已结算，不重复记账
    before = _totals(user)
    asyncio.run(graph.execute_task(task_id))
    assert _totals(user) == before


def test_cancel_race_after_settlement_leaves_warning(env, monkeypatch):
    user = "u-cancel-race-settled"
    task_id = "20261118000000cc22"
    _make_task(task_id, user)
    _cancel_mid_flight(monkeypatch, task_id, settled_first=True)

    asyncio.run(graph.execute_task(task_id))

    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        warnings = [str(item) for item in (task.warnings_json or [])]
    assert any("IMG_CANCELED_COST_UNRECORDED" in item for item in warnings), warnings
    # 已结算过就不能再记一次使用量/发起量
    assert _totals(user)["provider_calls"] == 0
