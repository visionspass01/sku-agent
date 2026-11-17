"""真·token 流式输出：run_chat_stream 通过 SSE delta 事件推送答案片段。"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, AIMessageChunk


class _Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, payload: dict) -> None:
        self.events.append((event, dict(payload)))

    def texts(self, event: str = "delta") -> str:
        return "".join(str(p.get("text") or "") for e, p in self.events if e == event)


class _FakeStreamAgent:
    """模拟 LangGraph create_react_agent 的 astream 双模式输出。

    `msg_id` 让 chunk 带上 AI 消息 id——复审 A4 的「撤回」是按消息 id 判定的：
    同一条消息先出文本、后出工具调用，说明那段文本是过程而不是最终答案。
    """

    def __init__(self, final_text: str, chunks: list[str] | None = None,
                 planning: list[str] | None = None, msg_id: str = "m1",
                 tool_call_after_text: bool = False) -> None:
        self.final_text = final_text
        self.chunks = chunks if chunks is not None else [final_text[:5], final_text[5:]]
        self.planning = planning or []
        self.msg_id = msg_id
        self.tool_call_after_text = tool_call_after_text

    async def astream(self, payload, stream_mode=None):
        for text in self.planning:
            yield "messages", (
                AIMessageChunk(id=self.msg_id, content=text, tool_call_chunks=[{
                    "name": "get_inventory", "args": '{"sku_id": "ST-01-C1-M"}', "index": 0,
                }]),
                {"langgraph_node": "agent"},
            )
        for text in self.chunks:
            yield "messages", (AIMessageChunk(id=self.msg_id, content=text),
                               {"langgraph_node": "agent"})
        if self.tool_call_after_text:
            # 同一条消息继续产出工具调用：前面那段文本不是最终答案
            yield "messages", (
                AIMessageChunk(id=self.msg_id, content="", tool_call_chunks=[{
                    "name": "get_inventory", "args": '{"sku_id": "ST-01-C1-M"}', "index": 0,
                }]),
                {"langgraph_node": "agent"},
            )
        yield "values", {"messages": list(payload["messages"]) + [AIMessage(content=self.final_text)]}


def _run_stream(agent, sink: _Sink, monkeypatch):
    from app.agents import chat_agent
    from app.auth import AuthUser
    from app.memory.runtime import event_sink_context

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda user, prompt, *a, **k: agent)
    user = AuthUser(username="admin", role="admin", department=None)

    async def _go():
        with event_sink_context(sink):
            return await chat_agent.run_chat_stream(user, [{"role": "user", "content": "hi"}], "查库存")

    return asyncio.run(_go())


def test_stream_emits_delta_tokens(seeded, monkeypatch):
    sink = _Sink()
    agent = _FakeStreamAgent("可用库存 693 件（res_aaa111bbb2）")
    result = _run_stream(agent, sink, monkeypatch)

    assert result["streamed"] is True
    assert sink.texts() == "可用库存 693 件（res_aaa111bbb2）"
    # 片段们拼起来就是最终答复（前端先把气泡写出来，done 时用最终答复覆盖）
    assert result["reply"] == sink.texts()
    assert result["run_detail"]


def test_stream_drops_tool_planning_tokens(seeded, monkeypatch):
    """「决定调工具」那几步的文本是过程，不该当成答案推给用户。"""
    sink = _Sink()
    agent = _FakeStreamAgent(
        "可用库存 693 件（res_aaa111bbb2）",
        planning=["我需要先查一下库存", "调用 get_inventory"],
    )
    result = _run_stream(agent, sink, monkeypatch)
    text = sink.texts()
    assert "我需要先查一下库存" not in text
    assert "调用 get_inventory" not in text
    assert text == "可用库存 693 件（res_aaa111bbb2）"
    assert result["reply"] == text


def test_stream_falls_back_when_no_deltas(seeded, monkeypatch):
    """模型不支持增量（一次吐完）时，delta 只有一个，但流程照常。"""
    sink = _Sink()
    agent = _FakeStreamAgent("可用库存 693 件（res_aaa111bbb2）", chunks=["可用库存 693 件（res_aaa111bbb2）"])
    result = _run_stream(agent, sink, monkeypatch)
    assert result["reply"] == "可用库存 693 件（res_aaa111bbb2）"
    assert len([e for e, _ in sink.events if e == "delta"]) == 1


def test_stream_retracts_prose_when_same_message_turns_into_tool_call(seeded, monkeypatch):
    """复审 A4：模型「先说话、后调工具」时，那段话不是最终答案 → 服务端发 delta_retract。"""
    sink = _Sink()
    agent = _FakeStreamAgent(
        "可用库存 693 件（res_aaa111bbb2）",
        chunks=["我先查一下库存", "马上给你结论"],
        tool_call_after_text=True,
    )
    _run_stream(agent, sink, monkeypatch)

    events = [e for e, _ in sink.events]
    assert "delta_retract" in events
    assert events.count("delta_retract") == 1
    # 撤回发生在过程文字之后（顺序：delta → delta_retract）
    first_delta = events.index("delta")
    retract = events.index("delta_retract")
    assert first_delta < retract
    retract_payload = next(p for e, p in sink.events if e == "delta_retract")
    assert retract_payload["reason"] == "tool_call"
    assert retract_payload["chars"] == len("我先查一下库存马上给你结论")


def test_stream_does_not_retract_when_no_tool_call_follows(seeded, monkeypatch):
    """纯文本答复（没有工具调用跟在后面）不能撤：那是用户要看的答案。"""
    sink = _Sink()
    agent = _FakeStreamAgent("可用库存 693 件（res_aaa111bbb2）")
    _run_stream(agent, sink, monkeypatch)
    assert "delta_retract" not in [e for e, _ in sink.events]


def test_non_stream_path_unaffected(seeded, monkeypatch):
    """run_chat（POST /api/chat）仍走 ainvoke，不发 delta。"""
    from langchain_core.messages import AIMessage

    from app.agents import chat_agent
    from app.auth import AuthUser
    from app.memory.runtime import event_sink_context

    class _FakeInvokeAgent:
        async def ainvoke(self, payload):
            return {"messages": list(payload["messages"]) + [AIMessage(content="可用库存 693 件（res_aaa111bbb2）")]}

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda user, prompt, *a, **k: _FakeInvokeAgent())
    sink = _Sink()
    user = AuthUser(username="admin", role="admin", department=None)

    async def _go():
        with event_sink_context(sink):
            return await chat_agent.run_chat(user, [{"role": "user", "content": "hi"}], "查库存")

    result = asyncio.run(_go())
    assert result["streamed"] is False
    assert sink.texts() == ""
    assert result["reply"]
