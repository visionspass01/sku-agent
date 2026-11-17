"""P2-11：对话模型复用同一个 HTTP 连接池（不再每轮重建连接）。"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _reset_pool():
    from app.agents import chat_agent

    asyncio.run(chat_agent.aclose_shared_http())
    yield
    asyncio.run(chat_agent.aclose_shared_http())


def _capture(monkeypatch) -> list[dict]:
    from app.agents import chat_agent

    captured: list[dict] = []

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(chat_agent, "ChatOpenAI", _FakeChatOpenAI)
    monkeypatch.setattr(chat_agent, "build_platform_tools", lambda user, **kw: [])
    monkeypatch.setattr(chat_agent, "create_react_agent", lambda *a, **k: object())
    return captured


def test_same_http_client_is_reused_across_turns(seeded, monkeypatch):
    from app.agents import chat_agent
    from app.auth import AuthUser

    captured = _capture(monkeypatch)
    user = AuthUser(username="admin", role="admin", department=None)
    chat_agent.build_chat_agent(user, "system")
    chat_agent.build_chat_agent(user, "system")

    clients = [kwargs.get("http_async_client") for kwargs in captured]
    assert all(client is not None for client in clients)
    assert clients[0] is clients[1], "两次装配应复用同一个连接池"
    assert not clients[0].is_closed


def test_close_resets_pool(seeded, monkeypatch):
    from app.agents import chat_agent
    from app.auth import AuthUser

    captured = _capture(monkeypatch)
    user = AuthUser(username="admin", role="admin", department=None)
    chat_agent.build_chat_agent(user, "system")
    first = captured[0]["http_async_client"]

    asyncio.run(chat_agent.aclose_shared_http())
    chat_agent.build_chat_agent(user, "system")
    second = captured[1]["http_async_client"]

    assert first is not second
    assert first.is_closed and not second.is_closed
