"""MCP 工具层（image_*，7 个）契约：参数映射、4xx 业务错误、5xx/网络失败重试后抛错。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.agents_image.config import reset_image_settings
from app.agents_image.mcp import product_image_server as server
from tests.image_platform_support import init_platform_db, platform_transport, reset_platform_engine


@pytest.fixture(scope="module")
def platform_db(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("mcp_image")
    init_platform_db(tmp)
    yield tmp
    reset_platform_engine()


@pytest.fixture()
def wired(platform_db, monkeypatch):
    """把 MCP 工具的 httpx 客户端接到中台应用（进程内，无端口）。"""
    monkeypatch.setenv("IMAGE_PLATFORM_URL", "http://platform.test")
    monkeypatch.setenv("IMAGE_MCP_RETRIES", "1")
    reset_image_settings()
    server.set_transport(platform_transport())
    yield server
    server.reset_transport()
    reset_image_settings()


def run(coro):
    return asyncio.run(coro)


def test_list_products(wired):
    body = run(wired.image_list_products(keyword="", limit=3))
    assert body["count"] == 3
    assert body["products"][0]["product_id"]


def test_get_product_includes_images(wired):
    body = run(wired.image_get_product("P-1002"))
    assert body["attrs"]["must_not_change"]
    assert len(body["images"]) == 2
    assert body["images"][0]["is_main"] is True
    assert body["images"][0]["url"].endswith(".png")


def test_get_product_missing_returns_business_error(wired):
    body = run(wired.image_get_product("P-9999"))
    assert body["error_kind"] == "not_found"
    assert body["status_code"] == 404


def test_compositions_and_scene_styles(wired):
    compositions = run(wired.image_list_compositions(platform="淘系"))
    assert compositions["count"] >= 4
    assert {item["kind"] for item in compositions["items"]} == {"composition"}
    styles = run(wired.image_list_scene_styles())
    assert any(item["label"] == "工厂实用风" for item in styles["items"])


def test_plan_policy_and_rules(wired):
    policy = run(wired.image_get_plan_policy())["plan_policy"]
    assert policy["default_count"] == 6 and len(policy["slots"]) == 9
    rules = run(wired.image_get_platform_rules("淘系"))["platform_rules"]
    assert rules["aspect"] == "1:1"


def test_save_result_roundtrip_and_idempotent(wired):
    payload = {"task_id": "t-mcp-1", "product_id": "P-3001", "request_id": "r-mcp-1",
               "results": [{"slot": 1, "sha256": "c" * 64}]}
    first = run(wired.image_save_result(payload))
    assert first["saved"] == 1 and first["deduped"] == 0
    again = run(wired.image_save_result(payload))
    assert again["saved"] == 0 and again["deduped"] == 1


def test_download_image(wired):
    raw = run(wired.download_image("/api/v1/image/assets/P-1001-img-1.png"))
    assert raw.startswith(b"\x89PNG")


def test_tool_registry_has_seven_tools(wired):
    assert set(wired.IMAGE_TOOLS) == set(wired.IMAGE_TOOL_NAMES)
    assert len(wired.IMAGE_TOOL_NAMES) == 7


def test_missing_token_surfaces_business_error(platform_db, monkeypatch):
    """令牌错 → 中台 401 → 工具返回业务错误（不是静默降级成默认值）。"""
    monkeypatch.setenv("IMAGE_PLATFORM_URL", "http://platform.test")
    monkeypatch.setenv("IMAGE_PLATFORM_TOKEN", "wrong-token")
    reset_image_settings()
    server.set_transport(platform_transport())
    try:
        body = run(server.image_list_products())
        assert body["error_kind"] == "auth"
        assert body["status_code"] == 401
    finally:
        server.reset_transport()
        reset_image_settings()


def test_server_error_after_retries_raises(platform_db, monkeypatch):
    monkeypatch.setenv("IMAGE_PLATFORM_URL", "http://platform.test")
    monkeypatch.setenv("IMAGE_MCP_RETRIES", "1")
    reset_image_settings()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"detail": "boom"})

    server.set_transport(httpx.MockTransport(handler))
    try:
        with pytest.raises(server.ImagePlatformError):
            run(server.image_get_product("P-1001"))
        assert calls["n"] == 2      # 原始 1 次 + 重试 1 次
    finally:
        server.reset_transport()
        reset_image_settings()


def test_network_error_after_retries_raises(platform_db, monkeypatch):
    monkeypatch.setenv("IMAGE_PLATFORM_URL", "http://platform.test")
    monkeypatch.setenv("IMAGE_MCP_RETRIES", "0")
    reset_image_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    server.set_transport(httpx.MockTransport(handler))
    try:
        with pytest.raises(server.ImagePlatformError):
            run(server.image_list_scene_styles())
    finally:
        server.reset_transport()
        reset_image_settings()
