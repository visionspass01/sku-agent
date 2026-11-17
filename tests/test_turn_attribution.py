"""归属与接线（D1/D2）：run_detail 必须如实记录「本轮用了哪版提示词/哪个模型」，
并且 query_plan 必须真的拿到会话工作集（否则指代解析是死的）。

注意：会话用**独立账号**（每次测试一个随机 username），不动 admin/worker 的会话列表——
`list_sessions` 默认只取最近 20 条，共享库里给 admin 多造会话会把别的用例挤出去。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from langchain_core.messages import AIMessage


@pytest.fixture()
def chat_user():
    from app.auth import AuthUser

    return AuthUser(username=f"attr_{uuid.uuid4().hex[:8]}", role="admin", department=None)


@pytest.fixture()
def fake_agent(monkeypatch):
    """替掉真实模型：不联网，直接返回一句带单位数字的答复。"""
    from app.agents import chat_agent

    class _FakeAgent:
        async def ainvoke(self, payload):
            return {"messages": payload["messages"] + [AIMessage(content="可用库存 900 件")]}

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda *a, **k: _FakeAgent())
    return _FakeAgent


@pytest.fixture()
def session_id(seeded, chat_user):
    from app.agents import chat_store
    from app.db.engine import session_scope

    with session_scope() as session:
        return chat_store.create_session(session, chat_user.username).id


def _turn(chat_user, session_id: str, message: str, **kwargs):
    from app.agents import chat_agent
    from app.memory.runtime import chat_turn_context

    with chat_turn_context(session_id=session_id):
        return asyncio.run(chat_agent.run_chat(chat_user, [], message, **kwargs))


def test_reference_resolution_uses_session_working_set(seeded, monkeypatch, _settings,
                                                       fake_agent, chat_user, session_id):
    """回归：`bundle["working_set"]` 曾经算完就被丢掉，导致「这个款」解析不到实体。"""
    monkeypatch.setattr(_settings, "grounding_enabled", False)

    _turn(chat_user, session_id, "查一下 ST-92-C1-M 的库存")
    second = _turn(chat_user, session_id, "这个款还能撑几天？")

    plan = second["run_detail"]["plan"]
    assert plan is not None
    assert plan["resolved_from"] == "working_set.skus", "工作集没有接到 query_plan 上"
    assert plan["entities"]["skus"] == ["ST-92-C1-M"]
    assert plan["confidence"] >= 0.6, "解析到实体后置信度不该仍停在阈值以下"


def test_plan_without_working_set_would_lose_entity(seeded):
    """对照：空工作集时同一句话解析不到实体（证明上面那条断言不是巧合）。"""
    from app.agents.query_plan import build_query_plan

    empty = build_query_plan("这个款还能撑几天？", working_set={})
    assert empty["resolved_from"] in ("", None)
    assert empty["entities"]["skus"] == []


def test_run_detail_prompt_records_the_prompt_actually_used(seeded, monkeypatch, _settings,
                                                            fake_agent, chat_user, session_id):
    """回归：这里曾写死 prompt_version("chat_system")，于是 diagnostician 的结论被记成
    默认模板产出的，可复现性字段直接撒谎。"""
    from app.agents.prompt_loader import prompt_version

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    result = _turn(chat_user, session_id, "ST-92-C1-M 补多少合适？", agent_id="diagnostician")

    prompt = result["run_detail"]["prompt"]
    assert result["agent"]["prompt"] == "diagnostician"
    assert prompt["name"] == "diagnostician"
    assert prompt["version"] == prompt_version("diagnostician")
    assert prompt["version"] != prompt_version("chat_system")
    # 兼容字段仍保留（前端/老脚本会读）
    assert prompt["chat_system"] == prompt_version("chat_system")


def test_run_detail_prompt_default_agent_still_matches(seeded, monkeypatch, _settings,
                                                       fake_agent, chat_user, session_id):
    from app.agents.prompt_loader import prompt_version

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    result = _turn(chat_user, session_id, "ST-92-C1-M 库存多少？", agent_id="analyst")

    prompt = result["run_detail"]["prompt"]
    assert prompt["name"] == "chat_system"
    assert prompt["version"] == prompt_version("chat_system")


def test_run_detail_llm_model_matches_agent_attribution(seeded, monkeypatch, _settings,
                                                        fake_agent, chat_user, session_id):
    monkeypatch.setattr(_settings, "grounding_enabled", False)
    result = _turn(chat_user, session_id, "ST-92-C1-M 库存多少？")

    agent = result["run_detail"]["agent"]
    llm = result["run_detail"]["llm"]
    assert llm["model"] == agent["model_used"]
    assert llm["model_requested"] == agent["model_requested"]
    assert llm["model_fallback_used"] is agent["fallback_used"]
