"""P1-2 错误契约：预算/循环/步数类错误必须「可读 + 带错误码 + 带追踪标记」。

早先的问题：`TurnBudgetExceeded` 在 `src/app` 里没有任何捕获点，`POST /api/chat` 直接
500 通用体——精心写好的「请再次重试」文案到不了用户；流式反而因为前端把业务失败当
传输失败而**自动降级 POST**，等于把同一个循环再跑一遍。
"""

from __future__ import annotations

import inspect


def _raise_in_run_chat(exc):
    """把 run_chat 换成「直接抛指定异常」的桩（路由层不需要真模型）。"""

    async def _boom(*args, **kwargs):
        raise exc

    return _boom


def _run_with_slow_agent(_settings, monkeypatch, *, timeout: float = 0.2):
    """让一轮对话必然触发硬超时（不需要真模型）。"""
    import asyncio

    from app.agents import chat_agent
    from app.auth import AuthUser

    class _SlowAgent:
        async def ainvoke(self, payload):  # noqa: ANN001
            await asyncio.sleep(1.0)
            return {"messages": payload["messages"]}

    async def _meta():
        return "口径", 1

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda *a, **k: _SlowAgent())
    monkeypatch.setattr(chat_agent, "_metadata_text", _meta)
    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(_settings, "chat_turn_timeout_seconds", timeout)
    user = AuthUser(username="worker1", role="user", department="男装")
    return asyncio.run(chat_agent.run_chat(user, [], "查库存"))


def test_hard_timeout_keeps_trace_marker(seeded, _settings, monkeypatch):
    """复审 D2：硬超时这条路径也必须带追踪标记（早先会给出「追踪 -」）。"""
    import pytest

    from app.agents.chat_agent import TurnBudgetExceeded
    from app.api.chat_routes import _agent_failure

    with pytest.raises(TurnBudgetExceeded) as err:
        _run_with_slow_agent(_settings, monkeypatch)
    exc = err.value
    assert getattr(exc, "trace_id", ""), "硬超时异常必须带 trace_id"
    assert exc.trace_id != "-", "硬超时不该退化成「追踪 -」"
    detail = _agent_failure(exc)["detail"]
    assert exc.trace_id in detail
    assert "追踪 -" not in detail


def test_loop_abort_detail_has_real_trace(seeded, monkeypatch):
    """循环中止同样要带真实 trace（与硬超时同一套补丁）。"""
    import asyncio
    import itertools

    import pytest
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from langgraph.prebuilt import create_react_agent

    from app.agents import chat_agent
    from app.agents.tool_guard import ToolLoopError
    from app.api.chat_routes import _agent_failure
    from app.auth import AuthUser
    from app.tools.platform_tools import build_platform_tools

    class _Looping(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):  # noqa: ANN001
            return self

    def _ai(i: int) -> AIMessage:
        return AIMessage(content="", tool_calls=[
            {"name": "get_metric_definitions", "args": {}, "id": f"c{i}"},
        ])

    user = AuthUser(username="worker1", role="user", department="男装")
    tools = [t for t in build_platform_tools(user, allow=["get_metric_definitions"])]
    agent = create_react_agent(_Looping(messages=itertools.chain(_ai(i) for i in range(20))),
                               tools)

    async def _meta():
        return "口径", 1

    monkeypatch.setattr(chat_agent, "_metadata_text", _meta)
    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda *a, **k: agent)

    with pytest.raises(ToolLoopError) as err:
        asyncio.run(chat_agent.run_chat(user, [], "口径是什么"))
    detail = _agent_failure(err.value)["detail"]
    assert err.value.trace_id and err.value.trace_id != "-"
    assert "追踪 -" not in detail



def test_agent_failure_maps_known_errors():
    from app.agents.chat_agent import TurnBudgetExceeded
    from app.agents.tool_guard import ToolLoopError
    from app.api.chat_routes import _agent_failure

    loop = ToolLoopError("工具调用陷入循环，请再次重试（错误码 TOOL_LOOP_REPEAT｜追踪 t_1）",
                         code="TOOL_LOOP_REPEAT", trace_id="t_1")
    fail = _agent_failure(loop)
    assert fail["status"] == 422
    assert fail["error_code"] == "TOOL_LOOP_REPEAT"
    assert fail["trace_id"] == "t_1"
    assert fail["fatal"] is True
    assert "工具调用陷入循环" in fail["detail"]

    budget = TurnBudgetExceeded("本轮重试/降级预算 90s 已用尽")
    budget.trace_id = "t_2"
    fail = _agent_failure(budget)
    assert fail["error_code"] == "TURN_BUDGET_EXCEEDED"
    assert "TURN_BUDGET_EXCEEDED" in fail["detail"]
    assert "追踪 t_2" in fail["detail"]
    # 保留原始原因，便于前端/日志解释「为什么用尽」
    assert "预算 90s" in fail["reason"]

    class GraphRecursionError(RuntimeError):   # 与 langgraph 同名，按类名识别
        pass

    step = _agent_failure(GraphRecursionError("Recursion limit of 34 reached"))
    assert step["error_code"] == "AGENT_STEP_LIMIT"
    assert "AGENT_STEP_LIMIT" in step["detail"]


def test_stream_failure_without_deltas_keeps_fallback(app_client, login, monkeypatch):
    """没有任何输出就失败（典型：传输抖动）：不标 fatal，仍允许前端 POST 兜底。"""
    from app.api import chat_routes

    exc = RuntimeError("connection reset")

    async def _boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(chat_routes, "run_chat_stream", _boom)
    headers = login("worker1", "hhw111")
    with app_client.stream("POST", "/api/chat/stream", headers=headers,
                           json={"message": "看下 ST-01-C1-M"}) as resp:
        text = "".join(resp.iter_text())
    payload = [line for line in text.splitlines() if line.startswith("data:")][-1]
    assert "connection reset" in payload
    assert '"fatal": true' not in payload


def test_stream_mid_stream_failure_is_fatal(app_client, login, monkeypatch):
    """复审 N6：已推过增量后失败 → fatal + STREAM_INTERRUPTED，前端不再自动重发。"""
    from app.api import chat_routes

    exc = RuntimeError("upstream 502 after deltas")
    exc.partial_deltas = 3          # 模拟「已经推给用户 3 段 token」
    exc.trace_id = "t_mid1"

    async def _boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(chat_routes, "run_chat_stream", _boom)
    headers = login("worker1", "hhw111")
    with app_client.stream("POST", "/api/chat/stream", headers=headers,
                           json={"message": "看下 ST-01-C1-M"}) as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())

    payload = [line for line in text.splitlines() if line.startswith("data:")][-1]
    assert "STREAM_INTERRUPTED" in payload
    assert "t_mid1" in payload
    assert '"fatal": true' in payload
    assert "不会自动重发" in payload


def test_agent_failure_returns_none_for_unknown_errors():
    from app.api.chat_routes import _agent_failure

    assert _agent_failure(RuntimeError("随便什么错")) is None


def test_chat_endpoint_returns_422_with_code_and_trace(app_client, login, monkeypatch):
    from app.agents.chat_agent import TurnBudgetExceeded
    from app.api import chat_routes

    exc = TurnBudgetExceeded("本轮重试预算 90s 已用尽，已中止")
    exc.trace_id = "t_abc123"
    monkeypatch.setattr(chat_routes, "run_chat", _raise_in_run_chat(exc))
    headers = login("worker1", "hhw111")

    resp = app_client.post("/api/chat", headers=headers, json={"message": "看下 ST-01-C1-M"})
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert "TURN_BUDGET_EXCEEDED" in body["detail"]
    assert "t_abc123" in body["detail"]
    assert resp.headers.get("X-Error-Code") == "TURN_BUDGET_EXCEEDED"
    assert resp.headers.get("X-Trace-Id") == "t_abc123"


def test_tool_loop_error_surfaces_the_required_sentence(app_client, login, monkeypatch):
    """需求原文：前端要看到「工具调用陷入循环，请再次重试 + 错误代码 + 追踪链路标记」。"""
    from app.agents.tool_guard import ToolLoopError
    from app.api import chat_routes

    exc = ToolLoopError("工具调用陷入循环，请再次重试（错误码 TOOL_LOOP_REPEAT｜追踪 t_loop1）",
                        code="TOOL_LOOP_REPEAT", trace_id="t_loop1")
    monkeypatch.setattr(chat_routes, "run_chat", _raise_in_run_chat(exc))
    headers = login("worker1", "hhw111")

    resp = app_client.post("/api/chat", headers=headers, json={"message": "看下 ST-01-C1-M"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail.startswith("工具调用陷入循环，请再次重试")
    assert "TOOL_LOOP_REPEAT" in detail and "t_loop1" in detail


def test_stream_error_event_carries_fatal_and_codes(app_client, login, monkeypatch):
    """SSE：错误事件要带 fatal（前端据此不再自动降级 POST）。"""
    from app.agents.tool_guard import ToolLoopError
    from app.api import chat_routes

    exc = ToolLoopError("工具调用陷入循环，请再次重试（错误码 TOOL_LOOP_FAILURES｜追踪 t_s1）",
                        code="TOOL_LOOP_FAILURES", trace_id="t_s1")

    async def _boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(chat_routes, "run_chat_stream", _boom)
    headers = login("worker1", "hhw111")

    with app_client.stream("POST", "/api/chat/stream", headers=headers,
                           json={"message": "看下 ST-01-C1-M"}) as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())

    assert "event: error" in text
    payload = [line for line in text.splitlines() if line.startswith("data:")][-1]
    assert "TOOL_LOOP_FAILURES" in payload
    assert "t_s1" in payload
    assert '"fatal": true' in payload


def test_stream_route_does_not_fall_back_for_fatal_errors():
    """前端契约：fatal 的 SSE 错误必须标 noFallback（纯静态检查，防止回退被删掉）。"""
    from pathlib import Path

    js = Path(__file__).resolve().parent.parent / "web" / "app.js"
    text = js.read_text(encoding="utf-8")
    assert "if (streamError.fatal) err.noFallback = true;" in text
    # 旧的写法（只存 message）不该再出现：那会把 payload 里的 fatal 丢掉
    assert 'if (event === "error") streamError = payload.message' not in text


def test_tool_guard_errors_are_never_retried():
    from app.agents.chat_agent import TurnBudgetExceeded
    from app.agents.tool_guard import ToolLoopError
    from app.resilience import is_retryable

    assert is_retryable(ToolLoopError("loop", code="TOOL_LOOP_REPEAT")) is False
    assert is_retryable(TurnBudgetExceeded("budget")) is False


# ---------------------------------------------------------------- 工具名幻觉（复审 A3）
def test_agent_failure_maps_invalid_tool_name():
    from app.agents.tool_guard import InvalidToolCallError
    from app.api.chat_routes import _agent_failure

    exc = InvalidToolCallError(["get_inventory_v2"], ["get_inventory"], trace_id="t_tool1")
    fail = _agent_failure(exc)
    assert fail is not None
    assert fail["status"] == 422
    assert fail["error_code"] == "AGENT_INVALID_TOOL_NAME"
    assert fail["trace_id"] == "t_tool1"
    assert fail["fatal"] is True            # 关键：前端不自动降级 POST（否则重跑一整轮）
    assert "get_inventory_v2" in fail["detail"]
    assert "get_inventory" in fail["detail"]


def test_chat_endpoint_returns_422_for_invalid_tool_name(app_client, login, monkeypatch):
    """非流式：422 + X-Error-Code + X-Trace-Id（而不是空消息的 500）。"""
    from app.agents.tool_guard import InvalidToolCallError
    from app.api import chat_routes

    exc = InvalidToolCallError(["no_such_tool"], ["get_inventory"], trace_id="t_tool2")
    monkeypatch.setattr(chat_routes, "run_chat", _raise_in_run_chat(exc))
    headers = login("worker1", "hhw111")

    resp = app_client.post("/api/chat", headers=headers, json={"message": "看下 ST-01-C1-M"})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "no_such_tool" in detail and "AGENT_INVALID_TOOL_NAME" in detail
    assert resp.headers.get("X-Error-Code") == "AGENT_INVALID_TOOL_NAME"
    assert resp.headers.get("X-Trace-Id") == "t_tool2"


def test_stream_invalid_tool_name_is_fatal(app_client, login, monkeypatch):
    """流式：错误事件带 fatal，前端据此不自动重发（避免同一轮跑两遍）。"""
    from app.agents.tool_guard import InvalidToolCallError
    from app.api import chat_routes

    exc = InvalidToolCallError(["no_such_tool"], ["get_inventory"], trace_id="t_tool3")

    async def _boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(chat_routes, "run_chat_stream", _boom)
    headers = login("worker1", "hhw111")

    with app_client.stream("POST", "/api/chat/stream", headers=headers,
                           json={"message": "看下 ST-01-C1-M"}) as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())

    assert "event: error" in text
    payload = [line for line in text.splitlines() if line.startswith("data:")][-1]
    assert "AGENT_INVALID_TOOL_NAME" in payload
    assert "t_tool3" in payload
    assert '"fatal": true' in payload


def test_invalid_tool_name_is_never_transient_retried():
    """纠错重试之外不应被 transient 逻辑再重试一遍（它是确定性的模型错误）。"""
    from app.agents.tool_guard import InvalidToolCallError
    from app.resilience import is_retryable

    assert is_retryable(InvalidToolCallError(["x"], ["y"])) is False


def test_agent_failure_maps_invalid_tool_args():
    """复审 R2：参数非法的错误码必须与"工具名不存在"分开，前端才不会误判。"""
    from app.agents.tool_guard import invalid_tool_args_error
    from app.api.chat_routes import _agent_failure

    exc = invalid_tool_args_error(["get_inventory"], ["get_inventory"], trace_id="t_args1")
    fail = _agent_failure(exc)
    assert fail is not None
    assert fail["status"] == 422
    assert fail["error_code"] == "AGENT_INVALID_TOOL_ARGS"
    assert fail["trace_id"] == "t_args1"
    assert fail["fatal"] is True
    assert "JSON" in fail["detail"]


def test_active_error_templates_cover_both_invalid_tool_codes():
    """两套文案都要在表里（漏一个就会出现"错误码有了但文案是兜底"）。"""
    from app.api.chat_routes import AGENT_FAILURE_TEMPLATES

    assert "AGENT_INVALID_TOOL_NAME" in AGENT_FAILURE_TEMPLATES
    assert "AGENT_INVALID_TOOL_ARGS" in AGENT_FAILURE_TEMPLATES


def test_source_has_no_other_catchers_that_would_swallow_them():
    """回归护栏：`_invoke_agent` 里必须先识别循环/步数错误再判 transient。"""
    from app.agents import chat_agent

    src = inspect.getsource(chat_agent._invoke_agent)
    assert "ToolLoopError" in src
    assert src.index("ToolLoopError") < src.index("_is_transient(exc)")
