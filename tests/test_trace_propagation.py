"""全链路 trace（评审 2.4-13）。

一次用户提问会产生 5 类日志：对话轮次（run_detail）、中台审计（api_audit_log）、
MCP 调用、RAG 检索、LLM 调用。没有统一 id 时只能靠时间戳猜；这里断言它们共享同一个
`trace_id`（= 本轮 turn_id），并且它会随 `X-Trace-Id` 传给中台。
"""

from __future__ import annotations

import asyncio
import logging

import httpx


def test_trace_falls_back_to_turn_id():
    """没有显式 trace 时，从本轮 turn_log 的 turn_id 推导（ReAct 循环里不需要到处手写）。"""
    from app.memory.runtime import append_llm_call, turn_log_context
    from app.observability import trace

    with turn_log_context("t_abc123", session_id="s1"):
        assert trace.current_trace_id() == "t_abc123"
        snapshot = trace.current_trace()
        assert snapshot["session_id"] == "s1"
        append_llm_call({"kind": "answer"})
    assert trace.current_trace_id() == ""


def test_explicit_trace_context_wins():
    from app.observability import trace

    with trace.trace_context("t_explicit", agent_id="diagnostician", session_id="s9"):
        assert trace.current_trace_id() == "t_explicit"
        assert trace.trace_status()["agent_id"] == "diagnostician"
    assert trace.current_trace_id() == ""


def test_trace_survives_thread_offload():
    """`asyncio.to_thread` 不继承 contextvar：`run_in_trace` 必须把它带过去。"""
    from app.observability import trace

    async def main():
        with trace.trace_context("t_thread"):
            return await trace.run_in_trace(trace.current_trace_id)

    assert asyncio.run(main()) == "t_thread"


def test_logging_filter_injects_trace_id():
    from app.observability import trace

    trace.install_logging()
    record = logging.LogRecord("sku-agent.test", logging.INFO, __file__, 1, "hi", None, None)
    trace.TraceIdFilter().filter(record)
    assert record.trace_id == trace.NO_TRACE      # 无轮次上下文时是占位符
    with trace.trace_context("t_log"):
        record2 = logging.LogRecord("sku-agent.test", logging.INFO, __file__, 1, "hi", None, None)
        trace.TraceIdFilter().filter(record2)
        assert record2.trace_id == "t_log"


def test_platform_request_id_is_prefixed_by_trace(seeded):
    """中台调用的 request_id 形如 `{trace_id}:{随机}`：审计行可按前缀一次捞全。"""
    from app.tools import platform_tools

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"request_id": request.headers.get("X-Request-Id", ""),
                     "trace_id": request.headers.get("X-Trace-Id", "")})
        return httpx.Response(200, json={"sku_id": "ST-92-C1-M", "available": 1,
                                         "schema_version": "1.1"})

    client = platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler))
    platform_tools.set_platform_client(client)
    try:
        from app.memory.runtime import turn_log_context

        with turn_log_context("t_trace42"):
            asyncio.run(client.get_fresh("/api/v1/skus/ST-92-C1-M/metrics"))
    finally:
        platform_tools.set_platform_client(None)

    assert seen and seen[0]["trace_id"] == "t_trace42"
    assert seen[0]["request_id"].startswith("t_trace42:")


def test_mcp_adapter_sends_trace_header(seeded):
    """MCP 通道同样带 trace：否则多 Agent 里"客服 Agent 调的订单接口"就串不起来。"""
    from app.mcp_servers import _platform

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"request_id": request.headers.get("X-Request-Id", ""),
                     "trace_id": request.headers.get("X-Trace-Id", ""),
                     "agent_id": request.headers.get("X-Agent-Id", "")})
        return httpx.Response(200, json={"order_id": "ORD-1", "found": False})

    _platform.set_transport(httpx.MockTransport(handler))
    try:
        from app.observability import trace

        async def main():
            with trace.trace_context("t_mcp7"):
                return await _platform.call("order", "GET", "/api/v1/orders/ORD-1")

        asyncio.run(main())
    finally:
        _platform.reset_transport()

    assert seen[0]["trace_id"] == "t_mcp7"
    assert seen[0]["request_id"].startswith("t_mcp7:")
    assert seen[0]["agent_id"] == "mcp-order"


def test_tool_step_records_trace_id(seeded):
    """run_detail 的工具步骤里带 trace_id（前端运行详情可复制去对账）。"""
    from app.auth import AuthUser
    from app.memory.runtime import turn_log_context
    from app.tools import platform_tools

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path})
        return httpx.Response(200, json={"sku_id": "ST-92-C1-M", "available": 5,
                                         "schema_version": "1.1"})

    platform_tools.set_platform_client(platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler)))
    admin = AuthUser(username="admin", role="admin", department=None)
    tools = {t.name: t for t in platform_tools.build_platform_tools(admin)}
    try:
        with turn_log_context("t_step"):
            from app.memory.runtime import current_turn_log

            asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-92-C1-M"}))
            steps = (current_turn_log() or {}).get("steps") or []
    finally:
        platform_tools.set_platform_client(None)
    assert steps and steps[0]["trace_id"] == "t_step"


def test_health_and_metrics_expose_trace(app_client, login):
    headers = login("admin", "hhw123")
    health = app_client.get("/api/health").json()
    assert "trace" in health and "trace_id" in health["trace"]
    assert health["trace"]["logging_installed"] is True
    metrics = app_client.get("/api/metrics", headers=headers).json()
    assert "trace" in metrics
