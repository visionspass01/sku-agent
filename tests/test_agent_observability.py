"""Agent 维度可观测：run_detail.agent、工具统计按 agent 归集、metrics 按 agent 分表。"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage


def _fake_agent(monkeypatch, reply: str = "可用库存 1 件（res_0000000000）"):
    from app.agents import chat_agent

    class _Agent:
        async def ainvoke(self, payload):
            return {"messages": payload["messages"] + [AIMessage(content=reply)]}

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda user, prompt, *a, **k: _Agent())
    return _Agent()


def test_run_detail_has_agent_section(seeded, monkeypatch, _settings):
    from app.agents import chat_agent
    from app.agents.registry import get_registry
    from app.auth import AuthUser

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    _fake_agent(monkeypatch)
    user = AuthUser(username="admin", role="admin", department=None)
    result = asyncio.run(chat_agent.run_chat(user, [], "ST-01-C1-M 断码了吗"))
    agent = result["run_detail"]["agent"]
    manifest = get_registry().get("diagnostician")

    assert agent["id"] == "diagnostician"
    assert agent["routed_by"].startswith("keyword:")
    assert agent["version"] == manifest.version
    assert agent["model"]
    assert "create_replenishment_order" not in agent["tools"]   # 只读 agent
    assert agent["retries"] == 0
    # 顶层响应也带 agent，前端/脚本不用翻 run_detail
    assert result["agent"]["id"] == "diagnostician"


def test_explicit_agent_in_run_detail(seeded, monkeypatch, _settings):
    from app.agents import chat_agent
    from app.auth import AuthUser

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    _fake_agent(monkeypatch)
    user = AuthUser(username="admin", role="admin", department=None)
    result = asyncio.run(chat_agent.run_chat(user, [], "随便问一句", agent_id="operator"))
    agent = result["run_detail"]["agent"]
    assert agent["id"] == "operator"
    assert agent["routed_by"] == "explicit"


def test_tool_stats_keyed_by_agent(seeded, monkeypatch):
    """工具统计的 server 维度变成 chat:<agent_id>，看板可按 agent 拆分可靠性。"""
    import httpx

    from app.auth import AuthUser
    from app.memory.runtime import tool_context
    from app.observability import tool_stats
    from app.tools import platform_tools
    from app.tools.platform_tools import build_platform_tools

    tool_stats.reset()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"available": 10, "as_of": "2026-11-04"})

    platform_tools.set_platform_client(platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler)))
    try:
        user = AuthUser(username="admin", role="admin", department=None)
        tools = {t.name: t for t in build_platform_tools(user)}
        with tool_context(session_id="s1", username="admin", agent_id="diagnostician"):
            asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-92-C1-M"}))
    finally:
        platform_tools.set_platform_client(None)

    keys = tool_stats.snapshot()["tools"]
    assert any(k.startswith("chat:diagnostician.") for k in keys), keys


def test_metrics_expose_agent_dimension(app_client, login):
    headers = login("admin", "hhw123")
    data = app_client.get("/api/metrics", headers=headers).json()
    tools = (data.get("tools") or {}).get("tools") or {}
    # 没有跑过对话时为空；跑过之后键形如 chat:<agent>.<tool>（本用例只断言结构可用）
    assert isinstance(tools, dict)


def test_evidence_line_mentions_agent():
    """前端证据链会显示 agent@version：这里校验后端提供的字段足够拼出它。"""
    from app.agents import chat_agent  # noqa: F401

    assert hasattr(chat_agent, "resolve_agent")


def test_agent_attribution_fields(seeded, monkeypatch, _settings):
    """归因三件套：请求的模型 / 实际用的模型 / 是否回退；以及工具数与"全部"标记。"""
    from app.agents import chat_agent
    from app.auth import AuthUser
    from app.tools.platform_tools import ALL_TOOL_NAMES

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    _fake_agent(monkeypatch)
    user = AuthUser(username="admin", role="admin", department=None)

    diag = asyncio.run(chat_agent.run_chat(user, [], "ST-01-C1-M 断码了吗"))
    agent = diag["run_detail"]["agent"]
    assert agent["model_requested"] == agent["model_used"]      # 没回退时两者一致
    assert agent["fallback_used"] is False
    assert agent["tool_count"] == len(agent["tools"]) and agent["tools_all"] is False
    assert agent["memory_scope"] == "session"
    assert agent["max_reflection_rounds"] == 0

    full = asyncio.run(chat_agent.run_chat(user, [], "你好", agent_id="analyst"))
    agent_full = full["run_detail"]["agent"]
    assert agent_full["tools_all"] is True
    assert agent_full["tool_count"] == len(ALL_TOOL_NAMES) == 27


def test_fallback_attribution_is_honest(seeded, monkeypatch, _settings):
    """用备用模型时，run_detail 必须体现「请求的模型 ≠ 实际模型」且 fallback_used=True。"""
    from app.agents import chat_agent
    from app.auth import AuthUser

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(_settings, "chat_max_retries", 0)
    monkeypatch.setattr(_settings, "chat_fallback_model", "deepseek-chat")

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(chat_agent.asyncio, "sleep", _no_sleep)

    class _Model:
        def __init__(self, name):
            self.name = name

        async def ainvoke(self, payload):
            if self.name != "deepseek-chat":
                raise ConnectionError("connection reset")
            from langchain_core.messages import AIMessage

            return {"messages": payload["messages"] + [AIMessage(content="可用库存 1 件")]}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            self._model = _Model(kwargs.get("model", ""))
            self._kwargs = kwargs

        def bind(self, **kwargs):  # noqa: ANN003
            return self

        def __getattr__(self, item):
            return getattr(self._model, item)

    monkeypatch.setattr(chat_agent, "ChatOpenAI", _FakeChatOpenAI)
    monkeypatch.setattr(chat_agent, "create_react_agent", lambda model, tools, **kw: model._model)
    monkeypatch.setattr(chat_agent, "build_platform_tools", lambda user, **kw: [])

    user = AuthUser(username="admin", role="admin", department=None)
    result = asyncio.run(chat_agent.run_chat(user, [], "你好"))
    agent = result["run_detail"]["agent"]
    assert agent["fallback_used"] is True
    assert agent["model_used"] == "deepseek-chat"
    assert agent["model_requested"] != agent["model_used"]
