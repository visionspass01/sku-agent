"""`IMAGE_PRODUCT_SOURCE=mcp`：McpCatalog 的错误翻译 + 经 MCP 走完整条链路（进程内中台）。"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.mcp import product_image_server as server
from app.agents_image.sources import product_source as ps
from app.agents_image.sources.mcp_catalog import McpCatalog
from app.agents_image.store.schema import ensure_image_schema
from app.agents_image.store.users import ensure_seed_users
from app.core.errors import ImageError
from app.db.engine import session_scope
from tests.image_platform_support import (
    TOKEN,
    init_platform_db,
    platform_client,
    platform_transport,
    reset_platform_engine,
)


def _direct_caller():
    """测试里让 MCP 工具直接跑在本进程（等价 direct transport），链路仍经中台 REST。"""

    async def call(tool: str, arguments: dict) -> dict:
        return await server.IMAGE_TOOLS[tool](**arguments)

    return call


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def platform_db(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("catalog_mode")
    init_platform_db(tmp)
    yield tmp
    reset_platform_engine()


@pytest.fixture()
def mcp_env(platform_db, monkeypatch, _db, app_client, tmp_path):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_PLATFORM_URL", "http://platform.test")
    monkeypatch.setenv("IMAGE_PRODUCT_SOURCE", "mcp")
    reset_image_settings()
    reset_throttle()
    image_metrics.reset()
    ensure_image_schema()
    with session_scope() as session:
        ensure_seed_users(session)

    server.set_transport(platform_transport())
    ps.reset_catalogs()
    ps._ensure_builtin()
    ps._INSTANCES["mcp"] = McpCatalog(caller=_direct_caller())
    yield app_client
    server.reset_transport()
    ps.reset_catalogs()
    ps._ensure_builtin()
    reset_image_settings()
    reset_throttle()
    image_metrics.reset()


def _login(client) -> dict:
    resp = client.post("/api/image/auth/login", json={"username": "admin", "password": "hhw123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


# ---------------------------------------------------------------- McpCatalog 契约
def test_mcp_catalog_maps_platform_data(platform_db, monkeypatch):
    monkeypatch.setenv("IMAGE_PLATFORM_URL", "http://platform.test")
    reset_image_settings()
    server.set_transport(platform_transport())
    try:
        catalog = McpCatalog(caller=_direct_caller())
        products = run(catalog.list_products(keyword="针织"))
        assert [item.product_id for item in products] == ["P-1001"]

        data = run(catalog.get_product("P-1001"))
        assert data.attrs["must_not_change"]
        assert len(data.images) == 3 and data.images[0].is_main

        compositions = run(catalog.list_compositions())
        assert any(item.label == "正面全身像·上装突出" for item in compositions)
        policy = run(catalog.get_plan_policy())
        assert policy.default_count == 6 and len(policy.slots) == 9
        rules = run(catalog.get_platform_rules(platform="淘系"))
        assert rules.aspect == "1:1"
    finally:
        server.reset_transport()
        reset_image_settings()


def test_mcp_catalog_maps_not_found_and_invalid(platform_db, monkeypatch):
    monkeypatch.setenv("IMAGE_PLATFORM_URL", "http://platform.test")
    reset_image_settings()
    server.set_transport(platform_transport())
    try:
        catalog = McpCatalog(caller=_direct_caller())
        with pytest.raises(ImageError) as exc:
            run(catalog.get_product("P-9999"))
        assert exc.value.code == "IMG_PRODUCT_NOT_FOUND"

        broken = McpCatalog(caller=lambda tool, args: _async({"product_id": "P-1",
                                                             "attrs": "not-a-dict",
                                                             "images": []}))
        with pytest.raises(ImageError) as exc:
            run(broken.get_product("P-1"))
        assert exc.value.code == "IMG_UPSTREAM_INVALID"
    finally:
        server.reset_transport()
        reset_image_settings()


async def _async(value):
    return value


def test_mcp_catalog_maps_transport_failure_to_503(platform_db, monkeypatch):
    monkeypatch.setenv("IMAGE_PLATFORM_URL", "http://platform.test")
    reset_image_settings()

    async def boom(tool: str, arguments: dict):
        raise ConnectionError("mcp down")

    catalog = McpCatalog(caller=boom)
    with pytest.raises(ImageError) as exc:
        run(catalog.list_products())
    assert exc.value.code == "IMG_UPSTREAM_MCP_UNAVAILABLE"
    assert exc.value.retryable is True
    reset_image_settings()


# ---------------------------------------------------------------- 端到端（mcp 模式）
def test_mcp_mode_full_chain_and_backfill(mcp_env):
    headers = _login(mcp_env)
    created = mcp_env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1002"], "prompt": "白底主图，正面全身像上装突出", "n": 1,
        "request_id": "req-e2e-1",
    })
    assert created.status_code == 201, created.text
    task_id = created.json()["tasks"][0]["task_id"]

    deadline = time.time() + 20
    detail: dict = {}
    while time.time() < deadline:
        detail = mcp_env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if (detail.get("status") in {"done", "failed"}
                and detail.get("backfill_status") in {"sent", "failed"}):
            break
        time.sleep(0.05)
    assert detail["status"] == "done", detail
    # 参考图确实来自中台（URL 指向中台 asset 接口，落地后存本地 refs）
    assert detail["refs"] and detail["refs"][0]["kind"] == "ref"
    assert detail["product_id"] == "P-1002"
    assert detail["backfill_status"] == "sent"

    with platform_client() as pc:
        rows = pc.get("/api/v1/image/results", params={"product_id": "P-1002"},
                      headers=TOKEN).json()
    assert rows["count"] >= 1
    assert rows["items"][0]["task_id"] == task_id
    assert rows["items"][0]["storage"] == "local"      # 只回写引用，不回写图片
    assert not rows["items"][0]["storage_key"].startswith("http")


def test_mcp_mode_platform_down_returns_503(mcp_env, monkeypatch):
    """中台 500 → 建任务直接 503，任务表零残留。"""
    headers = _login(mcp_env)
    before = mcp_env.get("/api/image/stats", headers=headers).json()["tasks_total"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "中台炸了"})

    server.set_transport(httpx.MockTransport(handler))
    try:
        resp = mcp_env.post("/api/image/tasks", headers=headers, json={
            "product_ids": ["P-1001"], "prompt": "白底主图",
        })
    finally:
        server.set_transport(platform_transport())
    assert resp.status_code == 503
    assert resp.json()["error_code"] == "IMG_UPSTREAM_MCP_UNAVAILABLE"

    after = mcp_env.get("/api/image/stats", headers=headers).json()["tasks_total"]
    assert after == before


def test_mcp_mode_products_and_catalog_forwarded(mcp_env):
    headers = _login(mcp_env)
    products = mcp_env.get("/api/image/products?keyword=工装", headers=headers).json()["products"]
    assert [item["product_id"] for item in products] == ["P-1002"]
    catalog = mcp_env.get("/api/image/catalog", headers=headers).json()
    assert any(item["label"] == "影棚正式风" for item in catalog["scene_styles"])
    assert catalog["plan_policy"]["default_count"] == 6
