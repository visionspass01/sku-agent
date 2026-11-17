"""Agent → 中台 的调用契约（治理下沉后新增）。

- 必须带 `X-Agent-Id` / `X-Request-Id`（中台按身份授权、按请求 id 审计）；
- 中台回显的 `request_id` 要落到工具结果与 run_detail 的步骤里（可对账）；
- 401/403/429 要映射成不同的 `error_kind`，且**不能**计入中台熔断（中台是好的，是我的问题）。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest


def _client(handler):  # noqa: ANN001
    from app.tools.platform_tools import PlatformClient

    return PlatformClient(base_url="http://mock.platform", token="k-test",
                          transport=httpx.MockTransport(handler))


def _install(monkeypatch, client) -> None:  # noqa: ANN001
    from app.tools import platform_tools

    platform_tools.set_platform_client(client)


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"sku_id": "ST-92-C1-M", "available": 510},
                          headers={"X-Request-Id": request.headers.get("X-Request-Id", "")})


def test_sends_agent_id_and_request_id(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["agent_id"] = request.headers.get("X-Agent-Id")
        seen["request_id"] = request.headers.get("X-Request-Id")
        seen["token"] = request.headers.get("X-Internal-Token")
        return _ok(request)

    client = _client(handler)
    _install(monkeypatch, client)
    data = asyncio.run(client.get("/api/v1/skus/ST-92-C1-M/inventory"))

    assert seen["agent_id"] == client.agent_id
    assert seen["token"] == "k-test"
    assert seen["request_id"], "必须传关联 id，否则无法和中台审计行对账"
    assert data["request_id"] == seen["request_id"]


def test_request_id_prefers_platform_echo(monkeypatch):
    """中台回显的 request_id 更权威（可能是它生成的），以它为准。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"available": 1},
                              headers={"X-Request-Id": "server-side-id"})

    client = _client(handler)
    _install(monkeypatch, client)
    data = asyncio.run(client.get("/api/v1/skus/ST-92-C1-M/inventory"))
    assert data["request_id"] == "server-side-id"


@pytest.mark.parametrize(
    "status,kind",
    [(401, "auth"), (403, "permission"), (429, "ratelimited")],
)
def test_auth_and_quota_errors_map_to_kinds(monkeypatch, status, kind):
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Retry-After": "7"} if status == 429 else {}
        return httpx.Response(status, json={"detail": "denied"}, headers=headers)

    client = _client(handler)
    _install(monkeypatch, client)
    data = asyncio.run(client.get("/api/v1/skus/ST-92-C1-M/inventory"))

    assert data["error_kind"] == kind
    assert "next_step" in data, "要告诉模型下一步怎么做，否则它会原地重试"
    assert data["request_id"]
    if status == 429:
        assert data["retry_after_s"] == "7"
        assert "重试" in data["next_step"]


@pytest.mark.parametrize("status", [401, 403, 429])
def test_auth_errors_do_not_trip_the_breaker(monkeypatch, status):
    """中台是好的、只是我的 key/配额不对：这三个都不该计入熔断，否则会把正常调用也挡住。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "denied"})

    client = _client(handler)
    _install(monkeypatch, client)
    for _ in range(client._breaker.threshold + 2):
        asyncio.run(client.get("/api/v1/skus/ST-92-C1-M/inventory"))
    assert client.breaker_state()["state"] == "closed"


def test_5xx_does_trip_the_breaker(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "boom"})

    client = _client(handler)
    _install(monkeypatch, client)
    for _ in range(client._breaker.threshold):
        asyncio.run(client.get("/api/v1/skus/ST-92-C1-M/inventory"))
    assert client.breaker_state()["state"] == "open"


def test_tool_step_carries_request_id(seeded, monkeypatch):
    """工具步骤里要能看到 request_id（run_detail 时间轴据此对账中台审计）。"""
    from app.memory.runtime import current_turn_log, turn_log_context

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sku_id": "ST-92-C1-M", "available": 510},
                              headers={"X-Request-Id": "trace-abc"})

    client = _client(handler)
    _install(monkeypatch, client)

    from app.auth import AuthUser
    from app.tools.platform_tools import build_platform_tools

    tools = {t.name: t for t in build_platform_tools(AuthUser(
        username="admin", role="admin", department=None))}
    with turn_log_context("t_reqid", "s-reqid"):
        asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-92-C1-M"}))
        steps = list(current_turn_log()["steps"])

    assert steps and steps[0]["request_id"] == "trace-abc"
