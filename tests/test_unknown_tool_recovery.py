"""模型幻觉工具名（复审 A3）：可读错误 → 一次纠错恢复 → 仍失败则 422 + fatal。

背景：Multi-Agent 下工具集按 Agent 裁剪（只读 Agent 没有写工具），模型点到"别的 Agent
里有、当前没有"的工具名是最现实的幻觉形态。LangGraph 的 ToolNode 对未知工具抛的是
**空消息** `RuntimeError`：前端只看到 `RuntimeError: `、拿不到错误码，还会因为"非 fatal"
自动降级 POST 把同一轮再跑一遍。
现在：模型产出之后先用 `post_model_hook` 校验工具名（白名单取自实际装配的工具），
抛出可读的 `InvalidToolCallError`；`_invoke_agent` 先做一次"把可用工具名写进提示"的
纠错重试，仍失败才上抛（路由层转 422 + `X-Error-Code` + fatal，前端不自动重发）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents.tool_guard import InvalidToolCallError, validate_tool_calls

SKU = "ST-92-C1-M"
FINAL = "可用库存 510 件。"


def _bad_call(call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": "get_inventory_v2", "args": {"sku_id": SKU}, "id": call_id,
        "type": "tool_call",
    }])


def _good_call(call_id: str = "c2") -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": "get_inventory", "args": {"sku_id": SKU}, "id": call_id,
        "type": "tool_call",
    }])


class _ScriptedModel(BaseChatModel):
    """按顺序返回脚本消息；**游标共享**，所以纠错重试重建 agent 后脚本继续往下走。"""

    responses: Any = None      # list[BaseMessage]
    cursor: Any = None         # list[int]（长度 1 的可变游标）

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

    def bind(self, **kwargs):  # noqa: ANN003
        return self

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None,  # noqa: ANN001
                  **kwargs) -> ChatResult:
        index = self.cursor[0]
        assert index < len(self.responses), "脚本消息用完：被调用的次数超出预期"
        self.cursor[0] = index + 1
        return ChatResult(generations=[ChatGeneration(message=self.responses[index])])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        return self._generate(messages, stop, run_manager, **kwargs)


# ---------------------------------------------------------------- hook 单元
def test_hook_rejects_unknown_tool_name():
    hook = validate_tool_calls({"get_inventory"})
    with pytest.raises(InvalidToolCallError) as err:
        hook({"messages": [HumanMessage("查库存"), _bad_call()]})

    exc = err.value
    assert exc.bad_tools == ["get_inventory_v2"]
    assert "get_inventory" in exc.allowed_tools
    assert exc.code == "AGENT_INVALID_TOOL_NAME"
    assert exc.retryable is False
    assert "get_inventory_v2" in str(exc) and "AGENT_INVALID_TOOL_NAME" in str(exc)


def test_hook_passes_valid_calls_and_plain_answers():
    hook = validate_tool_calls({"get_inventory"})
    assert hook({"messages": [_good_call()]}) is None
    assert hook({"messages": [AIMessage(content="直接回答")]}) is None
    assert hook({"messages": []}) is None


# ---------------------------------------------------------------- 参数非法（复审 R2）
def _bad_args_call(call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", invalid_tool_calls=[{
        "name": "get_inventory", "args": '{"sku_id": ', "id": call_id, "error": "bad json",
    }])


def test_hook_rejects_invalid_tool_arguments():
    """参数 JSON 坏了必须被拦下（否则图直接结束、用户只看到一句兜底文案）。"""
    hook = validate_tool_calls({"get_inventory"})
    with pytest.raises(InvalidToolCallError) as err:
        hook({"messages": [_bad_args_call()]})

    exc = err.value
    assert exc.code == "AGENT_INVALID_TOOL_ARGS"
    assert exc.reason == "invalid_args"
    assert exc.retryable is False
    assert "get_inventory" in str(exc)
    assert "JSON" in str(exc)


def test_agent_recovers_from_invalid_arguments(seeded, monkeypatch, _settings):
    """坏参数 → 纠错重试（要求合法 JSON）→ 第二轮直接作答。"""
    from app.agents import chat_agent
    from app.auth import AuthUser

    _patch(monkeypatch, _settings, [_bad_args_call(), AIMessage(content=FINAL)])
    result = asyncio.run(chat_agent.run_chat(
        AuthUser(username="admin", role="admin", department=None), [], f"{SKU} 库存？"))

    assert result["reply"] == FINAL
    assert result["run_detail"]["agent"]["invalid_tool_retries"] == 1
    assert result["run_detail"]["steps"] == []


def test_invalid_arguments_error_is_fatal_and_coded(seeded, monkeypatch, _settings):
    """两次都坏 → 抛可读异常，路由层能翻译成 422 + AGENT_INVALID_TOOL_ARGS。"""
    from app.agents import chat_agent
    from app.api.chat_routes import _agent_failure
    from app.auth import AuthUser

    _patch(monkeypatch, _settings, [_bad_args_call("c1"), _bad_args_call("c2")])
    with pytest.raises(InvalidToolCallError) as err:
        asyncio.run(chat_agent.run_chat(
            AuthUser(username="admin", role="admin", department=None), [], f"{SKU} 库存？"))

    failure = _agent_failure(err.value) or {}
    assert failure.get("status") == 422
    assert failure.get("error_code") == "AGENT_INVALID_TOOL_ARGS"
    assert failure.get("fatal") is True
    assert "JSON" in failure.get("detail", "")


def test_correction_text_differs_for_arguments_case():
    """纠错语要区分「工具名错」与「参数错」，否则模型不知道该改什么。"""
    from app.agents.chat_agent import _tool_name_correction
    from app.agents.tool_guard import invalid_tool_args_error

    text = _tool_name_correction(invalid_tool_args_error(["get_inventory"], ["get_inventory"]))
    assert "JSON" in text and "参数" in text
    assert "不存在的工具" not in text


# ---------------------------------------------------------------- 端到端
def _patch(monkeypatch, _settings, responses):
    """把 ChatOpenAI 换成脚本模型；游标共享，重建 agent 时脚本继续往下走。"""
    from app.agents import chat_agent

    script = list(responses)
    cursor = [0]
    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(chat_agent, "ChatOpenAI",
                        lambda *a, **k: _ScriptedModel(responses=script, cursor=cursor))


def test_agent_recovers_after_one_correction(seeded, monkeypatch, _settings):
    """第一次点错工具名 → 纠错重试 → 第二轮正常作答（本轮不失败）。"""
    from app.agents import chat_agent
    from app.auth import AuthUser

    _patch(monkeypatch, _settings, [_bad_call(), AIMessage(content=FINAL)])
    result = asyncio.run(chat_agent.run_chat(
        AuthUser(username="admin", role="admin", department=None), [], f"{SKU} 库存？"))

    assert result["reply"] == FINAL
    assert result["run_detail"]["agent"]["invalid_tool_retries"] == 1
    assert result["run_detail"]["steps"] == []      # 坏调用没有真的打到工具


def test_agent_raises_after_correction_exhausted(seeded, monkeypatch, _settings):
    """纠错后仍然点错 → 抛可读异常（不是空消息 RuntimeError）。"""
    from app.agents import chat_agent
    from app.auth import AuthUser

    _patch(monkeypatch, _settings, [_bad_call("c1"), _bad_call("c2")])
    with pytest.raises(InvalidToolCallError) as err:
        asyncio.run(chat_agent.run_chat(
            AuthUser(username="admin", role="admin", department=None), [], f"{SKU} 库存？"))

    assert err.value.bad_tools == ["get_inventory_v2"]
    assert "可用工具" in str(err.value)


def test_correction_text_lists_available_tools():
    """纠错提示必须写明可用工具名，否则模型还是会瞎猜。"""
    from app.agents.chat_agent import _tool_name_correction

    exc = InvalidToolCallError(["no_such_tool"], ["get_inventory", "get_sales"])
    text = _tool_name_correction(exc)
    assert "no_such_tool" in text
    assert "get_inventory" in text and "get_sales" in text
    assert "不要编造工具名" in text


def test_hook_trips_through_real_graph(seeded, monkeypatch, _settings):
    """走真 `create_react_agent`：hook 在模型之后、工具之前生效。"""
    from app.agents import chat_agent
    from app.auth import AuthUser

    _patch(monkeypatch, _settings, [_bad_call()])
    # 只构建图（不跑整轮），再手动 invoke 一次，确认 hole 抛出的是我们的异常类型
    agent = chat_agent.build_chat_agent(
        AuthUser(username="admin", role="admin", department=None), "系统提示")
    with pytest.raises(InvalidToolCallError):
        asyncio.run(agent.ainvoke({"messages": [HumanMessage("查库存")]}))


def test_valid_tool_call_still_runs_through_real_graph(seeded, monkeypatch, _settings):
    """对照组：合法工具名不被误拦，真的执行了工具（hook 不能把正常调用挡掉）。"""
    from app.agents import chat_agent
    from app.auth import AuthUser

    calls = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={
            "sku_id": SKU, "on_hand": 600, "locked": 30, "reserved_crm": 40,
            "order_hold": 20, "available": 510, "warehouse_id": "WH-HZ",
            "as_of": "2026-11-03", "cached": False,
        })

    from app.tools import platform_tools

    platform_tools.set_platform_client(platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(_handler)))
    try:
        _patch(monkeypatch, _settings, [_good_call(), AIMessage(content=FINAL)])
        result = asyncio.run(chat_agent.run_chat(
            AuthUser(username="admin", role="admin", department=None), [], f"{SKU} 库存？"))
    finally:
        platform_tools.set_platform_client(None)

    assert result["reply"] == FINAL
    assert result["run_detail"]["agent"]["invalid_tool_retries"] == 0
    assert any(path.endswith("/inventory") for path in calls)
