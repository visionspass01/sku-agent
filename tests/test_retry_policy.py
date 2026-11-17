"""P1-4 transient 判定 + P0-1 partial 失败不进退避/fallback。"""

from __future__ import annotations

import pytest

from app.agents.chat_agent import _is_transient


class _RateLimit(Exception):
    pass


class _APITimeoutError(Exception):
    pass


class _HTTPStatus(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_error_class_name_is_transient():
    assert _is_transient(_APITimeoutError("boom"))
    assert _is_transient(_RateLimit("slow down"))


def test_http_status_codes_are_transient():
    for code in (429, 500, 502, 503, 504):
        assert _is_transient(_HTTPStatus("upstream", code)) is True
    assert _is_transient(_HTTPStatus("bad request", 400)) is False


def test_message_based_fallback_is_narrow():
    assert _is_transient(RuntimeError("Connection reset by peer"))
    assert _is_transient(RuntimeError("request timed out"))
    assert _is_transient(RuntimeError("service unavailable"))
    # 关键回归：业务文案里出现 "500 件" 不能被当成 HTTP 500 而重试
    assert _is_transient(ValueError("补货数量必须 >= 500 件才进人工确认")) is False
    assert _is_transient(ValueError("库存 500 件，无需补货")) is False


def test_partial_deltas_never_transient():
    exc = TimeoutError("timed out")
    exc.partial_deltas = 3  # type: ignore[attr-defined]
    assert _is_transient(exc) is False


def test_partial_failure_does_not_fall_back(seeded, monkeypatch, _settings):
    """流式已经推过 delta 再失败：不重试、不切备用模型（避免前端收到两段答案）。"""
    import asyncio

    from app.agents import chat_agent
    from app.auth import AuthUser

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(_settings, "chat_max_retries", 2)
    monkeypatch.setattr(_settings, "chat_fallback_model", "deepseek-chat")
    attempts = {"n": 0}

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(chat_agent.asyncio, "sleep", _no_sleep)

    class _HalfBrokenAgent:
        async def astream(self, payload, stream_mode=None):
            attempts["n"] += 1
            yield "messages", (type("C", (), {"content": "可用库存 ", "tool_calls": None,
                                              "tool_call_chunks": None, "type": "ai"})(), {})
            raise TimeoutError("stream dropped")

    monkeypatch.setattr(chat_agent, "build_chat_agent",
                        lambda user, prompt, *a, **k: _HalfBrokenAgent())
    user = AuthUser(username="admin", role="admin", department=None)

    async def _go():
        with pytest.raises(TimeoutError):
            await chat_agent.run_chat_stream(user, [], "查库存")

    asyncio.run(_go())
    assert attempts["n"] == 1, "已推过 delta 时不允许再试一次（会重复输出）"
