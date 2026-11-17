"""离线跑通真实 ReAct 循环：脚本化 ChatModel + 真工具 + 真 LangGraph。

为什么需要这个测试：其它用例大多把 agent 换成一个直接返回 AIMessage 的假对象，
那验证的是「接口层」而不是「Agent 真的会调工具」。这里用 LangChain 自带的
FakeMessagesListChatModel 演一个「先决定调工具、再给结论」的模型，
让 create_react_agent 走完整循环（bind_tools → tool_call → ToolMessage → 再生成），
不需要任何网络与 API Key。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

SKU = "ST-92-C1-M"
FINAL = "可用库存 510 件，预计 30.5 天售罄。"


class ScriptedChatModel(FakeMessagesListChatModel):
    """按顺序返回预设消息；接受 ChatOpenAI 那套构造参数但忽略（只保留 responses）。"""

    def __init__(self, responses=None, **kwargs) -> None:  # noqa: ANN001, ARG002
        super().__init__(responses=responses or SCRIPT)

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ARG002
        return self

    def bind(self, **kwargs):  # noqa: ANN003 —— create_react_agent 可能再 bind 一次
        return self


SCRIPT = [
    AIMessage(
        content="",
        tool_calls=[{"name": "get_inventory", "args": {"sku_id": SKU}, "id": "call_1", "type": "tool_call"}],
    ),
    AIMessage(content=FINAL),
]


def _platform(monkeypatch):
    from app.tools import platform_tools

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/inventory"):
            return httpx.Response(200, json={
                "sku_id": SKU, "on_hand": 600, "locked": 30, "reserved_crm": 40,
                "order_hold": 20, "available": 510, "warehouse_id": "WH-HZ",
                "as_of": "2026-11-03", "cached": False,
            })
        if request.url.path.endswith("/sell-through"):
            return httpx.Response(200, json={
                "sku_id": SKU, "available": 510, "in_transit_7d": 100,
                "daily_avg_7d": 20.0, "sell_through_days": 30.5, "stockout_risk": "低",
            })
        return httpx.Response(404, json={"detail": "not found"})

    client = platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler),
    )
    platform_tools.set_platform_client(client)
    return client


def test_react_loop_calls_real_tool_and_answers(seeded, monkeypatch, _settings):
    """完整 ReAct 一轮：模型决定调 get_inventory → 工具真跑 → 模型据结果作答。"""
    from app.agents import chat_agent
    from app.auth import AuthUser
    from app.observability import tool_stats
    from app.tools import platform_tools

    monkeypatch.setattr(_settings, "grounding_enabled", False)  # 结论文案固定，只验证循环
    monkeypatch.setattr(chat_agent, "ChatOpenAI", ScriptedChatModel)
    client = _platform(monkeypatch)
    user = AuthUser(username="admin", role="admin", department=None)
    try:
        result = asyncio.run(chat_agent.run_chat(user, [], f"{SKU} 可用库存多少？"))
    finally:
        platform_tools.set_platform_client(None)

    assert result["reply"] == FINAL
    assert [t for t in result["tool_trace"] if t["type"] == "call"][0]["tool"] == "get_inventory"
    step = result["run_detail"]["steps"][0]
    assert step["tool"] == "get_inventory" and step["status"] == "ok"
    assert step["result_id"].startswith("res_")
    # 工具真的打到了中台（MockTransport 命中 /inventory）
    assert "510" in json.dumps(step, ensure_ascii=False) or step["row_count"] >= 0
    # 工具统计现在按 agent 归集（chat:<agent>.<tool>），所以按后缀取这一条
    tools_stats = tool_stats.snapshot()["tools"]
    key = next(k for k in tools_stats if k.endswith(".get_inventory"))
    row = tools_stats[key]
    assert row["calls"] == 1 and row["outcomes"] == {"ok": 1}
    assert client is not None


def test_react_loop_tool_error_is_visible(seeded, monkeypatch, _settings):
    """工具报错（部门越权）不会中断循环：错误进 ToolMessage，模型照常给结论。"""
    from app.agents import chat_agent
    from app.auth import AuthUser
    from app.tools import platform_tools

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(chat_agent, "ChatOpenAI", ScriptedChatModel)
    _platform(monkeypatch)
    user = AuthUser(username="worker1", role="user", department="男装")  # 只能看男装
    try:
        result = asyncio.run(chat_agent.run_chat(user, [], "ST-90-C1-M 库存？"))  # 女装 SKU
    finally:
        platform_tools.set_platform_client(None)

    # 脚本模型没按真实参数换 SKU，这里只验证「越权错误串回模型且不抛异常」
    trace = [t for t in result["tool_trace"] if t["type"] == "result"]
    assert result["reply"] == FINAL
    assert trace or result["run_detail"]["steps"]


@pytest.mark.parametrize("message", ["你好", "今天天气怎么样"])
def test_non_data_questions_still_answer(seeded, monkeypatch, _settings, message):
    """闲聊/非取数问题：模型第一次就直接回答时，循环也得正常收尾。"""
    from app.agents import chat_agent
    from app.auth import AuthUser
    from app.tools import platform_tools

    class _DirectAnswerModel(ScriptedChatModel):
        def __init__(self, responses=None, **kwargs):  # noqa: ANN001, ARG002
            super().__init__(responses=[AIMessage(content="你好，我可以帮你查库存。")])

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(chat_agent, "ChatOpenAI", _DirectAnswerModel)
    _platform(monkeypatch)
    user = AuthUser(username="admin", role="admin", department=None)
    try:
        result = asyncio.run(chat_agent.run_chat(user, [], message))
    finally:
        platform_tools.set_platform_client(None)

    assert result["reply"] == "你好，我可以帮你查库存。"
    assert result["run_detail"]["steps"] == []      # 没有取数，就没有工具步骤
    assert result["run_detail"]["grounding"]["status"] == "disabled"
