"""对话健壮性测试：transient 重试、业务错误不重试、流式半截不重试、fallback 模型、闸门计数。"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage


@pytest.fixture()
def fast_sleep(monkeypatch):
    """把重试退避改成 0 秒，测试不真等。"""
    import app.agents.chat_agent as chat_agent

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(chat_agent.asyncio, "sleep", _no_sleep)


def _user():
    from app.auth import AuthUser

    return AuthUser(username="admin", role="admin", department=None)


def _run(message: str = "查库存"):
    from app.agents import chat_agent

    return asyncio.run(chat_agent.run_chat(_user(), [], message))


def test_transient_error_is_retried_once(seeded, monkeypatch, _settings, fast_sleep):
    from app.agents import chat_agent

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(_settings, "chat_max_retries", 1)
    calls = {"n": 0}

    class _FlakyAgent:
        async def ainvoke(self, payload):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("upstream timed out")
            return {"messages": payload["messages"] + [AIMessage(content="可用库存 1 件")]}

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda user, prompt, *a, **k: _FlakyAgent())
    result = _run()
    assert calls["n"] == 2
    assert result["run_detail"]["agent"]["retries"] == 1
    assert result["reply"]


def test_business_error_is_not_retried(seeded, monkeypatch, _settings, fast_sleep):
    from app.agents import chat_agent

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(_settings, "chat_max_retries", 2)
    calls = {"n": 0}

    class _BrokenAgent:
        async def ainvoke(self, payload):
            calls["n"] += 1
            raise ValueError("参数不合法（业务错误）")

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda user, prompt, *a, **k: _BrokenAgent())
    with pytest.raises(ValueError):
        _run()
    assert calls["n"] == 1  # 业务错误不该被重试


def test_nontransient_error_does_not_fall_back(seeded, monkeypatch, _settings, fast_sleep):
    """非瞬时错误（参数/上下文超长）既不该重试，也不该再打一次备用模型。

    修复前的实现：`break` 只跳出内层重试循环，外层照样进入 fallback ——
    一次必然失败的请求要多等一个模型往返（最长 llm_timeout_seconds）。
    """
    from app.agents import chat_agent

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(_settings, "chat_max_retries", 2)
    monkeypatch.setattr(_settings, "chat_fallback_model", "deepseek-chat")
    calls = {"invoked": 0, "built": 0}

    class _BrokenAgent:
        async def ainvoke(self, payload):
            calls["invoked"] += 1
            raise ValueError("invalid_request_error: messages too long")

    def _build(user, prompt, *a, **k):
        calls["built"] += 1
        return _BrokenAgent()

    monkeypatch.setattr(chat_agent, "build_chat_agent", _build)
    with pytest.raises(ValueError):
        _run()
    assert calls["invoked"] == 1, "非瞬时错误不该被重试"
    assert calls["built"] == 1, "非瞬时错误不该再装配一个 fallback agent"


def test_stream_does_not_retry_after_partial_output(seeded, monkeypatch, _settings, fast_sleep):
    from app.agents import chat_agent

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(_settings, "chat_max_retries", 2)
    attempts = {"n": 0}

    class _HalfBrokenStreamAgent:
        async def astream(self, payload, stream_mode=None):
            attempts["n"] += 1
            yield "messages", (type("C", (), {"content": "可用库存 ", "tool_calls": None,
                                              "tool_call_chunks": None, "type": "ai"})(), {})
            raise TimeoutError("stream dropped")

    monkeypatch.setattr(chat_agent, "build_chat_agent",
                        lambda user, prompt, *a, **k: _HalfBrokenStreamAgent())

    async def _go():
        with pytest.raises(TimeoutError):
            await chat_agent.run_chat_stream(_user(), [], "查库存")

    asyncio.run(_go())
    assert attempts["n"] == 1  # 已经推过 delta，不再重试


def test_fallback_model_used_after_retries(seeded, monkeypatch, _settings, fast_sleep):
    from app.agents import chat_agent

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(_settings, "chat_max_retries", 0)
    monkeypatch.setattr(_settings, "chat_fallback_model", "deepseek-chat")
    seen_models: list[str] = []

    class _Model:
        def __init__(self, name):
            self.name = name
            seen_models.append(name)

        async def ainvoke(self, payload):
            if self.name != "deepseek-chat":
                raise ConnectionError("connection reset")
            return {"messages": payload["messages"] + [AIMessage(content="可用库存 1 件")]}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            self._model = _Model(kwargs.get("model", ""))

        def bind(self, **kwargs):  # noqa: ANN003
            return self

        def __getattr__(self, item):  # 让 create_react_agent 需要的属性透传
            return getattr(self._model, item)

    # 直接用真实 build_chat_agent（这样 fallback 才会重建一个指定模型的 agent）
    monkeypatch.setattr(chat_agent, "ChatOpenAI", _FakeChatOpenAI)
    monkeypatch.setattr(chat_agent, "create_react_agent", lambda model, tools, **kw: model._model)
    monkeypatch.setattr(chat_agent, "build_platform_tools", lambda user, **kw: [])
    result = _run()
    assert "deepseek-chat" in seen_models
    assert result["reply"]
    assert result["run_detail"]["agent"]["model_used"] == "deepseek-chat"
    assert result["run_detail"]["agent"]["fallback_used"] is True


def test_chat_gate_inflight_is_accurate(seeded, monkeypatch, _settings):
    """并发闸门的 inflight 走真实计数（不再反读 Semaphore._value）。"""
    from app.agents import chat_agent

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(_settings, "chat_max_concurrency", 4)
    chat_agent.reset_chat_semaphores()
    observed: list[int] = []

    class _SlowAgent:
        async def ainvoke(self, payload):
            observed.append(chat_agent.chat_limits()["inflight"])
            await asyncio.sleep(0.05)
            return {"messages": payload["messages"] + [AIMessage(content="可用库存 1 件")]}

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda user, prompt, *a, **k: _SlowAgent())

    async def _go():
        await asyncio.gather(*[chat_agent.run_chat(_user(), [], f"库存 {i}") for i in range(3)])

    asyncio.run(_go())
    assert max(observed) == 3          # 三个轮次同时在跑
    assert chat_agent.chat_limits()["inflight"] == 0  # 跑完归零
    chat_agent.reset_chat_semaphores()


def test_gate_rebuilds_when_limit_changes(seeded, monkeypatch, _settings):
    from app.agents import chat_agent

    chat_agent.reset_chat_semaphores()
    monkeypatch.setattr(_settings, "chat_max_concurrency", 2)

    async def _go():
        gate = chat_agent._chat_gate()
        assert gate.limit == 2
        monkeypatch.setattr(_settings, "chat_max_concurrency", 5)
        gate2 = chat_agent._chat_gate()
        return gate2.limit

    assert asyncio.run(_go()) == 5
    chat_agent.reset_chat_semaphores()
