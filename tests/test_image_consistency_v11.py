"""第五轮 W6：一致性小项（P1-5/6/8/9/10/11/12）。

- P1-5：iterate 显式改平台/变体/颜色 → 一定落 `IMG_REUSE_SKIPPED`（并指出差异维度）；
- P1-6：`admin/tasks?limit=abc` → 422（不再 500）；
- P1-8：SSE 上限可配（默认 300s）；
- P1-9：召回缓存接共享 TTL 缓存（fakeredis/Redis），health 暴露 `cache_backend` 与 `throttle_scope`；
- P1-10：iterate 的部分成功文案带复用张数；快照有 `total_images`；
- P1-11：终态集合单点（common.TERMINAL_STATUSES）；
- P1-12：身份锁定检查单点（schemas.identity_missing），validate 与 critic 同源。
"""

from __future__ import annotations

import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.nodes import critic as critic_mod
from app.agents_image.nodes import validate as validate_node
from app.agents_image.rag import index as rag_index
from app.agents_image.schemas import identity_missing
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.state import ImageTaskState, PromptDraft
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.core.errors import ImageError
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
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
    rag_index.reset_index()
    yield app_client
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()
    rag_index.reset_index()


def login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _wait(env, headers, task_id, timeout=30.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail.get("status") in {"done", "failed", "canceled"}:
            return detail
        time.sleep(0.05)
    return detail


# ---------------------------------------------------------------- P1-5：复用未命中必告警
def test_iterate_with_explicit_platform_that_differs_warns(env):
    """基座 淘系、iterate 显式改成抖系 → 复用链必然不命中，必须说清楚（过去静默）。"""
    headers = login(env)
    base = env.post("/api/image/tasks", headers=headers,
                    json={"product_ids": ["P-3001"], "prompt": "白底主图",
                          "platform": "淘系", "slot_count": 3, "image_count": 2})
    assert base.status_code == 201, base.text
    base_id = base.json()["tasks"][0]["task_id"]
    assert _wait(env, headers, base_id)["status"] == "done"

    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "只改主图",
                             "mode": "iterate", "base_task_id": base_id,
                             "platform": "抖系", "slot_count": 3, "image_count": 1})
    assert created.status_code == 201, created.text
    assert created.json()["reused_images"] == 0
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    skipped = [item for item in detail["warnings"] if item.startswith("IMG_REUSE_SKIPPED")]
    assert skipped, detail["warnings"]
    assert "平台" in skipped[0], skipped[0]


# ---------------------------------------------------------------- P1-6：非法 limit
def test_admin_tasks_invalid_limit_returns_422(env):
    headers = login(env)
    resp = env.get("/api/image/admin/tasks?limit=abc", headers=headers)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error_code"] == "IMG_INPUT_INVALID"


# ---------------------------------------------------------------- P1-8：SSE 上限可配
def test_sse_max_seconds_is_configurable(env, monkeypatch):
    from app.agents_image.api import stream_routes

    assert stream_routes._max_stream_seconds() == 360        # 默认：覆盖调用方 360s 最大等待
    monkeypatch.setenv("IMAGE_SSE_MAX_SECONDS", "240")
    reset_image_settings()
    assert stream_routes._max_stream_seconds() == 240


# ---------------------------------------------------------------- P1-9：共享召回缓存
def test_rag_cache_uses_shared_ttl_cache_and_reports_backend(env):
    backend = rag_index.cache_backend()
    assert backend in {"redis", "fakeredis", "memory"}, backend

    first = rag_index.retrieve("主图 白底 占比", top_k=3)
    before = rag_index.cache_stats()
    second = rag_index.retrieve("主图 白底 占比", top_k=3)
    after = rag_index.cache_stats()
    assert after["hits"] == before["hits"] + 1, (before, after)
    assert [hit.snippet for hit in first] == [hit.snippet for hit in second], \
        "缓存往返后片段必须一致（序列化/反序列化不能丢内容）"

    health = env.get("/api/image/health").json()
    assert health["cache_backend"] == backend
    assert health["throttle_scope"] == "instance"


# ---------------------------------------------------------------- P1-11：终态集合单点
def test_terminal_statuses_are_shared():
    from app.agents_image.api import batch_routes, common, stream_routes

    assert batch_routes._TERMINAL is common.TERMINAL_STATUSES
    assert stream_routes._TERMINAL is common.TERMINAL_STATUSES


# ---------------------------------------------------------------- P1-12：身份锁定单点
def _draft(slot: int = 1, preserve: list[str] | None = None) -> PromptDraft:
    return PromptDraft(slot=slot, role="主图", prompt="白底主图", aspect="1:1",
                       preserve=list(preserve if preserve is not None else []))


def test_identity_missing_is_single_source_of_truth():
    drafts = [_draft(1, ["颜色"]), _draft(2, ["颜色", "版型"])]
    missing = identity_missing(drafts, ["颜色", "版型", "图案"])
    assert missing == [(1, ["版型", "图案"]), (2, ["图案"])]

    state = ImageTaskState(prompt="白底主图", aspect="1:1", count=1)
    state.attrs = {"must_not_change": ["颜色", "版型"]}
    state.prompt_pack = [_draft(1, ["颜色"])]
    # validate 抛错（生产路径）
    with pytest.raises(ImageError) as exc:
        validate_node._validate_prompt_pack(state)
    assert exc.value.code == "IMG_SCHEMA_INVALID"
    assert "丢失身份锁定" in exc.value.detail

    # critic 判 blocking（同一条判定）
    result = critic_mod.critique(state, [_draft(1, ["颜色"])], [])
    assert "identity" in result.consistency["blocking"]


# ---------------------------------------------------------------- P1-10：复用张数进文案
def test_iterate_partial_message_mentions_reused(env):
    from app.agents_image.api.routes import _partial_fields

    task = type("T", (), {"image_count": 2, "delivered_images": 1, "reused_images": 3,
                          "status": "done", "error_code": ""})()
    fields = _partial_fields(task)
    assert fields["total_images"] == 4
    assert "另有 3 张复用" in fields["message"]
