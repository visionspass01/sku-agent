"""第九轮 P1-4：像素门禁必须在事件循环之外执行。

`stats_of` 是纯 Python 逐像素循环（1024² 实测约 1 秒/张），过去在 `execute_task` 协程里
同步跑：gate=all 或有 autofix 时单任务可卡住事件循环 6–18 秒，SSE 心跳、取消检查、
看门狗与其它任务全部被拖后。现在门禁走 `asyncio.to_thread`。
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from app.agents_image import graph as graph_module
from app.agents_image.graph import execute_task
from app.agents_image.ids import new_task_id
from app.agents_image.nodes import plan_slots
from app.agents_image.png import placeholder_png
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
    from app.agents_image.config import reset_image_settings

    reset_image_settings()
    ensure_image_schema()
    yield
    reset_image_settings()


def _seed_task(tmp_path, monkeypatch) -> str:
    from app.agents_image.config import reset_image_settings

    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    reset_image_settings()
    ensure_image_schema()
    user = f"u-gate-{uuid.uuid4().hex[:8]}"
    with session_scope() as session:
        ref = asset_store.save_bytes(session, placeholder_png("gate-ref", 64, 64),
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


def test_pixel_gate_runs_off_the_event_loop(env, tmp_path, monkeypatch):
    """门禁执行期间，事件循环必须还能调度别的协程（旧实现会整段阻塞）。"""
    real_gate = graph_module.compliance.gate

    def slow_gate(*args, **kwargs):
        time.sleep(0.2)                       # 模拟大图的纯 Python 像素统计
        return real_gate(*args, **kwargs)

    monkeypatch.setattr(graph_module.compliance, "gate", slow_gate)
    task_id = _seed_task(tmp_path, monkeypatch)

    async def scenario() -> int:
        ticks = {"n": 0}

        async def ticker():
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        try:
            await execute_task(task_id)
        finally:
            task.cancel()
        return ticks["n"]

    ticks = asyncio.run(scenario())
    assert ticks > 3, f"门禁阻塞了事件循环（ticker 只跑了 {ticks} 次）"
