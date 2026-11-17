"""对话限流 / 并发闸门 / prompt cache 指标测试。"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage


def _fake_reply(monkeypatch, *, calls: dict | None = None, delay: float = 0.0):
    """把 run_chat / run_chat_stream 换成可控的桩（记录并发与调用次数）。"""
    import app.api.chat_routes as chat_routes

    state = calls if calls is not None else {"n": 0, "peak": 0, "active": 0}

    async def fake_run_chat(user, history, message):
        state["n"] += 1
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            if delay:
                await asyncio.sleep(delay)
            return {
                "reply": "ok", "tool_trace": [], "metadata_version": 1, "query_plan": None,
                "clarify": None, "cited_result_ids": [], "memory": {},
                "session_references": [], "run_detail": None, "streamed": False,
            }
        finally:
            state["active"] -= 1

    monkeypatch.setattr(chat_routes, "run_chat", fake_run_chat)
    monkeypatch.setattr(chat_routes, "run_chat_stream", fake_run_chat)
    return state


def test_chat_rate_limited_per_user(app_client, login, _settings, monkeypatch):
    """超过每分钟上限 → 429 + Retry-After；换账号不受影响（按用户计数）。"""
    from app.api.chat_routes import reset_chat_throttle

    reset_chat_throttle()
    monkeypatch.setattr(_settings, "chat_rate_limit_per_minute", 2)
    _fake_reply(monkeypatch)
    headers = login("worker1", "hhw111")

    for _ in range(2):
        assert app_client.post("/api/chat", headers=headers,
                               json={"message": "hi"}).status_code == 200

    blocked = app_client.post("/api/chat", headers=headers, json={"message": "hi"})
    assert blocked.status_code == 429
    assert "频繁" in blocked.json()["detail"]
    assert blocked.headers.get("Retry-After")

    # 另一个账号不受影响
    other = login("admin", "hhw123")
    assert app_client.post("/api/chat", headers=other, json={"message": "hi"}).status_code == 200
    reset_chat_throttle()


def test_chat_stream_also_rate_limited(app_client, login, _settings, monkeypatch):
    from app.api.chat_routes import reset_chat_throttle

    reset_chat_throttle()
    monkeypatch.setattr(_settings, "chat_rate_limit_per_minute", 1)
    _fake_reply(monkeypatch)
    headers = login("worker2", "hhw222")
    assert app_client.post("/api/chat/stream", headers=headers,
                           json={"message": "hi"}).status_code == 200
    assert app_client.post("/api/chat/stream", headers=headers,
                           json={"message": "hi"}).status_code == 429
    reset_chat_throttle()


def test_chat_rate_counter_isolated_between_users(app_client, login, _settings, monkeypatch):
    from app.api.chat_routes import chat_throttle_state, reset_chat_throttle

    reset_chat_throttle()
    monkeypatch.setattr(_settings, "chat_rate_limit_per_minute", 1)
    _fake_reply(monkeypatch)
    worker = login("worker1", "hhw111")
    admin = login("admin", "hhw123")
    assert app_client.post("/api/chat", headers=worker, json={"message": "a"}).status_code == 200
    assert chat_throttle_state("worker1")["allowed"] is False
    assert chat_throttle_state("admin")["allowed"] is True
    assert app_client.post("/api/chat", headers=admin, json={"message": "b"}).status_code == 200
    reset_chat_throttle()


def test_chat_concurrency_semaphore_caps_parallel_turns(_settings, monkeypatch):
    """并发上限：同时发起 4 轮、limit=2 → 实际峰值并发 ≤ 2。"""
    from app.agents import chat_agent
    from app.auth import AuthUser

    monkeypatch.setattr(_settings, "chat_max_concurrency", 2)
    chat_agent.reset_chat_semaphores()
    state = {"active": 0, "peak": 0}

    class _SlowAgent:
        async def ainvoke(self, payload):
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            try:
                await asyncio.sleep(0.05)
                return {"messages": payload["messages"] + [AIMessage(content="可用库存 1 件")]}
            finally:
                state["active"] -= 1

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda user, prompt, *a, **k: _SlowAgent())
    monkeypatch.setattr(_settings, "grounding_enabled", False)
    user = AuthUser(username="worker1", role="user", department="男装")

    async def _run():
        await asyncio.gather(*[
            chat_agent.run_chat(user, [], f"库存 {i}") for i in range(4)
        ])

    asyncio.run(_run())
    assert state["peak"] <= 2
    assert chat_agent.chat_limits()["max_concurrency"] == 2
    chat_agent.reset_chat_semaphores()


def test_health_exposes_chat_limits(app_client):
    body = app_client.get("/api/health").json()
    assert body["limits"]["max_concurrency"] >= 1
    assert body["limits"]["rate_limit_per_minute"] >= 1
    assert "inflight" in body["limits"]
    # 工具循环守护（P1-1）的三个阈值也要能从健康检查读到，否则「线上到底卡没卡住」只能靠猜
    from app.agents.tool_guard import FAILURE_LIMIT, REPEAT_LIMIT

    assert body["limits"]["tool_call_budget"] >= 1
    assert body["limits"]["tool_failure_limit"] == FAILURE_LIMIT
    assert body["limits"]["tool_repeat_limit"] == REPEAT_LIMIT


def test_prompt_cache_tokens_recorded_in_run_detail(seeded, monkeypatch, _settings):
    """模型返回里带缓存命中 token → 落到 run_detail.llm.prompt_cache。"""
    from app.agents import chat_agent
    from app.auth import AuthUser

    monkeypatch.setattr(_settings, "grounding_enabled", False)

    class _CacheAgent:
        async def ainvoke(self, payload):
            msg = AIMessage(content="可用库存 1 件")
            msg.usage_metadata = {
                "input_tokens": 1000, "output_tokens": 50,
                "input_token_details": {"cache_read": 800},
            }
            return {"messages": payload["messages"] + [msg]}

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda user, prompt, *a, **k: _CacheAgent())
    user = AuthUser(username="admin", role="admin", department=None)
    result = asyncio.run(chat_agent.run_chat(user, [], "查库存"))
    cache = result["run_detail"]["llm"]["prompt_cache"]
    assert cache["hit_tokens"] == 800
    assert cache["hit_rate"] == pytest.approx(0.8)
    assert "命中" in cache["note"]


def test_prompt_cache_falls_back_to_openai_style_usage(seeded, monkeypatch, _settings):
    """DeepSeek/OpenAI 兼容层把命中放在 response_metadata.token_usage 里，也要读到。"""
    from app.agents import chat_agent
    from app.auth import AuthUser

    monkeypatch.setattr(_settings, "grounding_enabled", False)

    class _CacheAgent:
        async def ainvoke(self, payload):
            msg = AIMessage(content="可用库存 1 件")
            msg.usage_metadata = {"input_tokens": 500, "output_tokens": 20}
            msg.response_metadata = {"token_usage": {"prompt_cache_hit_tokens": 300}}
            return {"messages": payload["messages"] + [msg]}

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda user, prompt, *a, **k: _CacheAgent())
    user = AuthUser(username="admin", role="admin", department=None)
    result = asyncio.run(chat_agent.run_chat(user, [], "查库存"))
    assert result["run_detail"]["llm"]["prompt_cache"]["hit_tokens"] == 300
