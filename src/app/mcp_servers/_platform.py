"""MCP server → 数据中台 REST 的薄适配层（MCP 层不再碰任何数据库）。

背景（评审 耦合点1）：
早先 3 个 MCP server 全部直连 **Agent 自己的 SQLite**（`app.db.engine.session_scope`），
于是同一个 Agent 里「一半业务数据走受治理的中台、另一半绕过中台直读本地表」：
- 身份/配额/审计只覆盖前者，多 Agent 场景下后者完全不可追责；
- 换个 Agent 要用订单数据，只能复制那几张表，或者依赖 sku-agent 的实现。

现在 MCP 只保留「工具名 + 参数 → 中台端点」的映射（`transport` 能力不变：
memory/stdio/direct 都能用），业务数据 100% 从中台取，且以自己的身份
（`X-Agent-Id: mcp-<server>`）进审计与配额。

错误语义（与原实现保持一致，调用方不用改）：
- 4xx（405/401/403/400/404）→ 返回 `{"error": ..., "error_kind": ...}`（业务错误，**不计熔断**）；
- 5xx / 连接失败 → 抛 `McpPlatformError`（计入 MCP 熔断，避免每个工具都先等满超时）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx

from app.config import get_settings
from app.resilience import Breaker

logger = logging.getLogger("sku-agent.mcp.platform")


class McpPlatformError(RuntimeError):
    """中台链路故障（5xx/连接失败）：可重试、计入熔断。"""


_transport: httpx.AsyncBaseTransport | None = None
_client: httpx.AsyncClient | None = None
_client_loop: Any = None
_breakers: dict[str, Breaker] = {}


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """测试注入（httpx.MockTransport）；置 None 表示恢复真实网络。"""
    global _transport, _client, _client_loop
    _transport = transport
    _client = None
    _client_loop = None


def reset_transport() -> None:
    set_transport(None)
    _breakers.clear()


def _breaker(server: str) -> Breaker:
    breaker = _breakers.get(server)
    if breaker is None:
        settings = get_settings()
        breaker = Breaker(
            threshold=int(getattr(settings, "mcp_breaker_threshold", 3)),
            reset_seconds=float(getattr(settings, "mcp_breaker_reset_seconds", 60.0)),
        )
        _breakers[server] = breaker
    return breaker


def _client_for_loop() -> httpx.AsyncClient:
    """懒建并复用 AsyncClient；脚本里多次 `asyncio.run` 时按事件循环重建。"""
    global _client, _client_loop
    try:
        loop: Any = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _client is None or _client.is_closed or (_client_loop is not None and _client_loop is not loop):
        settings = get_settings()
        _client = httpx.AsyncClient(
            transport=_transport,
            timeout=float(getattr(settings, "platform_timeout_seconds", 15.0)),
            proxy=getattr(settings, "platform_proxy", "") or None,
            trust_env=False,
        )
        _client_loop = loop
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _token() -> str:
    settings = get_settings()
    # MCP 有独立身份（可选）：审计里能区分「对话工具走的 MCP」与「直连 REST」
    return str(getattr(settings, "platform_mcp_token", "") or settings.platform_internal_token)


async def call(server: str, method: str, path: str, *,
               params: dict | None = None, json_body: dict | None = None) -> dict:
    """调一个中台端点。返回 dict（可能是业务错误），链路故障抛 McpPlatformError。"""
    settings = get_settings()
    base_url = str(settings.platform_api_url).rstrip("/")
    breaker = _breaker(server)
    if not breaker.allow():
        state = breaker.state()
        raise McpPlatformError(
            f"数据中台熔断中（{state['retry_in_s']}s 后重试）：最近 {state['failures']} 次链路失败"
        )
    trace_id = ""
    try:
        from app.observability import trace as trace_mod

        trace_id = trace_mod.current_trace_id()
    except Exception:  # noqa: BLE001 —— 观测失败不影响取数
        trace_id = ""
    suffix = uuid.uuid4().hex[:6]
    request_id = f"{trace_id}:mcp-{server}:{suffix}" if trace_id else f"mcp-{server}:{suffix}"
    headers = {
        "X-Internal-Token": _token(),
        "X-Agent-Id": f"mcp-{server}",
        "X-Request-Id": request_id,
        "X-Trace-Id": trace_id,
    }
    try:
        resp = await _client_for_loop().request(
            method, base_url + path, params=params, json=json_body, headers=headers,
        )
    except Exception as exc:  # noqa: BLE001 —— 链路故障计入熔断
        breaker.record_failure()
        raise McpPlatformError(f"数据中台不可用：{type(exc).__name__}: {exc}") from exc
    finally:
        breaker.release_probe()

    if resp.status_code >= 500:
        breaker.record_failure()
        raise McpPlatformError(
            f"数据中台接口错误 HTTP {resp.status_code}: {_detail(resp)}")
    if resp.status_code >= 400:
        # 4xx 是调用方的问题（不存在/越权/参数），不计熔断，按业务错误返回
        return {"error": _detail(resp), "error_kind": _kind_of(resp.status_code),
                "status_code": resp.status_code, "request_id": request_id}
    breaker.record_success()
    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise McpPlatformError(f"中台响应不是合法 JSON：{exc}") from exc
    if isinstance(payload, dict):
        payload.setdefault("request_id", request_id)
        return payload
    return {"items": payload, "request_id": request_id}


def _kind_of(status: int) -> str:
    return {400: "invalid", 401: "auth", 403: "permission", 404: "not_found"}.get(status, "business")


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            return str(body.get("detail") or body.get("error") or "")[:300]
    except Exception:  # noqa: BLE001
        pass
    return (resp.text or "")[:300]


def is_error(payload: Any) -> bool:
    return isinstance(payload, dict) and bool(payload.get("error"))
