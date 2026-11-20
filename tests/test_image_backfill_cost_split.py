"""第八轮 P1-3：回写中台的成本字段必须分列（生图 / LLM / 合计）。

上一版把「生图 + LLM」总成本塞进 `params.cost_cny` 与单图 `cost_cny`，而 Agent 接口/任务
台账里的 `cost_cny` 只含生图成本 —— 同一个字段名两种口径，中台按单图成本对账时必然对不上。
现在：`cost_cny` = 生图（第三方真实计费），`llm_cost_cny` = 解析估算，`cost_total_cny` = 两者相加。
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from pathlib import Path

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.nodes import llm_parse, plan_slots
from app.agents_image.png import placeholder_png
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.state import ImageTaskState, SlotPlan
from app.agents_image.store import assets as asset_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    monkeypatch.delenv("IMAGE_PRODUCT_SOURCE", raising=False)
    monkeypatch.delenv("IMAGE_LLM_PARSE_MODE", raising=False)
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


class _StubChat:
    def __init__(self, raw: str):
        self.raw = raw

    async def invoke_text(self, system, user, max_tokens=None, *, json_mode=False):
        from app import llm as real_llm

        real_llm.add_turn_usage(400_000, 100_000)
        return self.raw


def _seed_task(monkeypatch, *, attrs: dict, tmp_path=None) -> str:
    base = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp(prefix="img-cost2-"))
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(base / "images"))
    reset_image_settings()
    ensure_image_schema()
    state = ImageTaskState(prompt="白底主图", aspect="1:1", count=1, refs=[])
    slots = [SlotPlan(**item) if isinstance(item, dict) else item
             for item in plan_slots.build_slots(
                 state, policy=asyncio.run(ps.get_catalog().get_plan_policy()))]
    with session_scope() as session:
        ref = asset_store.save_bytes(session, placeholder_png("r", 64, 64), kind="ref",
                                     product_id="P-3001", username="admin")
        task_id = f"2026113{uuid.uuid4().hex[:9]}"
        task_store.create_task(
            session, task_id=task_id, username="admin", department="女装",
            prompt="白底主图", platform="淘系", aspect="1:1", n=1,
            ref_asset_ids=[ref.asset_id], product_id="P-3001",
            attrs=dict(attrs), slots=[item.model_dump() for item in slots], image_count=1,
            slot_count=len(slots),
        )
        asset_store.bind_refs_to_task(session, [ref.asset_id], task_id)
    return task_id


def test_backfill_payload_splits_provider_and_llm_cost(env, monkeypatch, tmp_path):
    from app import llm as real_llm
    from app.agents_image import graph
    from app.agents_image.sources.fake_catalog import FakeCatalog

    monkeypatch.setenv("IMAGE_LLM_PARSE_ENABLED", "true")
    reset_image_settings()
    stub = _StubChat('{"color_hex": "#C8B7A6", "materials": "羊毛混纺"}')
    monkeypatch.setattr(real_llm, "get_llm", lambda: stub)

    captured: dict = {}
    real_save = FakeCatalog.save_result

    async def spy(self, payload):
        captured.update(payload)
        return await real_save(self, payload)

    monkeypatch.setattr(FakeCatalog, "save_result", spy)
    task_id = _seed_task(monkeypatch, attrs={"product_type": "针织开衫（短款）",
                                             "color_hex": "", "shape": "落肩",
                                             "category": "女装"},
                         tmp_path=tmp_path)
    asyncio.run(graph.execute_task(task_id))

    expected = real_llm.estimate_cost(400_000, 100_000)
    assert captured, "回写 payload 必须被调用"
    params = captured["params"]
    assert params["cost_cny"] == 0.0, "生图成本（fake provider 免费）"
    assert params["llm_cost_cny"] == pytest.approx(expected)
    assert params["cost_total_cny"] == pytest.approx(expected)
    first = captured["results"][0]
    assert first["cost_cny"] == 0.0
    assert first["cost_total_cny"] == pytest.approx(expected)


def test_llm_parse_still_reports_usage(env):
    """顺带确认 trim 之后 llm_parse 的记账入口没被删坏。"""
    assert hasattr(llm_parse, "_record_usage")
