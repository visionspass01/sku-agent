"""空答复不再算成功（复审 R3）：对话侧打标记、trajectory 不计成功、判定器判 fail。"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage

SKU = "ST-91-C1-M"


class _EmptyAnswerModel:
    """模型只回一个空 AIMessage（模拟"参数非法/max_tokens 用尽"等没正文的情形）。"""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

    def bind(self, **kwargs):  # noqa: ANN003
        return self

    async def ainvoke(self, payload):  # noqa: ANN001
        return {"messages": list(payload["messages"]) + [AIMessage(content="")]}


def test_empty_reply_is_marked_and_uses_fallback(seeded, monkeypatch, _settings):
    """run_chat：模型不给正文 → 兜底文案 + `answer.is_fallback=True` + `reply_empty=True`。"""
    from app.agents import chat_agent
    from app.agents.grounding import EMPTY_REPLY_FALLBACK
    from app.auth import AuthUser

    monkeypatch.setattr(_settings, "grounding_enabled", True)
    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda *a, **k: _EmptyAnswerModel())

    async def _meta():
        return "口径", 1

    monkeypatch.setattr(chat_agent, "_metadata_text", _meta)
    user = AuthUser(username="admin", role="admin", department=None)
    result = asyncio.run(chat_agent.run_chat(user, [], f"{SKU} 库存？"))

    assert result["reply"] == EMPTY_REPLY_FALLBACK
    assert result["reply_empty"] is True
    detail = result["run_detail"]
    assert detail["answer"]["is_fallback"] is True
    assert detail["answer"]["chars"] == len(EMPTY_REPLY_FALLBACK)
    # 空答复没有数字可校验：grounding 标 skipped 并注明原因（不是"通过"，也不是"故障"）
    assert detail["grounding"]["status"] == "skipped"
    assert "未给出正文" in detail["grounding"].get("note", "")


def test_normal_answer_is_not_marked(seeded, monkeypatch, _settings):
    """对照组：正常答复不会被标成 fallback（避免把好轮次也算失败）。"""
    from app.agents import chat_agent
    from app.auth import AuthUser

    class _Good(_EmptyAnswerModel):
        async def ainvoke(self, payload):  # noqa: ANN001
            return {"messages": list(payload["messages"]) + [AIMessage(content="可用库存 510 件。")]}

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda *a, **k: _Good())

    async def _meta():
        return "口径", 1

    monkeypatch.setattr(chat_agent, "_metadata_text", _meta)
    result = asyncio.run(chat_agent.run_chat(
        AuthUser(username="admin", role="admin", department=None), [], f"{SKU} 库存？"))

    assert result["reply"] == "可用库存 510 件。"
    assert result["reply_empty"] is False
    assert result["run_detail"]["answer"]["is_fallback"] is False


def test_trajectory_counts_fallback_turn_as_not_successful():
    """trajectory：兜底文案的追问轮次不进成功分子，并单独计数。"""
    from app.agents.trajectory import aggregate

    rows = [
        {"session_id": "s1", "grounding_status": "passed",
         "payload": {"steps": [], "answer": {"is_fallback": False}},
         "agent_id": "analyst", "tool_calls": 0, "created_at": "2026-01-01T00:00:00"},
        {"session_id": "s1", "grounding_status": "skipped",
         "payload": {"steps": [], "answer": {"is_fallback": True}},
         "agent_id": "analyst", "tool_calls": 0, "created_at": "2026-01-01T00:01:00"},
        {"session_id": "s1", "grounding_status": "passed",
         "payload": {"steps": [], "answer": {"is_fallback": False}},
         "agent_id": "analyst", "tool_calls": 0, "created_at": "2026-01-01T00:02:00"},
    ]
    agg = aggregate(rows, hours=24)

    assert agg["answer_fallback_turns"] == 1
    multi = agg["multi_turn"]
    assert multi["followup_turns"] == 2          # 两轮追问
    assert multi["followup_judged"] == 2
    assert multi["followup_fallback"] == 1
    # 两轮追问里只有一轮真的答了 → 成功率 50%，而不是 100%
    assert multi["followup_success_rate"] == 0.5


def test_trajectory_tolerates_old_payload_without_answer_section():
    """老台账没有 `answer` 段：不能报错，也不能把历史轮次都当失败。"""
    from app.agents.trajectory import aggregate, trajectory_of

    metric = trajectory_of({"steps": []}, grounding_status="passed")
    assert metric["is_fallback_answer"] is False
    agg = aggregate([{"session_id": "s1", "grounding_status": "passed",
                      "payload": {"steps": []}, "agent_id": "analyst",
                      "tool_calls": 0, "created_at": "2026-01-01T00:00:00"}], hours=24)
    assert agg["answer_fallback_turns"] == 0


def test_judge_flags_fallback_answer():
    """判定器：兜底文案/空正文判 fail（新增 answer 维度）。"""
    from app.agents.answer_eval import judge
    from app.agents.grounding import EMPTY_REPLY_FALLBACK

    trace = [
        {"type": "call", "tool": "get_inventory", "args": {"sku_id": SKU}},
        {"type": "result", "tool": "get_inventory", "result": '{"available": 510}'},
    ]
    steps = [{"tool": "get_inventory", "result_id": "res_aaa111bbb2"}]
    case = {"expect": {"tools": ["get_inventory"], "must_answer": True}}

    fallback = judge(case, EMPTY_REPLY_FALLBACK, trace, steps)
    assert fallback["passed"] is False
    assert fallback["checks"]["answer"] is False
    assert any("没有有效答复" in i for i in fallback["issues"])

    blank = judge(case, "", trace, steps)
    assert blank["checks"]["answer"] is False

    # run_detail 里标了 fallback 也算（即便文案被改写）
    marked = judge(case, "随便一句话", trace, steps,
                   run_detail={"answer": {"is_fallback": True}})
    assert marked["checks"]["answer"] is False

    # 正常答复通过
    ok = judge(case, "可用库存 510 件（res_aaa111bbb2）。", trace, steps)
    assert ok["checks"]["answer"] is True and ok["passed"] is True


def test_judge_can_opt_out_of_must_answer():
    from app.agents.answer_eval import judge

    case = {"expect": {"tools": [], "must_answer": False, "must_cite": False}}
    verdict = judge(case, "", [], [])
    assert "answer" not in verdict["checks"]
    assert verdict["passed"] is True
