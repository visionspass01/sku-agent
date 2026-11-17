"""工具循环守护（P1-1）单测：重复指纹 / 连续失败 / 成功预算 / 惰性 / 记账。

设计要点：这些用例**不经过真实 LLM**，直接对 `with_tool_loop_guard` 包装后的工具
发调用——守护是纯确定性逻辑，应该能像限流器一样被单测钉死。
"""

from __future__ import annotations

import asyncio
import itertools

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import HumanMessage

from app.agents import tool_guard
from app.agents.tool_guard import (
    FAILURE_LIMIT,
    REPEAT_LIMIT,
    ToolLoopError,
    tool_guard_context,
    with_tool_loop_guard,
)
from app.auth import AuthUser
from app.memory.runtime import current_turn_log, turn_log_context
from app.observability import tool_stats
from app.resilience import is_retryable


def _tool(calls: list, *, fail: bool = False, name: str = "get_inventory"):
    """构造一个「入参入账、可指定失败」的假工具，并套上循环守护。

    `fail=True` 用**链路类**失败（transport）；业务错误用 `_failing(...)` 构造。
    """

    async def body(sku_id: str) -> dict:
        calls.append(sku_id)
        if fail:
            return {"error": "数据中台不可用（模拟）", "error_kind": "transport"}
        return {"sku_id": sku_id, "available": 10}

    return with_tool_loop_guard(name)(body)


def _failing(calls: list, error_kind: str, **extra):
    """构造「一定返回带 error_kind 的业务/链路错误」的工具。"""

    async def body(sku_id: str) -> dict:
        calls.append(sku_id)
        return {"error": f"{error_kind} 失败（模拟）", "error_kind": error_kind, **extra}

    return with_tool_loop_guard("get_inventory")(body)


def _call(tool, sku_id: str):
    return asyncio.run(tool(sku_id))


def test_repeat_fingerprint_trips_on_third_success():
    calls: list = []
    tool = _tool(calls)
    with tool_guard_context(15):
        _call(tool, "ST-01-C1-M")
        _call(tool, "ST-01-C1-M")
        with pytest.raises(ToolLoopError) as err:
            _call(tool, "ST-01-C1-M")
    exc = err.value
    assert exc.code == "TOOL_LOOP_REPEAT"
    # 文案：需求要求的「循环 + 再次重试 + 错误码 + 追踪标记」四件套
    assert "工具调用陷入循环，请再次重试" in str(exc)
    assert "TOOL_LOOP_REPEAT" in str(exc)
    assert "追踪" in str(exc)
    assert exc.fingerprint and exc.trace_id
    assert len(calls) == REPEAT_LIMIT  # 第 3 次仍算一次真实调用（成功后判定）
    assert not is_retryable(exc), "循环终止必须不可重试（重试只会再撞一次）"


def test_different_arguments_never_trip():
    calls: list = []
    tool = _tool(calls)
    with tool_guard_context(15):
        for sku in ("ST-01-C1-M", "ST-02-C1-M", "ST-03-C1-M", "ST-04-C1-M"):
            _call(tool, sku)
    assert len(calls) == 4


def test_interleaved_calls_reset_repeat_streak():
    calls: list = []
    tool = _tool(calls)
    with tool_guard_context(15):
        # A,A,B,A,A → 连续重复只有 2，不该触发
        for sku in ("A", "A", "B", "A", "A"):
            _call(tool, sku)
    assert calls == ["A", "A", "B", "A", "A"]


def test_three_consecutive_failures_trip():
    calls: list = []
    tool = _tool(calls, fail=True)
    with tool_guard_context(15):
        with pytest.raises(ToolLoopError) as err:
            for sku in ("A", "B", "C"):
                _call(tool, sku)
    assert err.value.code == "TOOL_LOOP_FAILURES"
    assert f"连续 {FAILURE_LIMIT} 次链路失败" in str(err.value)


def test_loop_thresholds_are_pinned_to_requirement():
    """复审 E3：阈值是需求写死的 3；改这里必须同步改规格/文档，不能靠改常量让测试变绿。"""
    from app.agents.tool_guard import FAILURE_LIMIT as FL
    from app.agents.tool_guard import REPEAT_LIMIT as RL

    assert (FL, RL) == (3, 3)


def test_three_distinct_business_errors_do_not_trip():
    """复审 D1：3 个不同 SKU 的「不存在」是正常排查，不是模型卡住。"""
    calls: list = []
    tool = _failing(calls, "business")
    with tool_guard_context(15) as state:
        for sku in ("ST-X1", "ST-X2", "ST-X3"):
            envelope = _call(tool, sku)
            assert envelope["error"]
        summary = tool_guard.guard_summary()
    assert len(calls) == 3
    assert summary["business_errors"] == 3
    assert summary["failure_streak"] == 0
    assert summary["aborted"] is False
    assert state["success_calls"] == 0


@pytest.mark.parametrize("kind", ["permission", "auth", "invalid", "contract"])
def test_non_transient_error_kinds_are_not_loops(kind):
    calls: list = []
    tool = _failing(calls, kind)
    with tool_guard_context(15):
        for sku in ("A", "B", "C", "D"):
            _call(tool, sku)
    assert len(calls) == 4, "业务/配置类错误不该触发循环中止"


@pytest.mark.parametrize("extra", [
    {},
    {"link_error": True},
    {"server_error": True},
    {"breaker_open": True},
])
def test_transient_error_flags_still_trip(extra):
    """链路类失败四种写法都要能判定：error_kind=transport / link_error / server_error / breaker_open。"""
    calls: list = []
    tool = _failing(calls, "transport", **extra)
    with tool_guard_context(15):
        with pytest.raises(ToolLoopError) as err:
            for sku in ("A", "B", "C"):
                _call(tool, sku)
    assert err.value.code == "TOOL_LOOP_FAILURES"


def test_business_error_breaks_transient_streak():
    """挂-挂-参数错-挂-挂：业务错误打断连续链，不该被算成连续 3 次链路失败。"""
    transport_calls: list = []
    business_calls: list = []
    flaky = _tool(transport_calls, fail=True, name="get_sales")
    denied = _failing(business_calls, "permission")
    with tool_guard_context(15):
        _call(flaky, "A")
        _call(flaky, "B")
        _call(denied, "X")        # 打断
        _call(flaky, "C")
        _call(flaky, "D")
    assert len(transport_calls) == 4, "chain 被打断后不应在第 3 次挂的时候中止"


def test_unknown_error_without_kind_still_counts():
    """工具内部异常被契约包成 error（没有 error_kind）：保守计入失败。"""
    calls: list = []

    async def boom(sku_id: str) -> dict:
        calls.append(sku_id)
        return {"error": "ValueError: 意外异常"}

    tool = with_tool_loop_guard("get_inventory")(boom)
    with tool_guard_context(15):
        with pytest.raises(ToolLoopError) as err:
            for sku in ("A", "B", "C"):
                _call(tool, sku)
    assert err.value.code == "TOOL_LOOP_FAILURES"


def test_empty_error_string_is_contract_violation_not_success():
    """复审 F4：`{"error": ""}` 带了 error 字段却没内容 —— 不能当成成功、更不能扣预算。"""
    calls: list = []

    async def empty(sku_id: str) -> dict:
        calls.append(sku_id)
        return {"error": "", "sku_id": sku_id}

    tool = with_tool_loop_guard("get_inventory")(empty)
    with tool_guard_context(15) as state:
        with pytest.raises(ToolLoopError) as err:
            for sku in ("A", "B", "C"):
                _call(tool, sku)
        assert state["success_calls"] == 0, "空 error 不该消耗成功预算"
    assert err.value.code == "TOOL_LOOP_FAILURES"


def test_empty_error_with_business_kind_is_still_business():
    """空 error + error_kind=business：算业务错误（只计数），不判循环。"""
    calls: list = []

    async def denied(sku_id: str) -> dict:
        calls.append(sku_id)
        return {"error": "", "error_kind": "business"}

    tool = with_tool_loop_guard("get_inventory")(denied)
    with tool_guard_context(15) as state:
        for sku in ("A", "B", "C", "D"):
            _call(tool, sku)
        assert state["business_errors"] == 4
        assert state["failure_streak"] == 0
        assert state["success_calls"] == 0


def test_success_resets_failure_streak():
    ok_calls: list = []
    bad_calls: list = []
    ok_tool = _tool(ok_calls)
    bad_tool = _tool(bad_calls, fail=True, name="get_sales")
    with tool_guard_context(15):
        _call(bad_tool, "A")
        _call(bad_tool, "B")
        _call(ok_tool, "C")          # 成功清零失败 streak
        _call(bad_tool, "D")
        _call(bad_tool, "E")         # 又只累计到 2
    assert len(bad_calls) == 4 and len(ok_calls) == 1


def test_budget_blocks_before_executing_the_extra_call():
    calls: list = []
    tool = _tool(calls)
    with tool_guard_context(2):
        _call(tool, "A")
        _call(tool, "B")
        with pytest.raises(ToolLoopError) as err:
            _call(tool, "C")
    assert err.value.code == "TOOL_BUDGET_EXCEEDED"
    assert "本轮工具调用已达上限" in str(err.value)
    assert calls == ["A", "B"], "超预算的那次调用不应该真的执行"


def test_guard_is_inert_without_turn_context():
    calls: list = []
    tool = _tool(calls)
    for sku in ("A", "A", "A", "A"):     # 没有 tool_guard_context
        _call(tool, sku)
    assert len(calls) == 4


def test_aborted_state_raises_without_calling_tool_again():
    calls: list = []
    tool = _tool(calls)
    with tool_guard_context(15) as state:
        for _ in range(REPEAT_LIMIT - 1):
            _call(tool, "A")
        with pytest.raises(ToolLoopError) as first:
            _call(tool, "A")
        executed = len(calls)
        state["success_calls"] = 99           # 人为把计数打乱，验证走的是 aborted 快路径
        with pytest.raises(ToolLoopError) as second:
            _call(tool, "Z")
    assert len(calls) == executed, "已终止后不应再执行任何工具"
    assert second.value.code == first.value.code


def test_trip_is_recorded_in_turn_log_and_tool_stats():
    calls: list = []
    tool = _tool(calls)
    tool_stats.reset()
    with turn_log_context("t_test_guard", "s1"), tool_guard_context(15):
        for _ in range(REPEAT_LIMIT - 1):
            _call(tool, "A")
        with pytest.raises(ToolLoopError):
            _call(tool, "A")
        steps = list((current_turn_log() or {}).get("steps") or [])
    aborts = [s for s in steps if s.get("status") == "loop_aborted"]
    assert aborts, steps
    assert aborts[0]["error_code"] == "TOOL_LOOP_REPEAT"
    assert aborts[0]["fingerprint"]
    stats = tool_stats.snapshot()
    assert stats["overall"]["outcomes"].get("loop_aborted") == 1, stats


def test_guard_summary_reports_budget_usage():
    calls: list = []
    tool = _tool(calls)
    with tool_guard_context(15):
        _call(tool, "A")
        summary = tool_guard.guard_summary()
    assert summary["aborted"] is False
    assert summary["max_calls"] == 15
    assert summary["success_calls"] == 1
    assert tool_guard.guard_summary() is None, "退出上下文后不应再报告本轮状态"


def test_fingerprint_is_stable_and_argument_sensitive():
    a = tool_guard.fingerprint({"sku_id": "A", "days": 7})
    b = tool_guard.fingerprint({"days": 7, "sku_id": "A"})
    c = tool_guard.fingerprint({"sku_id": "A", "days": 8})
    assert a == b, "键序不同不应改变指纹"
    assert a != c
    # 空入参也要有稳定指纹（无参工具）
    assert tool_guard.fingerprint({}) == tool_guard.fingerprint(None)


# ---------------------------------------------------------------- 集成：真实 ReAct 图
class _LoopingModel(GenericFakeChatModel):
    """一直要求「用同一入参调同一个工具」的假模型（模拟模型卡在循环里）。"""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        return self


def _looping_agent(tool_names: list[str], args: dict):
    from langchain_core.messages import AIMessage
    from langgraph.prebuilt import create_react_agent

    def _ai(_i: int) -> AIMessage:
        return AIMessage(content="", tool_calls=[
            {"name": tool_names[0], "args": args, "id": f"c{_i}"},
        ])

    model = _LoopingModel(messages=itertools.chain(_ai(i) for i in range(50)))
    return create_react_agent(model, tools_for_names(tool_names))


def tools_for_names(names: list[str]):
    from app.auth import AuthUser
    from app.tools.platform_tools import build_platform_tools

    user = AuthUser(username="worker1", role="user", department="男装")
    return [t for t in build_platform_tools(user, allow=names) if t.name in names]


def test_react_loop_trips_through_real_graph(seeded):
    """工具层守护必须真的能终止 LangGraph 的 ReAct 循环（不是只在装饰器层生效）。

    用 `get_metric_definitions`（无参、离线可用）保证 3 次调用都**成功**，
    走到「同工具 + 同入参连续成功」这条判定上。
    """
    agent = _looping_agent(["get_metric_definitions"], {})

    with tool_guard_context(15):
        with pytest.raises(ToolLoopError) as err:
            asyncio.run(agent.ainvoke({"messages": [HumanMessage("口径是什么")]}))

    exc = err.value
    assert exc.code == "TOOL_LOOP_REPEAT"
    assert exc.fingerprint, "必须能指认是哪次入参重复"
    assert "工具调用陷入循环" in str(exc)


def test_run_chat_aborts_and_reports_trace(seeded, monkeypatch):
    """整轮 `run_chat`：守护终止后异常带追踪标记（路由层据此拼 422 文案）。"""
    from app.agents import chat_agent

    agent = _looping_agent(["get_metric_definitions"], {})

    async def _meta():
        return "可用库存口径", 1

    monkeypatch.setattr(chat_agent, "_metadata_text", _meta)
    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda *a, **k: agent)

    user = AuthUser(username="worker1", role="user", department="男装")
    with pytest.raises(ToolLoopError) as err:
        asyncio.run(chat_agent.run_chat(user, [], "口径是什么"))
    exc = err.value
    assert exc.code == "TOOL_LOOP_REPEAT"
    assert exc.trace_id and exc.trace_id != "-", "必须有追踪链路标记"
    assert "TOOL_LOOP_REPEAT" in str(exc) and exc.trace_id in str(exc)


def test_budget_respects_manifest_max_tool_calls():
    """manifest 的 max_tool_calls 会被 `_tool_budget_of` 采纳（缺省回落全局预算）。"""
    from app.agents.chat_agent import _tool_budget_of
    from app.agents.registry import AgentManifest

    manifest = AgentManifest(id="t1", prompt="chat_system", tools=["read"], max_tool_calls=6)
    assert _tool_budget_of(manifest, object()) == 6
    assert _tool_budget_of(None, type("S", (), {"chat_tool_call_budget": 11})()) == 11


def test_built_agent_binds_recursion_limit_from_manifest(seeded):
    """保险绳真的绑到了图上：`recursion_limit = 3×预算 + 6`（hook 是独立节点，一轮 3 超步）。"""
    from app.agents import chat_agent
    from app.agents.registry import get_registry

    registry = get_registry()
    registry.reload()
    manifest = registry.get("operator")
    assert manifest is not None
    agent = chat_agent.build_chat_agent(
        AuthUser(username="worker1", role="user", department="男装"), "系统提示", manifest)
    assert agent.config.get("recursion_limit") == chat_agent._recursion_limit_for(
        manifest.max_tool_calls)
    # 公式本身也钉住：图上有 post_model_hook 节点 → 一轮 3 个超步 + 6 余量
    assert chat_agent._recursion_limit_for(15) == 3 * 15 + 6


def test_recursion_limit_formula_matches_graph_shape(seeded):
    """复审 R1：公式必须跟着**图结构**走——hook 节点存在与否会改变每轮超步数。"""
    from app.agents import chat_agent

    agent = chat_agent.build_chat_agent(
        AuthUser(username="worker1", role="user", department="男装"), "系统提示")
    nodes = set(agent.get_graph().nodes)
    assert "post_model_hook" in nodes, nodes          # 有 hook → 3 超步
    assert chat_agent._recursion_limit_for(6, hook=True) == 3 * 6 + 6
    assert chat_agent._recursion_limit_for(6, hook=False) == 2 * 6 + 6
    assert chat_agent._recursion_limit_for(0) == 3 * 1 + 6   # 预算下限 1 + 6 余量


def _distinct_arg_agent(limit: int):
    """每次入参都不同的循环模型 + 带守护的真工具（不会触发「同参重复」判定）。"""
    from langchain_core.messages import AIMessage
    from langchain_core.tools import StructuredTool
    from langgraph.prebuilt import create_react_agent

    calls: list[int] = []

    async def _probe(i: int) -> dict:
        calls.append(i)
        return {"ok": i}

    tool = StructuredTool.from_function(
        coroutine=tool_guard.with_tool_loop_guard("probe_tool")(_probe),
        name="probe_tool", description="探针工具（只为测量预算与步数的先后）")
    model = _LoopingModel(messages=itertools.chain(
        AIMessage(content="", tool_calls=[{
            "name": "probe_tool", "args": {"i": i}, "id": f"c{i}", "type": "tool_call",
        }]) for i in range(80)))
    return create_react_agent(model, [tool]).with_config({"recursion_limit": limit}), calls


@pytest.mark.parametrize("budget", [3, 6, 15])
def test_budget_trips_before_step_limit_through_real_graph(seeded, budget):
    """复审 R1：distinct 入参的真图里，**预算必须比步数上限先触发**。

    回归背景：`post_model_hook` 是图上的独立节点，一轮工具循环从 2 超步变成 3 超步；
    公式仍按 2 算时，budget=15 会在第 11 次调用就撞 `GraphRecursionError`，
    用户看到的错误码从 `TOOL_BUDGET_EXCEEDED` 变成 `AGENT_STEP_LIMIT`，
    `manifest.max_tool_calls` 实际失效。
    """
    from app.agents.chat_agent import _recursion_limit_for

    agent, calls = _distinct_arg_agent(_recursion_limit_for(budget))
    with tool_guard.tool_guard_context(budget):
        with pytest.raises(tool_guard.ToolLoopError) as err:
            asyncio.run(agent.ainvoke({"messages": [HumanMessage("一直调工具")]}))

    exc = err.value
    assert exc.code == "TOOL_BUDGET_EXCEEDED", (exc.code, len(calls))
    assert exc.success_calls == budget, (exc.success_calls, budget)
    # 预算内的调用都真的执行了，且没有多调一次
    assert len(calls) == budget



def test_run_detail_exposes_guard_usage(seeded, monkeypatch):
    """复审 E4：`run_detail.guard` 是文档承诺过的字段，必须真的出现在答复里。

    正常轮次也要有（`max_calls/success_calls/failure_streak/business_errors/aborted`），
    否则「这轮用了几次预算」只能从日志里猜。
    """
    from langchain_core.messages import AIMessage

    from app.agents import chat_agent
    from app.agents.registry import get_registry

    class _Agent:
        async def ainvoke(self, payload):  # noqa: ANN001
            return {"messages": list(payload["messages"]) + [AIMessage(content="已完成。")]}

    async def _meta():
        return "口径", 1

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda *a, **k: _Agent())
    monkeypatch.setattr(chat_agent, "_metadata_text", _meta)

    user = AuthUser(username="worker1", role="user", department="男装")
    result = asyncio.run(chat_agent.run_chat(user, [], "你好"))
    guard = (result.get("run_detail") or {}).get("guard")
    assert isinstance(guard, dict), guard
    budget = get_registry().get("analyst").max_tool_calls
    assert guard["max_calls"] == budget
    assert guard["success_calls"] == 0
    assert guard["failure_streak"] == 0
    assert guard["business_errors"] == 0
    assert guard["aborted"] is False


