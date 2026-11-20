"""第六轮 P1-2 + P1-3：LLM/视觉解析真正可用的两条修复。

- P1-2：视觉解析过去取 `ref.key` 读字节，但 `state.refs` 是 `RefImage`（没有 key）
  → 即便配了 Key 也 100% 降级；现在按 `asset_id → ImageAsset.key` 读，并有单测证明 payload 里
  真的带上了 base64 图片；
- P1-3：解析触发从"attrs 为空才解析"改成 `IMAGE_LLM_PARSE_MODE=off|fallback|always`，
  默认 fallback（中台字段不全才解析），且合并时**中台字段优先、模型只补空位**。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import uuid
from pathlib import Path

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import get_image_settings, reset_image_settings
from app.agents_image.nodes import llm_parse, plan_slots
from app.agents_image.png import placeholder_png
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.state import ImageTaskState, RefImage, SlotPlan
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
    monkeypatch.delenv("IMAGE_LLM_PARSE_ENABLED", raising=False)
    monkeypatch.delenv("IMAGE_VISION_ENABLED", raising=False)
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


# ---------------------------------------------------------------- P1-3：触发条件
def test_attrs_incomplete_detects_missing_fields():
    assert llm_parse.attrs_incomplete({}) is True
    assert llm_parse.attrs_incomplete({"product_type": "针织开衫（短款）",
                                       "color_hex": ""}) is True
    assert llm_parse.attrs_incomplete({"product_type": "针织开衫（短款）",
                                       "color_hex": "#C8B7A6",
                                       "shape": "落肩", "materials": "羊毛混纺"}) is False


def test_merge_missing_attrs_keeps_platform_fields():
    """中台字段是权威：模型只能补空位，不能覆盖。"""
    base = {"product_type": "针织开衫（短款）", "color_hex": "#C8B7A6",
            "shape": "", "must_not_change": ["颜色", "版型"]}
    parsed = {"product_type": "模型乱改的品类", "color_hex": "#000000",
              "shape": "落肩、V 领", "materials": "羊毛混纺",
              "must_not_change": ["版型", "刺绣文字"], "source": "llm"}
    merged = llm_parse.merge_missing_attrs(base, parsed)
    assert merged["product_type"] == "针织开衫（短款）"     # 中台优先
    assert merged["color_hex"] == "#C8B7A6"
    assert merged["shape"] == "落肩、V 领"                  # 空位被补
    assert merged["materials"] == "羊毛混纺"
    assert merged["must_not_change"] == ["颜色", "版型", "刺绣文字"]
    assert merged["source"] == "llm"


def test_parse_mode_off_skips_model_call(env, monkeypatch):
    monkeypatch.setenv("IMAGE_LLM_PARSE_MODE", "off")
    monkeypatch.setenv("IMAGE_LLM_PARSE_ENABLED", "true")
    reset_image_settings()
    calls = {"n": 0}

    async def boom(*args, **kwargs):
        calls["n"] += 1
        return "{}"

    # 用桩替换整个解析入口：off 模式下不该被调用
    monkeypatch.setattr(llm_parse, "parse_clothing_attrs", boom)
    task_id = _seed_task(monkeypatch=monkeypatch, attrs={})
    from app.agents_image import graph

    asyncio.run(graph.execute_task(task_id))
    assert calls["n"] == 0, "off 模式不得调用模型解析"
    with session_scope() as session:
        status = task_store.get_task(session, task_id).status
    assert status == "done"


def test_fallback_mode_fills_missing_attrs_from_llm(env, monkeypatch, tmp_path):
    """中台 attrs 缺 color_hex → fallback 模式触发解析，且只补空位。"""
    monkeypatch.setenv("IMAGE_LLM_PARSE_ENABLED", "true")
    reset_image_settings()
    stub = _StubLLM('{"color_hex": "#C8B7A6", "materials": "羊毛混纺 30%", '
                    '"product_type": "模型猜的品类"}')
    monkeypatch.setitem(sys.modules, "app.llm", types.SimpleNamespace(get_llm=lambda: stub))
    task_id = _seed_task(monkeypatch=monkeypatch,
                         attrs={"product_type": "针织开衫（短款）", "color_hex": "",
                                "shape": "落肩", "category": "女装",
                                "must_not_change": ["颜色", "版型"]},
                         tmp_path=tmp_path)
    from app.agents_image import graph

    asyncio.run(graph.execute_task(task_id))
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        status, attrs_json = task.status, dict(task.attrs_json or {})
    assert status == "done"
    assert attrs_json["color_hex"] == "#C8B7A6", attrs_json
    assert attrs_json["materials"] == "羊毛混纺 30%"
    assert attrs_json["product_type"] == "针织开衫（短款）", "中台字段不能被模型覆盖"


# ---------------------------------------------------------------- P1-2：视觉读字节
class _FakeResponse:
    def __init__(self, raw: str):
        self._raw = raw

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._raw}}]}


class _FakeAsyncClient:
    captured: dict = {}

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):   # noqa: A002
        type(self).captured["url"] = url
        type(self).captured["json"] = json
        type(self).captured["headers"] = headers
        return _FakeResponse('{"product_type": "针织开衫（短款）", "color_hex": "#C8B7A6", '
                             '"must_not_change": ["颜色", "版型"]}')


def test_vision_reads_reference_bytes_via_asset_key(env, monkeypatch):
    """P1-2：refs 是 RefImage（无 key），必须按 asset_id→asset.key 读到字节并放进 payload。"""
    monkeypatch.setenv("IMAGE_VISION_ENABLED", "true")
    monkeypatch.setenv("IMAGE_ARK_API_KEY", "ark-test-key")
    monkeypatch.setenv("IMAGE_ARK_VISION_MODEL", "ark-vision-test")
    reset_image_settings()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.captured = {}

    with session_scope() as session:
        ref = asset_store.save_bytes(session, placeholder_png("vision-ref", 64, 64),
                                     kind="ref", product_id="P-3001", username="admin")
        ref_id, ref_sha, ref_mime = ref.asset_id, ref.sha256, ref.mime

    state = ImageTaskState(task_id="t-vision", prompt="白底主图", aspect="1:1", count=1,
                           platform="淘系",
                           refs=[RefImage(asset_id=ref_id, sha256=ref_sha, mime=ref_mime)])

    attrs, warnings = asyncio.run(
        llm_parse.parse_clothing_attrs(state, get_image_settings(),
                                       base_attrs={"product_type": "针织开衫（短款）"}))
    assert warnings == [], warnings
    assert attrs is not None and attrs["color_hex"] == "#C8B7A6"
    assert attrs["source"] == "vision"

    payload = _FakeAsyncClient.captured.get("json") or {}
    user_content = payload["messages"][1]["content"]
    images = [item for item in user_content if item.get("type") == "image_url"]
    assert images, "视觉请求必须带参考图（过去这里永远为空）"
    assert images[0]["image_url"]["url"].startswith("data:image/")


def test_vision_degrades_when_bytes_missing(env, monkeypatch):
    monkeypatch.setenv("IMAGE_VISION_ENABLED", "true")
    monkeypatch.setenv("IMAGE_ARK_API_KEY", "ark-test-key")
    monkeypatch.setenv("IMAGE_ARK_VISION_MODEL", "ark-vision-test")
    reset_image_settings()
    state = ImageTaskState(task_id="t-vision2", prompt="白底主图", aspect="1:1", count=1,
                           refs=[RefImage(asset_id="missing-asset")])
    attrs, warnings = asyncio.run(llm_parse.parse_clothing_attrs(state, get_image_settings()))
    assert attrs is None
    assert any("IMG_VISION_DEGRADED" in item for item in warnings), warnings


class _StubLLM:
    def __init__(self, raw: str):
        self.raw = raw
        self.stats = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    async def invoke_text(self, system, user, max_tokens=None, *, json_mode=False):
        assert json_mode is True
        self.stats["calls"] += 1
        self.stats["input_tokens"] += 50
        self.stats["output_tokens"] += 20
        return self.raw


def _seed_task(monkeypatch, *, attrs: dict, tmp_path=None) -> str:
    base = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp(prefix="img-llm-"))
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
