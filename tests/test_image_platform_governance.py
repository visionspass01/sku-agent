"""第三轮 W5：中台部门治理、outbox 自动重放、health 降级、清理范围、卡片过滤。"""

from __future__ import annotations

import datetime as dt

import pytest

from app.agents_image import graph
from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import get_image_settings, reset_image_settings
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.state import ImageTaskState
from app.agents_image.store import outbox as outbox_store
from app.agents_image.store import schema as schema_module
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema, reset_schema_issues
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
    yield app_client
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()
    reset_schema_issues()


def _admin_headers(env) -> dict:
    token = env.post("/api/image/auth/login",
                     json={"username": "admin", "password": "hhw123"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- P1-13：中台部门兜底
def _platform_client():
    from tests.image_platform_support import TOKEN, platform_client

    return TOKEN, platform_client()


@pytest.fixture(scope="module")
def platform_db(tmp_path_factory):
    from tests.image_platform_support import init_platform_db, reset_platform_engine

    tmp = tmp_path_factory.mktemp("w5_platform")
    init_platform_db(tmp)
    yield tmp
    reset_platform_engine()


def _department_scoped_key(monkeypatch, departments: list[str]) -> dict:
    """造一个"只能看某部门"的独立 key（`PLATFORM_API_KEYS` 主体）。"""
    import json as json_mod

    import data_platform.config as dp_config
    from data_platform.api import authz

    monkeypatch.setattr(dp_config, "API_KEYS_RAW", json_mod.dumps({
        "male-agent": {"key": "male-key", "scopes": ["read", "write"],
                       "departments": departments},
    }))
    authz.reset_principals()
    return {"X-Api-Key": "male-key"}


@pytest.fixture(autouse=True)
def _reset_platform_principals():
    yield
    from data_platform.api import authz

    authz.reset_principals()


def test_products_context_filters_by_department(platform_db, monkeypatch):
    client = _platform_client()[1]
    headers = _department_scoped_key(monkeypatch, ["男装"])
    with client as c:
        body = c.get("/api/v1/image/products/context",
                     params={"ids": "P-1001,P-1002"}, headers=headers).json()
    assert [item["product_id"] for item in body["products"]] == ["P-1002"]   # 男装
    assert body["forbidden"] == ["P-1001"]                                  # 女装被兜底拦下


def test_similar_by_image_filters_by_department(platform_db, monkeypatch):
    client = _platform_client()[1]
    headers = _department_scoped_key(monkeypatch, ["男装"])
    with client as c:
        body = c.post("/api/v1/image/similar-by-image", headers=headers,
                      json={"image_b64": "cXVlcnk=", "min_sales": 0, "top_n": 8}).json()
    assert body["count"] >= 1
    assert body["filtered"] >= 1
    assert {item["product_id"] for item in body["items"]} <= {"P-1002", "P-3002"}


# ---------------------------------------------------------------- P1-14：outbox 自动重放
def test_outbox_replay_marks_sent(env):
    import asyncio

    with session_scope() as session:
        row = outbox_store.enqueue(session, task_id="202611191200000a01",
                                   payload={"task_id": "202611191200000a01",
                                            "results": []},
                                   error="boom")
        row.next_retry_at = dt.datetime.now() - dt.timedelta(seconds=1)
        session.flush()

    class _Catalog:
        async def save_result(self, payload):
            return {"ok": True}

    # 第四轮 P1-5：replay 自己管短会话，网络 IO 不在会话里
    result = asyncio.run(outbox_store.replay(catalog=_Catalog(), limit=10))
    pending = outbox_store.pending_payloads(limit=10)
    assert result["sent"] >= 1
    assert all(item["task_id"] != "202611191200000a01" for item in pending)


def test_outbox_loop_is_wired():
    """后台要能自动重放（不再只靠人工点按钮）。"""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1]
              / "src/app/agents_image/runtime.py").read_text(encoding="utf-8")
    assert "_outbox_loop" in source
    assert "outbox_replay_seconds" in source
    settings = get_image_settings()
    assert hasattr(settings, "outbox_replay_seconds")
    assert hasattr(settings, "outbox_replay_batch")


def test_outbox_enqueue_failure_is_visible(env, monkeypatch):
    """入队失败要留告警，不能只打日志（否则这批结果永久缺失且没人知道）。"""
    state = ImageTaskState(task_id="202611191200000a02", username="admin")

    def boom(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(outbox_store, "enqueue", boom)
    graph._enqueue_outbox(state, {"task_id": "202611191200000a02"})
    assert any("IMG_OUTBOX_ENQUEUE_FAILED" in item for item in state.warnings)


# ---------------------------------------------------------------- P2-4：health 降级一致
def test_runtime_health_reflects_schema_degraded(env):
    from app.agents_image.runtime import health_block

    reset_schema_issues()
    assert health_block()["degraded"] is False
    schema_module._ISSUES.append("index uq_x: IntegrityError duplicate")
    block = health_block()
    assert block["degraded"] is True
    assert "uq_x" in block["degraded_reason"]
    reset_schema_issues()


# ---------------------------------------------------------------- P2-5/P2-6：清理与关停
def test_sweep_includes_blocked_dir(env, tmp_path):
    """blocked 资产也要参与 24h 清理（否则无上限增长）。"""
    import os

    from app.agents_image.runtime import sweep_local_files

    blocked_dir = get_image_settings().blocked_dir
    blocked_dir.mkdir(parents=True, exist_ok=True)
    path = blocked_dir / "old.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    old = dt.datetime.now() - dt.timedelta(hours=48)
    os.utime(path, (old.timestamp(), old.timestamp()))
    result = sweep_local_files()
    assert result["removed"] >= 1
    assert not path.exists()


def test_reset_for_tests_cancels_background_tasks():
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src/app/agents_image/runtime.py"
    ).read_text(encoding="utf-8")
    assert "_recovery_task" in source and "_outbox_task" in source
    assert source.count("_stop_task") >= 2          # shutdown 与 reset 都停后台任务


# ---------------------------------------------------------------- P2-16：回写带 blocked/warnings
def test_backfill_payload_carries_blocked_and_partial(env, monkeypatch):
    captured: dict = {}

    class _Catalog:
        async def save_result(self, payload):
            captured.update(payload)
            return {"ok": True}

    state = ImageTaskState(task_id="202611191200000a03", username="admin",
                           prompt="白底主图", count=2)
    state.warnings.append("IMG_SLOT_MISSING: 预期 2 张，输出 1 张")
    state.expected_images = 2
    state.partial = True
    monkeypatch.setattr("app.agents_image.sources.product_source.get_catalog",
                        lambda: _Catalog())
    import asyncio

    asyncio.run(graph._backfill(state, outputs_by_slot=[], blocked=[{"slot": 2, "reasons": ["x"]}]))
    assert captured["expected_images"] == 2
    assert captured["partial"] is True
    assert captured["blocked"] == [{"slot": 2, "reasons": ["x"]}]
    assert any("IMG_SLOT_MISSING" in item for item in captured["warnings"])


# ---------------------------------------------------------------- P2-13：死参清理
def test_iterate_hint_mentions_gap_fill():
    from app.agents_image.api.routes import _variant_matrix

    variants = _variant_matrix(None, {"mode": "iterate"})
    assert "补齐" in variants[0]["hint"]
