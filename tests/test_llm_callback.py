"""对话模型的延迟采样（llm_chat）与分类标签。"""

from __future__ import annotations

import time


def test_latency_categories_include_llm_chat():
    from app.observability import latency

    snap = latency.snapshot()
    assert "llm_chat" in snap["categories"]
    assert "对话" in snap["labels"]["llm_chat"]
    assert "JSON 模式" in snap["labels"]["llm"], "JSON 模式分类要在标签里写明口径"


def test_callback_records_chat_model_latency():
    from app.observability import latency
    from app.observability.llm_callback import LatencyCallbackHandler

    handler = LatencyCallbackHandler()

    class FakeRun:
        def __str__(self) -> str:
            return "run-llm-chat-1"

    handler.on_chat_model_start({}, [], run_id=FakeRun())
    time.sleep(0.03)
    handler.on_llm_end(None, FakeRun())

    bucket = latency.snapshot()["categories"]["llm_chat"]
    assert bucket["count"] >= 1
    assert bucket["p50"] >= 20, bucket


def test_callback_error_is_swallowed():
    """回调异常不能影响对话主流程。"""
    from app.observability.llm_callback import LatencyCallbackHandler

    handler = LatencyCallbackHandler()
    handler.on_llm_end(None, None)  # run_id 为 None 时应安全
    handler.on_llm_error(None, "unknown")


def test_chat_agent_attaches_latency_callback(seeded, monkeypatch):
    from app.agents import chat_agent
    from app.auth import AuthUser

    captured: dict = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(chat_agent, "ChatOpenAI", FakeOpenAI)
    monkeypatch.setattr(chat_agent, "build_platform_tools", lambda user, **kw: [])
    monkeypatch.setattr(chat_agent, "create_react_agent", lambda *a, **k: object())
    user = AuthUser(username="admin", role="admin", department=None)
    chat_agent.build_chat_agent(user, "system")
    callbacks = captured.get("callbacks") or []
    assert callbacks, "build_chat_agent 应给 ChatOpenAI 传 latency 回调"
    assert type(callbacks[0]).__name__ == "LatencyCallbackHandler"


def test_metrics_latency_exposes_llm_chat(app_client):
    data = app_client.get("/api/metrics").json()
    latency_payload = data["latency"]
    assert "llm_chat" in latency_payload["categories"]
    assert "对话·ReAct" in latency_payload["labels"]["llm_chat"]
