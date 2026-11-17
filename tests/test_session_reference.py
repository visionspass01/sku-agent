"""`@session` 引用解析（复审 S1）：要么命中，要么**明确失败**，绝不拿无关档案顶上。

背景：早先 `resolve_session_reference` 在「id 不存在 / 无权限 / 标题没命中」三种情况下
都会回退成 `search_analyses(token)`，而那边关键词没命中时又会返回"最近一份档案"——
于是三种完全不同的语义在模型看来是同一个结果，它会拿一份无关档案当作用户引用的那份作答，
`run_detail.references.analyses` 也会记下一个错误的 id。
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture()
def archives(seeded):
    """造三份档案：本部门可见（男装）、别部门（女装）、全员（公司级）。"""
    from app.db.engine import session_scope
    from app.memory import org_memory

    with session_scope() as session:
        men = org_memory.save_analysis(
            session=session, department="男装", username="worker1",
            question="ST-92-C1-M 积压怎么处理", conclusion="男装 ST-92 建议先调拨再清仓",
            key_metrics={"available": 900}, evidence=["res_men0000001"],
        )
        women = org_memory.save_analysis(
            session=session, department="女装", username="worker2",
            question="ST-91-C1-M 积压怎么处理", conclusion="女装 ST-91 建议 8 折清仓",
            key_metrics={"available": 900}, evidence=["res_women00001"],
        )
        company = org_memory.save_analysis(
            session=session, department=None, username="admin",
            question="整体动销率怎么样", conclusion="全公司动销率 62%",
            key_metrics={"rate": 0.62}, evidence=["res_company0001"],
        )
    return {"men": men["analysis_id"], "women": women["analysis_id"],
            "company": company["analysis_id"]}


def _resolve(message: str, departments: list[str] | None = None) -> dict:
    from app.db.engine import session_scope
    from app.memory import org_memory

    with session_scope() as session:
        return org_memory.resolve_session_reference(session, message, departments=departments)


MEN = ["男装"]


def test_exact_id_hit(archives):
    state = _resolve(f"@session {archives['men']} 这个结论还用吗", MEN)
    assert state["status"] == "ok"
    assert [i["analysis_id"] for i in state["items"]] == [archives["men"]]


def test_other_department_id_is_denied_not_replaced(archives):
    """别部门的 id → denied，且**不返回任何档案**（旧行为会塞一份"最近档案"）。"""
    state = _resolve(f"@session {archives['women']} 这个结论还用吗", MEN)
    assert state["status"] == "denied", state
    assert state["items"] == []
    assert state["token"] == archives["women"]


def test_unknown_id_is_not_found_not_replaced(archives):
    state = _resolve("@session 00000000-0000-0000-0000-000000000000 看看这份", MEN)
    assert state["status"] == "not_found", state
    assert state["items"] == []


def test_free_text_without_match_is_not_found(archives):
    """自由文本没命中 → not_found；不再回退成"最近的档案"。"""
    state = _resolve("@session 完全不存在的主题内容 看看这份", MEN)
    assert state["status"] == "not_found", state
    assert state["items"] == []


def test_free_text_with_match_returns_hits(archives):
    state = _resolve("@session 男装 积压 调拨", MEN)
    assert state["status"] == "ok"
    assert state["items"]
    assert state["items"][0]["matched"] == "keyword"
    assert "ST-92" in state["items"][0]["conclusion"]


def test_free_text_keeps_the_whole_title(archives):
    """带空格的标题不再被截成第一个词（旧正则只取 `[^\\s]+`）。"""
    state = _resolve("@session 男装 积压 调拨 复盘结论 看看", MEN)
    assert state["status"] == "ok", state
    assert state["token"].startswith("男装 积压")


def test_no_reference_returns_none_status(archives):
    state = _resolve("看下 ST-92-C1-M 的库存", MEN)
    assert state["status"] == "none"
    assert state["items"] == []


def test_analysis_context_renders_status_for_failures(archives):
    """失败状态必须进提示词（否则模型只能猜，而猜的做法是拿最近的档案顶上）。"""
    from app.memory.org_memory import analysis_context

    denied = analysis_context(_resolve(f"@session {archives['women']} 还记得吗", MEN))
    assert 'status="denied"' in denied
    assert "不能引用" in denied

    missing = analysis_context(_resolve("@session 00000000-0000-0000-0000-000000000000", MEN))
    assert 'status="not_found"' in missing
    assert "不要编造" in missing

    none = analysis_context(_resolve("今天天气怎么样", MEN))
    assert none == ""


def test_visible_id_renders_ok_block(archives):
    from app.memory.org_memory import analysis_context

    text = analysis_context(_resolve(f"@session {archives['company']} 还用吗", MEN))
    assert 'status="ok"' in text
    assert "全公司动销率 62%" in text

    text_women = analysis_context(_resolve(f"@session {archives['women']} 还用吗",
                                           ["女装", "童装"]))
    assert 'status="ok"' in text_women
    assert "女装 ST-91" in text_women


def test_tool_keeps_recent_fallback_but_labels_it(archives, monkeypatch):
    """`search_analyses` **工具**仍保留"最近档案"回退（周报场景），但必须带 `matched` 标注。"""
    import asyncio

    from app.auth import AuthUser
    from app.memory.runtime import tool_context, turn_log_context
    from app.tools import platform_tools

    tools = {t.name: t for t in platform_tools.build_platform_tools(
        AuthUser(username="worker1", role="user", department="男装"))}

    async def _run():
        with tool_context(session_id="s_ref", username="worker1", department="男装",
                          agent_id="analyst"):
            with turn_log_context("t_ref", "s_ref"):
                return await tools["search_analyses"].ainvoke({"query": "完全不存在的主题"})

    out = asyncio.run(_run())
    assert out["count"] >= 1                      # 回退仍然发生（工具语义不变）
    assert out["matched"] == "recent_fallback"
    assert "最近的档案" in out["note"]
    assert all(i["matched"] == "recent_fallback" for i in out["items"])


def test_judge_checks_session_reference_status():
    """复审 S4：判定器新增 `session_reference` 维度（仅 live：靠 run_detail 判定）。"""
    from app.agents.answer_eval import judge

    case = {"expect": {"tools": [], "must_cite": False, "session_reference": "not_found"}}
    ok_detail = {"references": {"session_reference": {"status": "not_found", "token": "t"}}}
    ok = judge(case, "没找到你引用的那份档案。", [], [], run_detail=ok_detail)
    assert ok["checks"]["session_reference"] is True
    assert ok["passed"] is True

    bad_detail = {"references": {"session_reference": {"status": "ok", "token": "t"}}}
    bad = judge(case, "那份结论说女装 8 折清仓。", [], [], run_detail=bad_detail)
    assert bad["checks"]["session_reference"] is False
    assert any("引用解析状态不符" in i for i in bad["issues"])

    # 离线样例没有 run_detail → 该维度不检查（不计入分母）
    assert "session_reference" not in judge(case, "x", [], [])["checks"]


def test_reference_state_is_reported_in_run_detail(seeded, monkeypatch, _settings):
    """`run_detail.references.session_reference` 要能回答"这轮引用了什么、成不成功"。"""
    from langchain_core.messages import AIMessage

    from app.agents import chat_agent
    from app.auth import AuthUser
    from app.db.engine import session_scope

    class _Agent:
        async def ainvoke(self, payload):  # noqa: ANN001
            return {"messages": list(payload["messages"]) + [AIMessage(content="没找到你说的那份档案。")]}

    async def _meta():
        return "口径", 1

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda *a, **k: _Agent())
    monkeypatch.setattr(chat_agent, "_metadata_text", _meta)

    from app.agents import chat_store

    with session_scope() as session:
        row = chat_store.create_session(session, "admin")
        session_id = row.id

    from app.memory.runtime import chat_turn_context

    with chat_turn_context(session_id=session_id, include_query_plan=False):
        result = asyncio.run(chat_agent.run_chat(
            AuthUser(username="admin", role="admin", department=None), [],
            "@session 00000000-0000-0000-0000-000000000000 这份结论还用吗"))

    ref = result["run_detail"]["references"]["session_reference"]
    assert ref["status"] == "not_found", ref
    assert ref["token"] == "00000000-0000-0000-0000-000000000000"
    assert ref["count"] == 0
    assert result["session_references"] == []
