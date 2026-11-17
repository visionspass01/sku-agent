"""队列重投/死信（E1）与流式诚实性（D3）。"""

from __future__ import annotations

import asyncio
import uuid

import pytest


# ---------------------------------------------------------------- E1 重投 / 死信
def test_unacked_message_is_reclaimable():
    """进程崩溃/异常退出留下的 PEL 消息必须能被捞回来，否则「SKU 悄悄少跑」。"""
    from app.cache.stream_queue import get_stream_queue

    queue = get_stream_queue()
    msg_id, _ = queue.enqueue({"sku_id": "SKU-R"})
    batch = queue.consume(count=10)
    assert [m for m, _ in batch] == [msg_id]

    reclaimed, dead = queue.claim_stale(min_idle_ms=0)   # 故意不 ack 就回收
    assert [m for m, _ in reclaimed] == [msg_id]
    assert [t["sku_id"] for _, t in reclaimed] == ["SKU-R"]
    assert dead == 0

    assert queue.ack(msg_id) == 1
    assert queue.pending_count() == 0


def test_message_over_retry_limit_goes_to_dead_letter(_settings, monkeypatch):
    from app.cache.stream_queue import get_stream_queue

    monkeypatch.setattr(_settings, "queue_max_deliveries", 1)
    queue = get_stream_queue()
    msg_id, _ = queue.enqueue({"sku_id": "SKU-D"})
    queue.consume(count=10)                               # 第 1 次投递

    reclaimed, dead = queue.claim_stale(min_idle_ms=0)    # 第 2 次 > 上限 → 死信
    assert reclaimed == []
    assert dead == 1
    assert msg_id in queue.dead_letters()
    assert queue.dead_letters()[msg_id]["task"]["sku_id"] == "SKU-D"
    assert queue.pending_count() == 0                     # 已 ack，不会无限重投
    assert queue.stats()["dead"] == 1


def test_claimed_message_is_processed_and_acked():
    from app.cache.stream_queue import get_stream_queue

    queue = get_stream_queue()
    msg_id, _ = queue.enqueue({"sku_id": "SKU-C"})
    queue.consume(count=10)
    reclaimed, dead = queue.claim_stale(min_idle_ms=0)
    assert len(reclaimed) == 1 and dead == 0
    assert queue.ack(reclaimed[0][0]) == 1
    assert queue.pending_count() == 0
    assert msg_id == reclaimed[0][0]


def test_claim_stale_with_empty_pel():
    from app.cache.stream_queue import get_stream_queue

    queue = get_stream_queue()
    assert queue.claim_stale(min_idle_ms=0) == ([], 0)


# ---------------------------------------------------------------- D3 先落库
def _user():
    from app.auth import AuthUser

    return AuthUser(username=f"persist_{uuid.uuid4().hex[:8]}", role="admin", department=None)


def _session(user):
    from app.agents import chat_store
    from app.db.engine import session_scope

    with session_scope() as session:
        return chat_store.create_session(session, user.username).id


def _messages(session_id):
    """返回纯 dict（ORM 实例离开 session 后会 Detached）。"""
    from app.agents import chat_store
    from app.db.engine import session_scope

    with session_scope() as session:
        return [{"role": m.role, "content": m.content}
                for m in chat_store.list_messages(session, session_id)]


def test_user_message_survives_failed_turn(seeded, monkeypatch, _settings):
    """回归：用户这句话曾经在 run 成功之后才落库，模型失败/断连时它会凭空消失。"""
    from app.api import chat_routes

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    user = _user()
    sid = _session(user)

    async def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("upstream down")

    monkeypatch.setattr(chat_routes, "run_chat", _boom)
    req = chat_routes.ChatRequest(session_id=sid, message="这句话不能丢")

    with pytest.raises(RuntimeError):
        asyncio.run(chat_routes._execute_turn(user, req))

    msgs = _messages(sid)
    assert [m["role"] for m in msgs] == ["user"]
    assert msgs[0]["content"] == "这句话不能丢"


def test_successful_turn_stores_user_then_assistant(seeded, monkeypatch, _settings):
    from app.api import chat_routes

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    user = _user()
    sid = _session(user)

    async def _ok(chat_user, history, message, **kwargs):  # noqa: ANN001, ANN003
        return {"reply": "可用库存 1 件", "tool_trace": [], "metadata_version": None,
                "run_detail": None}

    monkeypatch.setattr(chat_routes, "run_chat", _ok)
    asyncio.run(chat_routes._execute_turn(
        user, chat_routes.ChatRequest(session_id=sid, message="查库存")))

    msgs = _messages(sid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "可用库存 1 件"


def test_history_does_not_include_current_message(seeded, monkeypatch, _settings):
    """先落库的副作用要挡住：history 必须在落库之前取，否则本轮问题会被注入两次。"""
    from app.api import chat_routes

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    user = _user()
    sid = _session(user)
    captured: list[list[dict]] = []

    async def _ok(chat_user, history, message, **kwargs):  # noqa: ANN001, ANN003
        captured.append(list(history))
        return {"reply": f"回：{message}", "tool_trace": [], "metadata_version": None,
                "run_detail": None}

    monkeypatch.setattr(chat_routes, "run_chat", _ok)
    asyncio.run(chat_routes._execute_turn(
        user, chat_routes.ChatRequest(session_id=sid, message="第一句")))
    asyncio.run(chat_routes._execute_turn(
        user, chat_routes.ChatRequest(session_id=sid, message="第二句")))

    assert captured[0] == []                       # 首轮没有历史
    roles = [m["role"] for m in captured[1]]
    assert roles == ["user", "assistant"]          # 只有上一轮，不含本轮"第二句"
    assert captured[1][0]["content"] == "第一句"


# ---------------------------------------------------------------- D3 流式未校验标注
def test_grounding_detail_flags_streamed_before_check():
    from app.agents.chat_agent import _grounding_detail

    repaired = _grounding_detail({"streamed_deltas": 3, "grounding": {"status": "repaired"}})
    assert repaired["streamed_before_check"] is True

    degraded = _grounding_detail({"streamed_deltas": 3, "grounding": {"status": "degraded"}})
    assert degraded["streamed_before_check"] is True

    passed = _grounding_detail({"streamed_deltas": 3, "grounding": {"status": "passed"}})
    assert passed["streamed_before_check"] is False

    not_streamed = _grounding_detail({"streamed_deltas": 0, "grounding": {"status": "repaired"}})
    assert not_streamed["streamed_before_check"] is False

    disabled = _grounding_detail({})
    assert disabled["status"] == "disabled" and disabled["streamed_before_check"] is False


def test_streamed_unverified_reaches_the_response(seeded, monkeypatch, _settings):
    """流式 + 闸门改写过答案 → 响应必须标记 streamed_unverified，前端据此提示。"""
    from langchain_core.messages import AIMessage

    from app.agents import chat_agent
    from app.memory.runtime import chat_turn_context

    monkeypatch.setattr(_settings, "grounding_enabled", True)

    class _StreamingAgent:
        async def astream(self, payload, stream_mode=None):  # noqa: ANN001
            yield "messages", (type("C", (), {"content": "可用库存 ", "tool_calls": None,
                                              "tool_call_chunks": None, "type": "ai"})(), {})
            yield "values", {"messages": list(payload["messages"]) + [
                AIMessage(content="可用库存 900 件")]}

    async def _repair(reply, *, steps=None, tool_trace=None):  # noqa: ANN001
        return {"reply": "可用库存 900 件（未取数修正）",
                "report": {"status": "repaired", "issues": ["未标注 result_id"]}}

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda *a, **k: _StreamingAgent())
    monkeypatch.setattr("app.agents.grounding.ground_reply", _repair)

    user = _user()
    with chat_turn_context(session_id=_session(user)):
        result = asyncio.run(chat_agent.run_chat_stream(user, [], "查库存"))

    assert result["streamed"] is True
    assert result["streamed_unverified"] is True
    assert result["run_detail"]["grounding"]["streamed_before_check"] is True
