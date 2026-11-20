"""第六轮 P1-7：阶段耗时直方图必须记「上一阶段」的耗时（不能整体错位一格）。

上一版在 `touch(stage)` 时把「上次 touch 到现在的耗时」打进**本阶段**标签：
sequence understand → generate → critic 时，generate 的真实耗时被记在 critic 名下，
`/metrics` 的 image_stage_seconds 用它排查"谁慢"会得出相反结论。
"""

from __future__ import annotations

import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.state import ImageTaskState


@pytest.fixture(autouse=True)
def _reset_metrics():
    image_metrics.reset()
    yield
    image_metrics.reset()


def test_histogram_labels_previous_stage():
    state = ImageTaskState(task_id="t-stage", prompt="白底主图", aspect="1:1", count=1)
    state.touch("understand")
    time.sleep(0.02)
    state.touch("generate")
    time.sleep(0.02)
    state.touch("critic")

    stages = image_metrics.stage_snapshot()
    assert stages["understand"]["count"] == 1, stages
    assert stages["generate"]["count"] == 1, stages
    # critic 刚"开始"，还没结束 → 不该有耗时样本（错位版本这里会有）
    assert "critic" not in stages or stages["critic"]["count"] == 0, stages
    assert stages["understand"]["sum"] >= 0  # 值本身在毫秒桶里


def test_timeline_marks_previous_stage():
    state = ImageTaskState(task_id="t-stage2", prompt="白底主图", aspect="1:1", count=1)
    state.touch("understand")
    state.touch("generate", note="x")
    first, second = state.timeline[0], state.timeline[1]
    assert first["stage"] == "understand" and first["ms"] == 0
    assert second["stage"] == "generate"
    assert second["prev_stage"] == "understand"
    assert second["ms"] >= 0


def test_task_run_records_generate_under_generate(_db, monkeypatch, tmp_path):
    """端到端：跑一个任务，generate 阶段必须有耗时样本（之前会被记到 critic 名下）。"""
    import asyncio
    import uuid

    from app.agents_image import graph
    from app.agents_image.nodes import plan_slots
    from app.agents_image.png import placeholder_png
    from app.agents_image.sources import product_source as ps
    from app.agents_image.state import SlotPlan
    from app.agents_image.store import assets as asset_store
    from app.agents_image.store import tasks as task_store
    from app.agents_image.store.schema import ensure_image_schema
    from app.db.engine import session_scope

    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    ensure_image_schema()
    state = ImageTaskState(prompt="白底主图", aspect="1:1", count=1)
    slots = [SlotPlan(**item) if isinstance(item, dict) else item
             for item in plan_slots.build_slots(
                 state, policy=asyncio.run(ps.get_catalog().get_plan_policy()))]
    with session_scope() as session:
        ref = asset_store.save_bytes(session, placeholder_png("r", 64, 64), kind="ref",
                                     product_id="P-3001", username="admin")
        task_id = f"2026115{uuid.uuid4().hex[:9]}"
        task_store.create_task(
            session, task_id=task_id, username="admin", department="女装",
            prompt="白底主图", platform="淘系", aspect="1:1", n=1,
            ref_asset_ids=[ref.asset_id], product_id="P-3001",
            attrs={"product_type": "半身裙（长款）", "color_hex": "#111111",
                   "category": "女装"},
            slots=[item.model_dump() for item in slots], image_count=1,
            slot_count=len(slots),
        )
        asset_store.bind_refs_to_task(session, [ref.asset_id], task_id)

    asyncio.run(graph.execute_task(task_id))
    stages = image_metrics.stage_snapshot()
    assert stages["generate"]["count"] >= 1, stages
    assert stages["critic"]["count"] >= 1, stages
    assert stages["understand"]["count"] >= 1, stages
