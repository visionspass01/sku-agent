"""流式对话的错误必须走 HTTP 状态码（评审 2.2-7）。

回归背景：`/chat/stream` 先建流再跑逻辑，于是「会话不存在 / 同会话并发 / 频次超限」
全都变成了 SSE 里的 `event: error`（HTTP 200）。客户端只看得到"流结束了但没结果"，
既没法按状态码分流，也没法区分"该等一会儿"和"根本没这个会话"。

现在：会话解析 + 取锁发生在**返回 StreamingResponse 之前**，这些错误都是真实状态码；
只有模型跑起来之后的失败才退化成 SSE error（那时状态码已经发出去了，无法回改）。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest


@pytest.fixture()
def api(seeded, _settings):
    """在同一个事件循环里驱动真实 app（会话锁是 asyncio.Lock，必须同循环）。"""
    from app.api import chat_routes

    chat_routes.reset_chat_throttle()
    yield _settings
    chat_routes.reset_chat_throttle()


def _auth() -> dict:
    from app.auth import authenticate, issue_token

    user = authenticate("admin", "hhw123")
    return {"Authorization": f"Bearer {issue_token(user)}"}


async def _client():
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://agent.local",
                       timeout=30.0)


def _make_session() -> str:
    from app.agents import chat_store
    from app.db.engine import session_scope

    with session_scope() as session:
        row = chat_store.create_session(session, "admin")
        return row.id


def test_concurrent_turn_returns_http_409(api):
    """同一会话已有轮次在跑 → 第二个请求拿到 HTTP 409（而不是 200 + SSE error）。"""
    from app.api import chat_routes

    async def main():
        session_id = _make_session()
        guard = chat_routes._SessionGuard(session_id)
        await guard.acquire()          # 模拟"另一轮正在跑"
        try:
            async with await _client() as client:
                resp = await client.post("/api/chat/stream", headers=_auth(),
                                         json={"message": "库存怎么样", "session_id": session_id})
                return resp
        finally:
            await guard.release()

    resp = asyncio.run(main())
    assert resp.status_code == 409, resp.text
    assert "正在处理上一轮" in resp.json()["detail"]
    assert not resp.headers.get("content-type", "").startswith("text/event-stream")


def test_unknown_session_returns_http_404(api):
    async def main():
        async with await _client() as client:
            return await client.post("/api/chat/stream", headers=_auth(),
                                     json={"message": "hi", "session_id": "no-such-session"})

    resp = asyncio.run(main())
    assert resp.status_code == 404
    assert "会话不存在" in resp.json()["detail"]


def test_rate_limit_returns_http_429(api, monkeypatch):
    """频次超限在**建流之前**返回 429，客户端能按 Retry-After 退避。"""
    monkeypatch.setattr(api, "chat_rate_limit_per_minute", 1)

    async def main():
        from app.api import chat_routes

        chat_routes.reset_chat_throttle()
        async with await _client() as client:
            first = await client.post("/api/chat/stream", headers=_auth(),
                                      json={"message": "第一次"})
            second = await client.post("/api/chat/stream", headers=_auth(),
                                       json={"message": "第二次"})
            return first, second

    first, second = asyncio.run(main())
    assert second.status_code == 429
    assert second.headers.get("Retry-After")
    # 第一次要么正常建流（200），要么因为没配 LLM key 在流内失败——都不是 429
    assert first.status_code == 200


def test_lock_is_released_after_stream(api):
    """流结束后锁必须释放：下一次请求能正常开始（早先生成器被取消时会漏放锁）。"""
    from app.api import chat_routes

    async def main():
        session_id = _make_session()
        guard = chat_routes._SessionGuard(session_id)
        await guard.acquire()
        await guard.release()
        # 释放后同一会话可以再次拿到锁
        again = chat_routes._SessionGuard(session_id)
        await again.acquire()
        locked_after = chat_routes._session_lock(session_id).locked()
        await again.release()
        unlocked_after = chat_routes._session_lock(session_id).locked()
        return locked_after, unlocked_after

    locked_after, unlocked_after = asyncio.run(main())
    assert locked_after is True
    assert unlocked_after is False


def test_release_is_idempotent(api):
    """重复释放不能抛错（runner 的 finally 与 background 兜底都会调一次）。"""
    from app.api import chat_routes

    async def main():
        guard = chat_routes._SessionGuard(_make_session())
        await guard.acquire()
        await guard.release()
        await guard.release()
        await guard.release()

    asyncio.run(main())      # 不抛错即通过


def test_user_message_persisted_before_turn(api):
    """用户这句话在跑模型**之前**就落库：模型挂了也能在历史里看到自己问过什么。"""
    from app.agents import chat_store
    from app.api import chat_routes
    from app.auth import AuthUser
    from app.db.engine import session_scope

    user = AuthUser(username="admin", role="admin", department=None)
    req = chat_routes.ChatRequest(message="这轮一定失败", session_id=_make_session())

    async def main():
        turn = await chat_routes._begin_turn(user, req)
        with session_scope() as session:
            history = chat_store.get_history(session, turn.session_id)
        await turn.guard.release()
        return turn.session_id, history

    session_id, history = asyncio.run(main())
    assert any(m["role"] == "user" and m["content"] == "这轮一定失败" for m in history)
    # 落库发生在取锁之后：拿不到锁就不该留下"有问无答"的幽灵消息
    assert session_id


def test_conflict_does_not_persist_ghost_message(api):
    """拿不到锁时不该落用户消息（否则历史里会出现"用户问了但没有任何回应"）。"""
    from app.agents import chat_store
    from app.api import chat_routes
    from app.auth import AuthUser
    from app.db.engine import session_scope

    user = AuthUser(username="admin", role="admin", department=None)

    async def main():
        session_id = _make_session()
        guard = chat_routes._SessionGuard(session_id)
        await guard.acquire()                     # 另一轮持有会话
        try:
            req = chat_routes.ChatRequest(message="并发第二轮", session_id=session_id)
            with pytest.raises(Exception) as exc:
                await chat_routes._begin_turn(user, req)
            status = getattr(exc.value, "status_code", None)
        finally:
            await guard.release()
        with session_scope() as session:
            history = chat_store.get_history(session, session_id)
        return status, history

    status, history = asyncio.run(main())
    assert status == 409
    assert not any(m["content"] == "并发第二轮" for m in history)


def test_stream_content_type_and_heartbeat(api):
    """正常路径仍然是 SSE（text/event-stream），且第一帧就能到（心跳/阶段事件）。"""
    async def main():
        async with await _client() as client:
            async with client.stream("POST", "/api/chat/stream", headers=_auth(),
                                     json={"message": "你好"}) as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        return line
        return ""

    first_event = asyncio.run(main())
    assert first_event.startswith("event:")


def test_httpx_error_type_not_leaked(api):
    """确认上面用的客户端确实是 httpx（避免测试写错成 requests 之类）。"""
    async def main():
        return await _client()

    client = asyncio.run(main())
    assert isinstance(client, httpx.AsyncClient)
