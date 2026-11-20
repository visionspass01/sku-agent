"""第五轮 W7：观测导出 + LLM 解析接线（P1-7 / 简历 4.5、4.4）。

- `/api/image/metrics` 输出 Prometheus text format（零依赖渲染器，装了官方库可自动切换）；
- 可选 `IMAGE_METRICS_TOKEN` 做 Bearer 鉴权（默认公开只读数值）；
- `nodes/llm_parse.py`：模型输出一律交给 `parse_attrs` 收口；开关默认关，无 Key 自动降级；
- LLM 用量进 counters，五张卡的 llm_calls/llm_tokens 不再是常量 0。
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import get_image_settings, reset_image_settings
from app.agents_image.nodes import llm_parse
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.state import ImageTaskState
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    monkeypatch.delenv("IMAGE_PRODUCT_SOURCE", raising=False)
    monkeypatch.delenv("IMAGE_METRICS_TOKEN", raising=False)
    monkeypatch.delenv("IMAGE_LLM_PARSE_ENABLED", raising=False)
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


def login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _state() -> ImageTaskState:
    return ImageTaskState(task_id="t-parse", prompt="白底主图 米色针织开衫",
                          aspect="1:1", count=1, platform="淘系")


class _StubLLM:
    def __init__(self, raw: str | None = None, *, boom: bool = False):
        self.raw = raw
        self.boom = boom
        self.stats = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    async def invoke_text(self, system, user, max_tokens=None, *, json_mode=False):
        assert json_mode is True, "解析必须走 JSON mode"
        self.stats["calls"] += 1
        self.stats["input_tokens"] += 100
        self.stats["output_tokens"] += 40
        if self.boom:
            raise RuntimeError("no key")
        return self.raw or "{}"


def _install_stub(monkeypatch, stub: _StubLLM) -> None:
    monkeypatch.setitem(sys.modules, "app.llm", types.SimpleNamespace(get_llm=lambda: stub))


# ---------------------------------------------------------------- /metrics（Prometheus text）
def test_metrics_endpoint_returns_prometheus_text(env):
    image_metrics.record_task_created(2)
    image_metrics.record_stage("generate", 120.0)
    resp = env.get("/api/image/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert "# TYPE image_tasks_total counter" in body
    assert 'image_tasks_total{status="created"} 2' in body
    assert "image_stage_seconds_bucket" in body
    assert "image_in_flight" in body
    assert "version=0.0.4" in resp.headers["content-type"]


def test_metrics_endpoint_optional_token(env, monkeypatch):
    monkeypatch.setenv("IMAGE_METRICS_TOKEN", "probe-token")
    reset_image_settings()
    assert env.get("/api/image/metrics").status_code == 401
    ok = env.get("/api/image/metrics", headers={"Authorization": "Bearer probe-token"})
    assert ok.status_code == 200
    assert "image_tasks_total" in ok.text


def test_metrics_counts_swallowed_and_cost(env):
    image_metrics.record_swallowed("outbox_replay")
    image_metrics.record_provider_calls(3, cost_cny=0.6)
    body = env.get("/api/image/metrics").text
    assert 'image_swallowed_total{kind="outbox_replay"}' in body
    assert "image_provider_calls_total 3" in body
    assert "image_cost_cny_total 0.6" in body


# ---------------------------------------------------------------- 4.4：LLM 解析接线
def test_llm_parse_disabled_by_default(env):
    attrs, warnings = asyncio.run(llm_parse.parse_clothing_attrs(_state(), get_image_settings()))
    assert attrs is None and warnings == []


def test_llm_parse_feeds_raw_into_schema(env, monkeypatch):
    """模型输出（含 ```json 围栏）→ parse_attrs 收口 → 身份锁定字段可用。"""
    monkeypatch.setenv("IMAGE_LLM_PARSE_ENABLED", "true")
    reset_image_settings()
    stub = _StubLLM('```json\n{"product_type": "针织开衫（短款）", "category": "女装", '
                    '"color_hex": "#C8B7A6", "logo_text": "胸前 LILY 刺绣", '
                    '"must_not_change": ["颜色", "版型", "刺绣文字"]}\n```')
    _install_stub(monkeypatch, stub)

    before = image_metrics.snapshot()
    attrs, warnings = asyncio.run(llm_parse.parse_clothing_attrs(_state(), get_image_settings()))
    assert attrs is not None and warnings == []
    assert attrs["product_type"] == "针织开衫（短款）"
    assert attrs["color_hex"] == "#C8B7A6"
    assert attrs["must_not_change"] == ["颜色", "版型", "刺绣文字"]
    assert attrs["source"] == "llm", "来源要标成 llm（模型输出走 schema 收口）"
    after = image_metrics.snapshot()
    assert after["llm_calls"] > before["llm_calls"], "LLM 用量要进 counters"
    assert after["llm_tokens"] > before["llm_tokens"]


def test_llm_parse_degrades_on_error(env, monkeypatch):
    monkeypatch.setenv("IMAGE_LLM_PARSE_ENABLED", "true")
    reset_image_settings()
    _install_stub(monkeypatch, _StubLLM(boom=True))
    attrs, warnings = asyncio.run(llm_parse.parse_clothing_attrs(_state(), get_image_settings()))
    assert attrs is None
    assert any("IMG_LLM_PARSE_DEGRADED" in item for item in warnings), warnings


def test_llm_parse_bad_json_falls_back_to_heuristic(env, monkeypatch):
    monkeypatch.setenv("IMAGE_LLM_PARSE_ENABLED", "true")
    reset_image_settings()
    _install_stub(monkeypatch, _StubLLM("这不是 JSON"))
    attrs, warnings = asyncio.run(llm_parse.parse_clothing_attrs(_state(), get_image_settings()))
    assert attrs is not None, "坏 JSON 也要退回启发式，而不是让任务失败"
    assert any("IMG_SCHEMA_INVALID" in item for item in warnings), warnings


def test_vision_disabled_warns_when_enabled_without_key(env, monkeypatch):
    monkeypatch.setenv("IMAGE_VISION_ENABLED", "true")
    reset_image_settings()
    attrs, warnings = asyncio.run(llm_parse.parse_clothing_attrs(_state(), get_image_settings()))
    assert attrs is None
    assert any("IMG_VISION_DISABLED" in item for item in warnings), warnings
