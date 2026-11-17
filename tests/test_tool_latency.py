"""工具延迟修复：PlatformClient 复用 httpx 连接池（否则每次新建 client 要 0.5~1.4s）。"""

from __future__ import annotations

import asyncio

import httpx


def _transport() -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(200, json={"sku_id": "ST-92-C1-M", "available": 10})
    )


def test_platform_client_reuses_single_http_client():
    from app.tools.platform_tools import PlatformClient

    client = PlatformClient(base_url="http://mock.platform", token="t", transport=_transport())

    async def run():
        first = client._get_client()
        async with httpx.AsyncClient() as _:  # 打散一次事件循环，确认不是偶然缓存
            pass
        second = client._get_client()
        await client.get("/api/v1/skus/ST-92-C1-M/inventory")
        third = client._get_client()
        await client.aclose()
        return id(first), id(second), id(third), client._client

    a, b, c, after_close = asyncio.run(run())
    assert a == b == c, "同一个 PlatformClient 必须复用同一个 AsyncClient"
    assert after_close is None, "aclose() 之后连接池应被清空"


def test_platform_client_does_not_trust_env_proxy():
    """httpx 默认 trust_env=True 会读 Windows 注册表代理，导致连 127.0.0.1 也被绕进代理。"""
    from app.tools.platform_tools import PlatformClient

    client = PlatformClient(base_url="http://mock.platform", token="t", transport=_transport())
    http_client = client._get_client()

    async def close():
        await client.aclose()

    assert getattr(http_client, "trust_env", None) is False or getattr(
        http_client, "_trust_env", None
    ) is False, "PlatformClient 必须关闭 trust_env，避免系统代理拖慢本地调用"
    asyncio.run(close())


def test_platform_call_latency_is_recorded_and_small(seeded):
    """复用连接后，中台调用样本应在毫秒级（MockTransport 下几乎为 0）。"""
    from app.observability import latency
    from app.tools.platform_tools import PlatformClient

    client = PlatformClient(base_url="http://mock.platform", token="t", transport=_transport())

    async def run():
        # 用不同路径避开 Agent 旁路缓存，确保 3 次都真正打到「中台」
        for sku in ("ST-01", "ST-02", "ST-03"):
            await client.get(f"/api/v1/skus/{sku}/inventory")
        await client.aclose()

    asyncio.run(run())
    snap = latency.snapshot()["categories"]["platform"]
    assert snap["count"] == 3
    assert snap["p95"] < 100, snap


def test_aclose_is_safe_when_never_used():
    from app.tools.platform_tools import PlatformClient

    client = PlatformClient(base_url="http://mock.platform", token="t", transport=_transport())
    asyncio.run(client.aclose())  # 没建过 client 也不应报错
