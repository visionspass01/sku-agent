"""真图流式端到端（评审 E1）：真 create_react_agent + 真 run_chat_stream + 脚本化流式模型。

为什么需要：`tests/test_chat_stream_tokens.py` 把 `build_chat_agent` 整个换成替身，
验证的是"接口层"；真图的 `astream(stream_mode=["messages","values"])` 双模式合并
（token 增量 + 最终 state）、工具消息过滤、以及"先说一句再调工具"的撤回
从来没在**真图**上跑过。这里补上，且仍然零网络。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

SKU = "ST-91-C1-M"
ANSWER = "可用库存 510 件。"


class _ChunkScriptModel(BaseChatModel):
    """按"步"产出流式 chunk；游标共享，便于多次构建 agent 时继续。"""

    steps: Any = None       # list[list[dict]]
    cursor: Any = None      # list[int]

    @property
    def _llm_type(self) -> str:
        return "chunk-script"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

    def bind(self, **kwargs):  # noqa: ANN003
        return self

    def _take(self) -> list[dict]:
        index = self.cursor[0]
        assert index < len(self.steps), "脚本步数用完：模型调用次数超出预期"
        self.cursor[0] = index + 1
        return self.steps[index]

    def _stream(self, messages: list[BaseMessage], stop=None, run_manager=None,  # noqa: ANN001
                **kwargs):
        for spec in self._take():
            yield ChatGenerationChunk(message=AIMessageChunk(
                content=spec.get("text", ""),
                id=spec.get("id") or "m",
                tool_call_chunks=spec.get("tool_call_chunks") or [],
            ))

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        for chunk in self._stream(messages, stop, run_manager, **kwargs):
            yield chunk

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None,  # noqa: ANN001
                  **kwargs) -> ChatResult:
        chunks = self._take()
        text = "".join(spec.get("text", "") for spec in chunks)
        calls = [spec["tool_calls"] for spec in chunks if spec.get("tool_calls")]
        message = AIMessage(content=text, tool_calls=[c for group in calls for c in group])
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        return self._generate(messages, stop, run_manager, **kwargs)


def _tool_call_spec(call_id: str = "call_1", msg_id: str = "m1") -> dict:
    return {
        "id": msg_id,
        "tool_call_chunks": [{
            "name": "get_inventory", "args": f'{{"sku_id": "{SKU}"}}',
            "index": 0, "id": call_id, "type": "tool_call_chunk",
        }],
        "tool_calls": [{"name": "get_inventory", "args": {"sku_id": SKU},
                        "id": call_id, "type": "tool_call"}],
    }


def _platform(monkeypatch):
    """中台桩：只回 /inventory（工具真的会跑到）。"""
    from app.tools import platform_tools

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/inventory"):
            return httpx.Response(200, json={
                "sku_id": SKU, "on_hand": 600, "locked": 30, "reserved_crm": 40,
                "order_hold": 20, "available": 510, "warehouse_id": "WH-HZ",
                "as_of": "2026-11-03", "cached": False,
            })
        return httpx.Response(404, json={"detail": "not found"})

    platform_tools.set_platform_client(platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler)))


@pytest.fixture()
def sink():
    class _Sink:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        def emit(self, event: str, payload: dict) -> None:
            self.events.append((event, dict(payload)))

        def names(self) -> list[str]:
            return [name for name, _ in self.events]

        def texts(self) -> str:
            return "".join(str(p.get("text") or "") for n, p in self.events if n == "delta")

    yield _Sink()
    from app.tools import platform_tools

    platform_tools.set_platform_client(None)


def _run(monkeypatch, _settings, sink, steps):
    from app.agents import chat_agent
    from app.auth import AuthUser
    from app.memory.runtime import event_sink_context
    from app.tools import platform_tools

    cursor = [0]
    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(chat_agent, "ChatOpenAI",
                        lambda *a, **k: _ChunkScriptModel(steps=steps, cursor=cursor))
    _platform(monkeypatch)
    assert platform_tools.get_platform_client() is not None   # mock 中台已装配
    user = AuthUser(username="admin", role="admin", department=None)

    async def _go():
        with event_sink_context(sink):
            return await chat_agent.run_chat_stream(user, [], f"{SKU} 可用库存多少")

    return asyncio_run(_go())


def asyncio_run(coro):  # noqa: ANN001 —— 单测里统一入口，便于断言前收尾
    import asyncio

    return asyncio.run(coro)


def test_stream_real_graph_emits_deltas(seeded, monkeypatch, _settings, sink):
    """真图 + 流式模型：答案 token 分片推送，最终 reply 与推送内容一致。"""
    result = _run(monkeypatch, _settings, sink, [
        [{"text": "可用库存 ", "id": "m1"}, {"text": "510 件。", "id": "m1"}],
    ])

    assert result["streamed"] is True
    assert sink.texts() == "可用库存 510 件。"
    assert result["reply"] == sink.texts()
    assert result["run_detail"]["steps"] == []          # 没调工具
    assert "delta_retract" not in sink.names()


def test_stream_real_graph_tool_then_answer(seeded, monkeypatch, _settings, sink):
    """真图里跑一次工具调用：工具消息不推给用户，工具步骤进 run_detail。"""
    result = _run(monkeypatch, _settings, sink, [
        [_tool_call_spec()],
        [{"text": ANSWER, "id": "m2"}],
    ])

    assert result["reply"] == ANSWER
    assert sink.texts() == ANSWER                       # 只有最终答复被推给用户
    steps = result["run_detail"]["steps"]
    assert [s["tool"] for s in steps] == ["get_inventory"]
    assert steps[0]["status"] == "ok"
    assert steps[0]["result_id"].startswith("res_")


def test_stream_real_graph_retracts_prose_when_tool_call_follows(seeded, monkeypatch, _settings, sink):
    """同一消息先出文本、再出工具调用：服务端发 delta_retract，用户只看到最终答案。"""
    result = _run(monkeypatch, _settings, sink, [
        [{"text": "我先查一下库存", "id": "m1"},
         {"text": "，稍等", "id": "m1"},
         _tool_call_spec(msg_id="m1")],
        [{"text": ANSWER, "id": "m2"}],
    ])

    names = sink.names()
    assert "delta_retract" in names, names
    retract = next(p for n, p in sink.events if n == "delta_retract")
    assert retract["reason"] == "tool_call"
    assert retract["chars"] == len("我先查一下库存，稍等")
    # 撤回之后只推最终答案
    assert sink.texts().endswith(ANSWER)
    assert result["reply"] == ANSWER
    assert [s["tool"] for s in result["run_detail"]["steps"]] == ["get_inventory"]


def test_stream_real_graph_marks_unverified_when_grounding_flags(seeded, monkeypatch, _settings, sink):
    """出口闸门与流式的张力：先推初稿、后被判 degraded 时要显式标注。"""
    from app.agents import chat_agent
    from app.auth import AuthUser
    from app.memory.runtime import event_sink_context

    async def _fake_grounding(reply, *, steps=None, tool_trace=None):  # noqa: ANN001
        return {"reply": reply + "\n\n> ⚠️ 自检未通过", "report": {"status": "degraded"}}

    monkeypatch.setattr("app.agents.grounding.ground_reply", _fake_grounding)
    monkeypatch.setattr(_settings, "grounding_enabled", True)
    monkeypatch.setattr(chat_agent, "ChatOpenAI", lambda *a, **k: _ChunkScriptModel(
        steps=[[{"text": ANSWER, "id": "m1"}]], cursor=[0]))
    _platform(monkeypatch)
    user = AuthUser(username="admin", role="admin", department=None)

    async def _go():
        with event_sink_context(sink):
            return await chat_agent.run_chat_stream(user, [], f"{SKU} 可用库存多少")

    result = asyncio_run(_go())
    assert result["streamed"] is True
    assert result["streamed_unverified"] is True
    assert result["run_detail"]["grounding"]["status"] == "degraded"
    assert result["run_detail"]["grounding"]["streamed_before_check"] is True
