"""第三轮 W6：前端能力（颜色多选 / 图位数 / 模式 / 双平台 / iterate / 部分成功可见）。
后端契约：商品清单返回颜色候选、SSE 快照带部分成功字段。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


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


def login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _wait(env, headers, task_id, timeout=45.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail.get("status") in {"done", "failed", "canceled"} and \
                detail.get("backfill_status") != "pending":
            return detail
        time.sleep(0.05)
    return detail


# ---------------------------------------------------------------- 商品清单带颜色候选
def test_products_endpoint_returns_colors(env):
    headers = login(env)
    body = env.get("/api/image/products?limit=20", headers=headers).json()
    assert body["products"]
    by_id = {item["product_id"]: item for item in body["products"]}
    colors = {entry["color"] for entry in by_id["P-1001"]["colors"]}
    assert colors == {"米色", "黑色", "藏蓝"}
    assert all(entry["color_hex"] for entry in by_id["P-1001"]["colors"])
    # 单色商品也返回一个颜色
    assert by_id["P-3001"]["colors"][0]["color"] == "黑色"


# ---------------------------------------------------------------- SSE 快照：部分成功字段
def test_snapshot_carries_partial_fields(env):
    from app.agents_image.api import stream_routes
    from app.agents_image.store import tasks as task_store

    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1})
    task_id = created.json()["tasks"][0]["task_id"]
    detail = _wait(env, headers, task_id)
    assert detail["status"] == "done"
    with session_scope() as session:
        snapshot = stream_routes._snapshot_payload(task_store.get_task(session, task_id))
    for key in ("expected_images", "partial", "message", "warnings", "blocked"):
        assert key in snapshot, key
    assert snapshot["partial"] is False


# ---------------------------------------------------------------- 前端契约（源码级）
def test_frontend_sends_slot_and_image_count(env):
    js = (WEB / "image.js").read_text(encoding="utf-8")
    assert "slot_count:" in js and "image_count:" in js
    assert 'id="slot-count-input"' in (WEB / "image.html").read_text(encoding="utf-8")
    assert 'id="mode-select"' in (WEB / "image.html").read_text(encoding="utf-8")
    assert 'id="platform-select"' in (WEB / "image.html").read_text(encoding="utf-8")


def test_frontend_supports_color_selection(env):
    js = (WEB / "image.js").read_text(encoding="utf-8")
    assert "colorsSelected" in js
    assert "product_colors" in js and "all_colors" in js
    assert "color-check" in js


def test_frontend_supports_iterate_and_partial(env):
    js = (WEB / "image.js").read_text(encoding="utf-8")
    assert "attachIterateButton" in js
    assert "mode: \"iterate\"" in js or 'mode: "iterate"' in js
    # 部分成功文案由后端给（message），前端判断的是 partial 字段
    assert "data.partial" in js
    assert "data.message" in js
    # 第六轮 P1-5：失败原因（error_code）优先，输出/预期不能把它覆盖掉
    assert "const outputNote" in js
    assert "${data.error_code} · ${outputNote}" in js


def test_frontend_version_and_batch_restore(env):
    js = (WEB / "image.js").read_text(encoding="utf-8")
    html = (WEB / "image.html").read_text(encoding="utf-8")
    assert "image-v2" in js
    assert 'data-ui-build="image-v2"' in html
    assert "image_agent_batch" in js


def test_iterate_api_round_trip_with_color(env):
    """iterate 的 API 契约（前端按钮走的就是这套参数）。"""
    headers = login(env)
    base = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1001"],
        "product_colors": [{"product_id": "P-1001", "colors": ["米色"]}],
        "prompt": "白底主图", "slot_count": 3, "image_count": 2,
    })
    base_id = base.json()["tasks"][0]["task_id"]
    assert _wait(env, headers, base_id)["status"] == "done"
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1001"],
        "product_colors": [{"product_id": "P-1001", "colors": ["米色"]}],
        "prompt": "主图点击率低，只改主图", "mode": "iterate", "base_task_id": base_id,
        "slot_count": 3, "image_count": 1,
    })
    assert created.status_code == 201, created.text
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert detail["status"] == "done"
    assert detail["reused_images"] >= 1
    assert detail["base_task_id"] == base_id
