"""第七轮 P1-1：LLM/视觉解析的成本必须进台账（过去恒为 0）。

上一版 `metrics.record_llm` 只记 calls/tokens、从不传 cost，预算闸门只看生图成本，
管理端卡片的 `cost_sum` 也只有生图成本 → 开 `IMAGE_LLM_PARSE_MODE=always` 或视觉解析
之后，"钱花在哪条链路"完全不可见。现在：
- 解析用量按 token 单价估算成本（`app.llm.estimate_cost`，与 LLM 侧同一套单价）；
- 成本同时记进 `/metrics` 计数器、任务台账（`llm_cost_cny`）与使用量流水；
- 预算闸门按「生图 + LLM」总成本判定，超限告警里分列两项；
- 卡片/任务详情给出 `cost_cny`（生图）、`llm_cost_cny`、`cost_total_cny` 三列。
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
from app.agents_image.store import usage as usage_store
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
    monkeypatch.delenv("IMAGE_VISION_ENABLED", raising=False)
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


# ---------------------------------------------------------------- 单价与记账
def test_record_usage_estimates_cost_and_updates_state(env):
    from app import llm as real_llm

    state = ImageTaskState(task_id="t-cost")
    usage = {"calls": 1, "input_tokens": 400_000, "output_tokens": 100_000}
    expected = real_llm.estimate_cost(400_000, 100_000)
    assert expected > 0, "单价默认非 0（LLM_PRICE_INPUT/OUTPUT）"
    before = image_metrics.snapshot()
    llm_parse._record_usage(state, usage)
    after = image_metrics.snapshot()
    assert state.llm_calls == 1
    assert state.llm_tokens == 500_000
    assert state.llm_cost_cny == pytest.approx(expected)
    assert after["llm_cost_cny"] - before["llm_cost_cny"] == pytest.approx(expected)


def test_record_usage_accumulates_and_ignores_empty(env):
    state = ImageTaskState(task_id="t-cost2")
    llm_parse._record_usage(state, {"calls": 1, "input_tokens": 1_000_000, "output_tokens": 0})
    first = state.llm_cost_cny
    llm_parse._record_usage(state, {"calls": 1, "input_tokens": 0, "output_tokens": 1_000_000})
    assert state.llm_cost_cny > first
    llm_parse._record_usage(state, {})            # 空用量：不累加、不记指标
    assert state.llm_calls == 2


# ---------------------------------------------------------------- 端到端
class _StubChat:
    """模拟真实 client：成功一次（进上下文用量），全局 stats 也被写大（不得算进本次）。"""

    def __init__(self, raw: str):
        self.raw = raw
        self.stats = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    async def invoke_text(self, system, user, max_tokens=None, *, json_mode=False):
        from app import llm as real_llm

        real_llm.add_turn_usage(400_000, 100_000)
        self.stats["calls"] += 3
        self.stats["input_tokens"] += 9_999_999
        self.stats["output_tokens"] += 9_999_999
        return self.raw


def _seed_task(monkeypatch, *, attrs: dict, tmp_path=None) -> str:
    base = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp(prefix="img-cost-"))
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


def test_task_and_cards_carry_llm_cost(env, monkeypatch, tmp_path):
    """跑一个真实任务：任务台账 / 使用量流水 / 卡片 / 详情都要有 LLM 成本。"""
    from app import llm as real_llm
    from app.agents_image import graph

    monkeypatch.setenv("IMAGE_LLM_PARSE_ENABLED", "true")
    reset_image_settings()
    stub = _StubChat('{"color_hex": "#C8B7A6", "materials": "羊毛混纺"}')
    monkeypatch.setattr(real_llm, "get_llm", lambda: stub)
    task_id = _seed_task(monkeypatch, attrs={"product_type": "针织开衫（短款）",
                                             "color_hex": "", "shape": "落肩",
                                             "category": "女装"},
                         tmp_path=tmp_path)
    asyncio.run(graph.execute_task(task_id))

    expected = real_llm.estimate_cost(400_000, 100_000)
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        status = task.status
        llm_cost = float(task.llm_cost_cny or 0.0)
        provider_cost = float(task.external_cost_cny or 0.0)
        mine = usage_store.totals(session, username="admin")
    assert status == "done"
    assert llm_cost == pytest.approx(expected), "LLM 成本必须落到任务台账"
    assert provider_cost == 0.0, "fake provider 不计费，成本全部来自 LLM"
    assert mine["llm_cost_cny"] >= expected, mine

    cards = env.get("/api/image/admin/metrics?granularity=day&count=7&refresh=1",
                    headers=_login(env)).json()["cards"]
    assert cards["llm_cost_cny"] >= expected, cards
    assert cards["cost_total_cny"] == pytest.approx(cards["cost_cny"] + cards["llm_cost_cny"])

    detail = env.get(f"/api/image/admin/tasks/{task_id}", headers=_login(env)).json()
    assert detail["llm_cost_cny"] == pytest.approx(expected)
    assert detail["cost_total_cny"] == pytest.approx(expected)


def _login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}
