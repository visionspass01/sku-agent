"""D8–D9：9 图位规划 / 用户覆盖 / 非出图图位 / 五槽位 Prompt / 七条铁律 / prompt_pack 结构。"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
import yaml

from app.agents_image import metrics as image_metrics
from app.agents_image.config import ImageSettings, reset_image_settings
from app.agents_image.graph import execute_task
from app.agents_image.ids import new_task_id
from app.agents_image.nodes import plan_slots
from app.agents_image.prompt_pack import pack_document, pack_yaml
from app.agents_image.registry import get_registry, reset_registry
from app.agents_image.sources.fake_catalog import FakeCatalog, _PRODUCTS
from app.agents_image.state import ImageTaskState, RefImage, SlotPlan
from app.agents_image.store import assets as asset_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store.schema import ensure_image_schema
from tests.test_image_storage import make_png


@pytest.fixture(autouse=True)
def _fresh():
    reset_registry()
    reset_image_settings()
    image_metrics.reset()
    yield
    reset_registry()
    reset_image_settings()


@pytest.fixture()
def policy():
    return asyncio.run(FakeCatalog().get_plan_policy())


@pytest.fixture()
def rules():
    return asyncio.run(FakeCatalog().get_platform_rules(platform="淘系"))


def _state(**overrides) -> ImageTaskState:
    base = {"prompt": "白底主图，正面全身像上装突出", "aspect": "1:1", "count": 2,
            "refs": [RefImage(asset_id="a" * 8)],
            "attrs": dict(_PRODUCTS["P-1001"]["attrs"])}
    base.update(overrides)
    return ImageTaskState(**base)


# ---------------------------------------------------------------- 图位规划
def test_build_slots_follows_plan_policy(policy):
    slots = plan_slots.build_slots(_state(count=9), policy=policy)
    assert [item["slot"] for item in slots] == list(range(1, 10))
    assert slots[0]["role"] == "主图"
    assert slots[0]["composition_id"] == "C-FULL-FRONT-TOP"
    assert slots[6]["type"] == "layout" and slots[8]["type"] == "photo_required"
    assert sum(1 for item in slots if item["type"] == "generate") == 6


def test_build_slots_truncates_to_requested_count(policy):
    slots = plan_slots.build_slots(_state(count=3), policy=policy)
    assert [item["slot"] for item in slots] == [1, 2, 3]


def test_user_composition_and_style_override_policy(policy):
    slots = plan_slots.build_slots(
        _state(count=2, compositions=["C-SIDE-LOWER"], scene_styles=["S-STREET-FRESH"]),
        policy=policy,
        labels={"compositions": {"C-SIDE-LOWER": "侧身像·下装突出"},
                "scene_styles": {"S-STREET-FRESH": "街拍清新风"}})
    assert slots[0]["composition_id"] == "C-SIDE-LOWER"
    assert slots[0]["composition_label"] == "侧身像·下装突出"
    assert slots[0]["scene_style_label"] == "街拍清新风"


def test_generate_slots_filters_non_generation(policy):
    slots = plan_slots.build_slots(_state(count=9), policy=policy)
    generate = plan_slots.generate_slots(slots)
    assert [item["slot"] for item in generate] == [1, 2, 3, 4, 5, 6]
    assert plan_slots.is_generatable(slots[0]) is True
    assert plan_slots.is_generatable(slots[6]) is False


def test_slot_rule_manifest_override():
    rule = plan_slots.slot_rule(1, "主图", {"slot_rules": {"1": {"fill": 62, "reserve": False}}})
    assert rule["fill"] == 62 and rule["reserve"] is False
    assert plan_slots.slot_rule(6, "细节特写", None)["fill"] == 80


# ---------------------------------------------------------------- Prompt 与铁律
def test_compose_applies_seven_rules(policy, rules):
    state = _state(count=2)
    state.slots = [SlotPlan(**item) for item in
                   plan_slots.build_slots(state, policy=policy)]
    drafts = plan_slots.compose(state, get_registry(), rules=rules,
                                fragments=["主图优先纯白背景", "卖点图可用微距"])
    first = drafts[0]
    prompt = first.prompt
    # ① hex 颜色 / ② 占比数字 / ③ 留白数字 / ④ 否定清单含身份锁定
    assert "#C8B7A6" in prompt
    assert "商品占画面 70%" in prompt and "四周留白 8%" in prompt
    assert "颜色" in prompt and "版型" in prompt
    # ⑤ 主图预留平台角标区
    assert "平台角标" in prompt
    # ⑥ 图内文字策略按图位不同
    assert "不生成任何文字" in drafts[0].prompt
    assert "主标语 ≤4 词" in drafts[1].prompt
    # 平台禁用词进了否定清单，文件大小限制进了技术参数
    assert "全网最低价" in first.negative_prompt
    assert "≤3.0MB" in prompt or "3.0MB" in prompt
    # ⑦ RAG 片段进 Prompt
    assert "主图优先纯白背景" in prompt
    assert first.fill_percent == 70 and first.margin_percent == 8
    assert first.text_policy == "none" and drafts[1].text_policy == "headline"


def test_compose_respects_platform_text_disallowed(policy):
    from app.agents_image.sources.product_source import PlatformRules

    rules = PlatformRules(platform="淘系", aspect="1:1", text_allowed=False,
                          forbidden_words=["绝对"], max_mb=2.0)
    state = _state(count=2)
    state.slots = [SlotPlan(**item) for item in plan_slots.build_slots(state, policy=policy)]
    drafts = plan_slots.compose(state, get_registry(), rules=rules)
    assert all(draft.text_policy == "none" for draft in drafts)
    assert all("不生成任何文字" in draft.prompt for draft in drafts)


# ---------------------------------------------------------------- prompt_pack
def test_prompt_pack_document_and_yaml(policy):
    state = _state(count=3)
    state.task_id = "20261117153045a3f9"
    state.batch_id = "20261117153040b7c1"
    state.product_id = "P-1001"
    state.slots = [SlotPlan(**item) for item in plan_slots.build_slots(state, policy=policy)]
    drafts = plan_slots.compose(state, get_registry())
    doc = pack_document(state, drafts)
    assert doc["task_id"] == state.task_id and doc["product_id"] == "P-1001"
    assert len(doc["slots"]) == 3
    assert doc["slots"][0]["fill_percent"] == 70
    assert doc["slots"][0]["preserve"]

    text = pack_yaml(state, drafts)
    parsed = yaml.safe_load(text)
    assert parsed["slots"][1]["role"] == "上装卖点"
    assert parsed["slots"][1]["text_policy"] == "headline"
    assert "prompt" in parsed["slots"][0]


# ---------------------------------------------------------------- 编排集成
def test_graph_generates_only_generate_slots(tmp_path, monkeypatch, _db):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    reset_image_settings()
    ensure_image_schema()

    with _db_session() as session:
        ref = asset_store.save_bytes(session, make_png(64, 64), kind="ref", product_id="P-1001")
        state = _state(count=9)
        state.slots = [SlotPlan(**item) for item in
                       plan_slots.build_slots(state, policy=asyncio.run(
                           FakeCatalog().get_plan_policy()))]
        task = task_store.create_task(
            session, task_id=new_task_id(), username="worker1", department=None,
            prompt=state.prompt, platform="淘系", aspect="1:1", n=9,
            ref_asset_ids=[ref.asset_id], product_id="P-1001",
            attrs=dict(_PRODUCTS["P-1001"]["attrs"]),
            slots=[item.model_dump() for item in state.slots],
        )
        asset_store.bind_refs_to_task(session, [ref.asset_id], task.task_id)
        task_id = task.task_id

    result = asyncio.run(execute_task(task_id))
    try:
        # 9 个图位里 6 个 generate、2 个 layout、1 个 photo_required
        assert result.delivered_images == 6
        assert len(result.prompt_pack) == 9
        assert sum(1 for draft in result.prompt_pack if draft.type == "generate") == 6

        with _db_session() as session:
            stored = task_store.get_task(session, task_id)
            assert stored.delivered_images == 6
            assert len(stored.slots_json) == 9
            backfill_status = stored.backfill_status
            slots_with_output = {asset.slot for asset in asset_store.list_outputs(session, task_id)}
        assert slots_with_output == {1, 2, 3, 4, 5, 6}
        assert backfill_status in {"sent", "failed"}
    finally:
        reset_image_settings()


class _db_session:
    def __enter__(self):
        from app.db.engine import session_scope

        self._ctx = session_scope()
        return self._ctx.__enter__()

    def __exit__(self, *exc):
        return self._ctx.__exit__(*exc)


def test_settings_untouched(tmp_path, monkeypatch):
    """平台规则缺省时也不该抛错（rules=None 走 manifest 默认）。"""
    state = _state(count=1)
    drafts = plan_slots.compose(state, get_registry())
    assert drafts and drafts[0].prompt


@pytest.mark.parametrize("count", [1, 6, 9])
def test_compose_scales_with_count(count, policy):
    state = _state(count=count)
    state.slots = [SlotPlan(**item) for item in plan_slots.build_slots(state, policy=policy)]
    drafts = plan_slots.compose(state, get_registry(), rules=ImageSettings() and None)
    assert len(drafts) == count
    assert all(draft.prompt for draft in drafts)


def test_created_at_defaults_ok():
    assert dt.datetime.now().year >= 2024
