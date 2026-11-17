"""订单反查 SKU 的缓存语义（复审 F4）：只缓存确定性结论，链路故障不固化。

背景：`_order_sku` 早先无论成败都写缓存 → 中台抖动一次，部门账号在 10 分钟内查该订单
都会拿到"订单不存在"（误报）。方向仍然是 fail-closed（拿不到 SKU 就当看不见），
但"不知道"不该被缓存成"不存在"。
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _clean_cache():
    from app.security.scope import reset_order_cache

    reset_order_cache()
    yield
    reset_order_cache()


def _patch_hub(monkeypatch, handler):
    """把 MCP hub 换成可控桩，并统计 `call` 次数。"""
    import app.mcp_client.client as mcp_client

    calls = {"n": 0}

    class _Hub:
        async def call(self, server, tool, arguments=None):  # noqa: ANN001, ANN002
            calls["n"] += 1
            return handler(calls["n"])

    monkeypatch.setattr(mcp_client, "get_mcp_hub", lambda: _Hub())
    return calls


def test_transient_failure_is_not_cached(monkeypatch):
    """链路故障 → 不写缓存：第二次查询会重新打 MCP（而不是复用"不存在"）。"""
    def handler(_n: int) -> dict:
        raise RuntimeError("connection reset by peer")

    calls = _patch_hub(monkeypatch, handler)
    from app.security.scope import _order_sku

    assert asyncio.run(_order_sku("SO-1")) is None
    assert asyncio.run(_order_sku("SO-1")) is None
    assert calls["n"] == 2, "故障结果不该被缓存"


def test_definitive_not_found_is_cached(monkeypatch):
    """中台明确返回 found=False → 是确定性结论，缓存（避免每次追问都打一次 MCP）。"""
    def handler(_n: int) -> dict:
        return {"order_id": "SO-2", "found": False, "note": "订单不存在"}

    calls = _patch_hub(monkeypatch, handler)
    from app.security.scope import _order_sku

    assert asyncio.run(_order_sku("SO-2")) is None
    assert asyncio.run(_order_sku("SO-2")) is None
    assert calls["n"] == 1, "确定性结论应当命中缓存"


def test_found_order_caches_the_sku(monkeypatch):
    def handler(_n: int) -> dict:
        return {"order_id": "SO-3", "found": True, "order": {"sku_id": "ST-92-C1-M"}}

    calls = _patch_hub(monkeypatch, handler)
    from app.security.scope import _order_sku

    assert asyncio.run(_order_sku("SO-3")) == "ST-92-C1-M"
    assert asyncio.run(_order_sku("SO-3")) == "ST-92-C1-M"
    assert calls["n"] == 1


def test_error_payload_is_not_cached(monkeypatch):
    """MCP 返回 error 载荷（而不是抛异常）同样属于"不确定"，不缓存。"""
    def handler(_n: int) -> dict:
        return {"error": "订单服务暂不可用"}

    calls = _patch_hub(monkeypatch, handler)
    from app.security.scope import _order_sku

    assert asyncio.run(_order_sku("SO-4")) is None
    assert asyncio.run(_order_sku("SO-4")) is None
    assert calls["n"] == 2


def test_ensure_order_visible_denies_when_lookup_fails(monkeypatch):
    """授权方向不变：查不到 SKU → 部门账号看到"订单不存在"（fail-closed），admin 不受影响。"""
    def handler(_n: int) -> dict:
        raise RuntimeError("boom")

    _patch_hub(monkeypatch, handler)
    from app.auth import AuthUser
    from app.security.scope import ensure_order_visible

    worker = AuthUser(username="worker1", role="user", department="男装")
    denied = asyncio.run(ensure_order_visible(worker, "SO-5"))
    assert denied and denied.get("error_kind") == "business"
    assert "不存在" in denied.get("error", "")

    admin = AuthUser(username="admin", role="admin", department=None)
    assert asyncio.run(ensure_order_visible(admin, "SO-5")) is None
