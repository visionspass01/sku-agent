"""SKU 参考目录缓存（Agent 侧作用域判定的数据来源）。

要点：
- 目录来自中台 `/metadata/catalog`（元数据，不含库存销量），分页拉全量；
- 平台不可用时不阻塞启动，`status()` 里能看出 degraded 与原因；
- 目录未加载时回落到本地演示库（过渡路径，Wave 2 完成后删除）；
- 目录命中失败按「不可见」处理（fail-closed），绝不猜。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest


@pytest.fixture(autouse=True)
def _reset_catalog():
    from app.tools import catalog

    catalog.reset()
    yield
    catalog.reset()


def _install(handler) -> None:  # noqa: ANN001
    from app.tools import platform_tools

    platform_tools.set_platform_client(platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler),
    ))


def _uninstall() -> None:
    from app.tools import platform_tools

    platform_tools.set_platform_client(None)


def test_sync_paginates_and_indexes():
    from app.tools import catalog

    pages = {
        0: {"items": [
            {"sku_id": "ST-01-C1-M", "style_id": "ST-01", "category": "女装",
             "department": "女装", "color": "黑色", "size": "M"},
            {"sku_id": "ST-01-C1-L", "style_id": "ST-01", "category": "女装",
             "department": "女装", "color": "黑色", "size": "L"},
        ], "total": 3, "next_offset": 2, "data_version": 7},
        2: {"items": [
            {"sku_id": "ST-05-C1-M", "style_id": "ST-05", "category": "男装",
             "department": "男装", "color": "黑色", "size": "M"},
        ], "total": 3, "next_offset": None, "data_version": 7},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        return httpx.Response(200, json=pages[offset])

    _install(handler)
    try:
        status = asyncio.run(catalog.sync())
        assert status["loaded"] is True
        assert status["skus"] == 3
        assert status["styles"] == 2
        assert status["data_version"] == 7
        assert catalog.sku_category("ST-01-C1-M") == "女装"
        assert catalog.sku_category("ST-05-C1-M") == "男装"
        assert catalog.style_of("ST-01-C1-M") == "ST-01"
        assert len(catalog.style_skus("ST-01")) == 2
    finally:
        _uninstall()


def test_platform_failure_keeps_agent_alive():
    from app.tools import catalog

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "down"})

    _install(handler)
    try:
        status = asyncio.run(catalog.sync())
        assert status["loaded"] is False
        assert status["error"], "失败原因必须可见（否则排障只能猜）"
    finally:
        _uninstall()


def test_missing_sku_is_fail_closed(seeded):
    """目录里没有的 SKU：作用域判定按不可见处理（宁严不宽）。"""
    from app.auth import AuthUser
    from app.security import scope
    from app.tools import catalog

    assert catalog.is_loaded() is False
    worker = AuthUser(username="worker1", role="user", department="男装")
    # 目录未加载 → 走本地演示库兜底（seeded 里有 ST-92-C1-M 属男装、ST-90-C1-M 属女装）
    assert scope.ensure_sku_visible(worker, "ST-90-C1-M") is not None
    assert scope.ensure_sku_visible(worker, "ST-92-C1-M") is None
    assert scope.ensure_sku_visible(worker, "ST-99-C1-M") is not None


def test_visibility_uses_platform_catalog_not_local_db(seeded):
    """目录加载后，判定只看中台目录：本地库有、中台没声明的 SKU 也按不可见处理。"""
    from app.auth import AuthUser
    from app.security import scope
    from app.tools import catalog

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "items": [{"sku_id": "ST-92-C1-M", "style_id": "ST-92", "category": "男装",
                       "department": "男装", "color": "黑色", "size": "M"}],
            "total": 1, "next_offset": None, "data_version": 3,
        })

    _install(handler)
    try:
        asyncio.run(catalog.sync())
        worker = AuthUser(username="worker1", role="user", department="男装")
        assert scope.ensure_sku_visible(worker, "ST-92-C1-M") is None
        # 本地 seeded 库里有 ST-90-C1-M，但中台目录没声明 → 不可见（以中台为准）
        assert scope.ensure_sku_visible(worker, "ST-90-C1-M") is not None
        assert catalog.status()["source"] == "platform"
    finally:
        _uninstall()


def test_needs_refresh_tracks_data_version():
    from app.tools import catalog

    assert catalog.needs_refresh(None) is True

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "items": [{"sku_id": "ST-01-C1-M", "style_id": "ST-01", "category": "女装",
                       "department": "女装"}],
            "total": 1, "next_offset": None, "data_version": 5,
        })

    _install(handler)
    try:
        asyncio.run(catalog.sync())
        assert catalog.needs_refresh(5) is False
        assert catalog.needs_refresh(6) is True       # 中台数据版本变了 → 该刷新
        assert catalog.needs_refresh(None, ttl_seconds=-1.0) is True
    finally:
        _uninstall()
