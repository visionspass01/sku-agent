"""R5 的四个小项：token 归因、中止轮 guard 快照、MCP 确定性错误、事实卡数字可读性。

四个点都不改变主流程，只是把「口径/可观测性」上的偏差补平：
1. 纠错重试/备用模型重试那几次调用的 token 早先被漏掉（成本偏乐观）→ 用回调口径补；
2. 中止轮（工具名非法/预算用尽）台账里 `tool_calls=0` → 统一挂 guard 快照；
3. MCP「未知工具/服务未注册」是确定性错误，重试无意义且会误开熔断 → 不重试、不计熔断；
4. 事实卡里 `693，700` 这种位置化数字 → 改成 `available=693；on_hand=700`。
"""

from __future__ import annotations

import asyncio

import pytest


# ---------------------------------------------------------------- 1) token 归因
def test_llm_usage_prefers_callback_and_reports_retry_calls(seeded, _settings):
    from app.agents import chat_agent
    from app.auth import AuthUser
    from app.memory.runtime import append_llm_call, turn_log_context

    body = {
        "reply": "可用库存 1 件。",
        "tool_trace": [],
        "metadata_version": 1,
        "plan": None,
        # 只有"最终消息"里的那一次调用（模拟纠错重试后只剩成功那次）
        "llm_usage": {"calls": 1, "input_tokens": 100, "output_tokens": 10},
        "agent": {"id": "analyst", "prompt": "chat_system"},
    }
    user = AuthUser(username="admin", role="admin", department=None)
    usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "errors": 0}

    with turn_log_context("t_r5", "s_r5"):
        append_llm_call({"index": 1, "elapsed_ms": 5, "kind": "tool_call",
                         "input_tokens": 120, "output_tokens": 8})
        append_llm_call({"index": 2, "elapsed_ms": 7, "kind": "answer",
                         "input_tokens": 130, "output_tokens": 20})
        detail = chat_agent._build_run_detail("t_r5", body, usage, user)

    llm = detail["llm"]
    assert llm["token_source"] == "callback"
    assert llm["input_tokens"] == 250        # 回调口径：含那次没进消息的调用
    assert llm["output_tokens"] == 28
    assert llm["retry_calls"] == 1           # 2 次调用 vs 1 次进消息


def test_llm_usage_falls_back_to_messages_without_callback_tokens(seeded, _settings):
    from app.agents import chat_agent
    from app.auth import AuthUser
    from app.memory.runtime import append_llm_call, turn_log_context

    body = {"reply": "x", "tool_trace": [], "metadata_version": 1, "plan": None,
            "llm_usage": {"calls": 1, "input_tokens": 100, "output_tokens": 10},
            "agent": {"id": "analyst", "prompt": "chat_system"}}
    user = AuthUser(username="admin", role="admin", department=None)

    with turn_log_context("t_r5b", "s_r5"):
        # 回调拿不到 token（某些上游不回 usage）→ 回退消息口径，并标注来源
        append_llm_call({"index": 1, "elapsed_ms": 5, "kind": "answer"})
        detail = chat_agent._build_run_detail(
            "t_r5b", body, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "errors": 0}, user)

    llm = detail["llm"]
    assert llm["token_source"] == "messages"
    assert llm["input_tokens"] == 100
    assert llm["calls"] == 1


# ---------------------------------------------------------------- 2) 中止轮 guard 快照
def test_aborted_turn_carries_guard_snapshot(seeded, monkeypatch, _settings):
    """工具名非法的中止轮也要带 guard 快照（否则台账里工具数是 0）。"""
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from app.agents import chat_agent
    from app.agents.tool_guard import InvalidToolCallError
    from app.auth import AuthUser

    class _BadNameModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "bad-name"

        def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
            return self

        def bind(self, **kwargs):  # noqa: ANN003
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
            msg = AIMessage(content="", tool_calls=[{
                "name": "no_such_tool", "args": {}, "id": "c1", "type": "tool_call",
            }])
            return ChatResult(generations=[ChatGeneration(message=msg)])

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
            return self._generate(messages, stop, run_manager, **kwargs)

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(chat_agent, "ChatOpenAI", lambda *a, **k: _BadNameModel())

    async def _meta():
        return "口径", 1

    monkeypatch.setattr(chat_agent, "_metadata_text", _meta)
    user = AuthUser(username="admin", role="admin", department=None)

    with pytest.raises(InvalidToolCallError) as err:
        asyncio.run(chat_agent.run_chat(user, [], "查库存"))

    guard = getattr(err.value, "guard", None)
    assert isinstance(guard, dict) and guard, "中止轮必须带 guard 快照"
    assert guard["max_calls"] >= 1
    assert guard["success_calls"] == 0        # 坏调用没有真的执行工具


# ---------------------------------------------------------------- 3) MCP 确定性错误
def test_direct_mcp_client_marks_unknown_tool_as_deterministic():
    from app.mcp_client.client import DirectMCPClient, McpCallError

    client = DirectMCPClient("order")
    with pytest.raises(McpCallError) as err:
        asyncio.run(client.call("no_such_tool", {}))
    assert err.value.retryable is False


def test_hub_does_not_retry_or_trip_breaker_on_deterministic_errors(_settings):
    from app.mcp_client.client import McpCallError, MCPHub
    from app.resilience import Breaker

    hub = MCPHub()
    attempts = {"n": 0}

    class _Client:
        pending = 0

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def call(self, tool, arguments):  # noqa: ANN001, ANN002
            attempts["n"] += 1
            exc = McpCallError("未知工具: order.no_such_tool")
            exc.retryable = False
            raise exc

    hub._clients = {"order": _Client()}
    hub._breakers = {"order": Breaker(threshold=2, reset_seconds=30)}
    hub._started = True

    with pytest.raises(McpCallError) as err:
        asyncio.run(hub.call("order", "no_such_tool", {}, use_cache=False))

    assert attempts["n"] == 1, "确定性错误不该重试"
    assert "未重试" in str(err.value)
    assert hub._breakers["order"].failures == 0, "工具名写错不该把服务端判成不健康"


# ---------------------------------------------------------------- 4) 事实卡数字可读性
def test_facts_card_renders_named_pairs():
    from app.memory.session_memory import build_facts_card

    envelope = (
        '{"available": 693, "on_hand": 700, '
        '"summary": "get_inventory：available=693；on_hand=700", '
        '"result_id": "res_abc1234567"}'
    )
    card, _trigger = build_facts_card([
        {"role": "user", "content": "ST-01-C1-M 库存多少", "tool_trace": []},
        {"role": "assistant", "content": "可用库存 693 件（res_abc1234567）。",
         "tool_trace": [{"type": "result", "tool": "get_inventory", "result": envelope}]},
    ])

    assert "available=693" in card and "on_hand=700" in card
    # 旧的"位置化数字"（`693，700`）不再出现
    assert "] 693，" not in card


def test_facts_card_without_summary_falls_back_to_text():
    from app.memory.session_memory import build_facts_card

    envelope = '{"available": 693, "result_id": "res_abc1234567"}'
    card, _ = build_facts_card([
        {"role": "assistant", "content": "可用库存 693 件（res_abc1234567）。",
         "tool_trace": [{"type": "result", "tool": "get_inventory", "result": envelope}]},
    ])
    assert "[get_inventory]" in card and "res_abc1234567" in card
