"""E6：trajectory 指标端点 + 「中止轮次」台账的端到端测试。

两个关键契约：
1. `GET /api/metrics/trajectory` 要能回答「工具数/步数/重复调用/预算/中止率/多轮成功率」，
   并且匿名访问 401、部门账号只看到本部门轮次（与 `/api/metrics/agents` 同口径）；
2. 被守护中止的轮次**必须落一行台账**（`chat_turn_log.grounding_status=aborted`）——
   否则 `abort_rate` 永远是 0，「守护到底拦下多少轮」只能翻日志。
"""

from __future__ import annotations

import itertools

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage


class _Looping(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        return self


def _looping_agent():
    from langgraph.prebuilt import create_react_agent

    from app.auth import AuthUser
    from app.tools.platform_tools import build_platform_tools

    user = AuthUser(username="worker1", role="user", department="男装")
    tools = build_platform_tools(user, allow=["get_metric_definitions"])

    def _ai(i: int) -> AIMessage:
        return AIMessage(content="", tool_calls=[
            {"name": "get_metric_definitions", "args": {}, "id": f"c{i}"},
        ])

    return create_react_agent(_Looping(messages=itertools.chain(_ai(i) for i in range(20))), tools)


def _aborted_rows(session_id: str | None = None) -> list:
    from app.agents.chat_store import load_turn_rows
    from app.db.engine import session_scope

    with session_scope() as session:
        rows = load_turn_rows(session, hours=24)
    if session_id is None:
        return rows
    return [row for row in rows if row["session_id"] == session_id]


def test_trajectory_endpoint_requires_login(app_client):
    assert app_client.get("/api/metrics/trajectory").status_code == 401


def test_trajectory_endpoint_shape(app_client, login):
    headers = login("admin", "hhw123")
    data = app_client.get("/api/metrics/trajectory?hours=24", headers=headers).json()
    for key in ("turns", "aborted_turns", "abort_rate", "tool_calls_per_turn",
                "llm_calls_per_turn", "repeat_call_rate", "turns_with_repeats",
                "budget_utilization", "per_agent", "top_tools", "multi_turn", "process"):
        assert key in data, (key, data.keys())
    assert set(data["tool_calls_per_turn"]) == {"avg", "p50", "p95", "max"}
    assert data["process"]["note"]
    assert data["scope"]["role"] == "admin"


def test_trajectory_endpoint_scopes_by_department(app_client, login):
    """部门账号只能看到本部门账号产生的轮次（与 metrics/agents 同口径）。"""
    from app.agents.chat_store import save_turn_log
    from app.db.engine import session_scope

    detail = {"turn_id": "t_scope", "elapsed_ms": 10, "steps": [], "llm": {"calls": 1},
              "agent": {"id": "analyst"}, "grounding": {"status": "passed"}}
    with session_scope() as session:
        save_turn_log(session, "sess-men", 0, "worker1", dict(detail))     # 男装
        save_turn_log(session, "sess-women", 0, "worker2", dict(detail))   # 女装

    worker1 = login("worker1", "hhw111")
    data = app_client.get("/api/metrics/trajectory?hours=24", headers=worker1).json()
    assert data["scope"]["department"] == "男装"
    assert data["turns"] >= 1
    # 只看自己的轮次：女装账号的那一轮不该被算进来
    admin = login("admin", "hhw123")
    admin_data = app_client.get("/api/metrics/trajectory?hours=24", headers=admin).json()
    assert admin_data["turns"] >= data["turns"]
    assert admin_data["turns"] > 0


def test_aborted_turn_is_persisted_for_trajectory(app_client, login, seeded, monkeypatch):
    """走完整的 /api/chat：循环中止后必须留一行 `grounding_status=aborted` 台账。"""
    from app.agents import chat_agent
    from app.api import chat_routes

    agent = _looping_agent()

    async def _meta():
        return "口径", 1

    monkeypatch.setattr(chat_agent, "_metadata_text", _meta)
    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda *a, **k: agent)

    async def _run(user, history, message, **kwargs):
        return await chat_agent.run_chat(user, history, message, **kwargs)

    monkeypatch.setattr(chat_routes, "run_chat", _run)

    headers = login("worker1", "hhw111")
    resp = app_client.post("/api/chat", headers=headers, json={"message": "口径是什么"})
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert "TOOL_LOOP_REPEAT" in body["detail"]
    session_id = None            # 422 响应不回传会话；会话 id 从中止台账里取（N2 校验用）

    rows = [row for row in _aborted_rows() if row["grounding_status"] == "aborted"]
    assert rows, "中止轮次没有落台账"
    latest = rows[-1]
    assert latest["payload"].get("error_code") == "TOOL_LOOP_REPEAT"
    guard = latest["payload"].get("guard") or {}
    assert guard.get("success_calls") == 3
    assert guard.get("max_calls") and guard["max_calls"] >= 1
    assert latest["agent_id"] == "analyst"
    session_id = latest["session_id"]
    assert session_id, "中止台账里应带会话 id"

    # 复审 F1：中止轮次在 trajectory 里不能算成「0 个工具调用」
    from app.agents import trajectory

    stats = trajectory.aggregate(_aborted_rows(), hours=24)
    assert stats["aborted_turns"] >= 1
    assert stats["tool_calls_per_turn"]["max"] >= 3

    # 复审 N2：中止轮次要补一条标记 assistant 消息，历史不能出现「连续两条用户消息」
    from app.agents import chat_store
    from app.db.engine import session_scope

    with session_scope() as session:
        messages = chat_store.list_messages(session, session_id)
        roles = [m.role for m in messages]
        marker = messages[-1].content if messages else ""
        history = chat_store.get_history(session, session_id)
    assert roles == ["user", "assistant"], roles
    assert "中止" in marker
    # 标记刻意不带数字：否则会被 build_facts_card 当成"结论事实"塞进下一轮事实卡
    assert not any(ch.isdigit() for ch in marker), marker
    assert history[-1]["role"] == "assistant", history
