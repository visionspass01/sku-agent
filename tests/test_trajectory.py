"""Trajectory 指标（E6）单测：把「指标定义」钉死在测试里。

为什么值得单独测：这些指标是**代理指标**（重复调用用截断入参做键、追问成功率是流程成功率），
定义一旦漂移（比如把 0 轮当达标、把不可判定的老轮次算成失败），看板上就会给出误导性结论。
"""

from __future__ import annotations

from app.agents.trajectory import aggregate, trajectory_of


def _turn(agent="analyst", steps=None, llm_calls=2, grounding="passed", guard=None,
          session="s1", created="2026-01-01 00:00", error_code=""):
    payload = {
        "steps": steps or [],
        "llm": {"calls": llm_calls, "cost_cny": 0.01},
        "agent": {"id": agent},
    }
    if guard is not None:
        payload["guard"] = guard
    if error_code:
        payload["error_code"] = error_code
    return {"session_id": session, "agent_id": agent, "grounding_status": grounding,
            "payload": payload, "created_at": created}


def _step(tool: str, args: str = "") -> dict:
    return {"tool": tool, "args": args, "status": "ok"}


def test_trajectory_of_counts_repeats_by_tool_and_args():
    payload = {
        "steps": [_step("get_inventory", '{"sku":"A"}'), _step("get_inventory", '{"sku":"A"}'),
                  _step("get_inventory", '{"sku":"B"}'), _step("get_sales", '{"sku":"A"}')],
        "llm": {"calls": 4, "cost_cny": 0.02},
        "guard": {"max_calls": 15, "success_calls": 4, "business_errors": 1},
        "agent": {"id": "diagnostician"},
    }
    metrics = trajectory_of(payload)
    assert metrics["tool_calls"] == 4
    assert metrics["distinct_tools"] == 2
    assert metrics["repeat_calls"] == 1          # 只有重复的那一次算
    assert metrics["has_repeats"] is True
    assert metrics["success_calls"] == 4 and metrics["max_calls"] == 15
    assert metrics["business_errors"] == 1
    assert metrics["llm_calls"] == 4
    assert metrics["agent_id"] == "diagnostician"
    assert metrics["aborted"] is False


def test_trajectory_of_tolerates_missing_fields():
    """老轮次 payload 可能没有 llm/guard/agent：不能抛，只能给保守值。"""
    metrics = trajectory_of({})
    assert metrics["tool_calls"] == 0
    assert metrics["llm_calls"] == 0
    assert metrics["success_calls"] is None and metrics["max_calls"] is None
    assert metrics["aborted"] is False


def test_aggregate_percentiles_and_rates():
    rows = [
        _turn(steps=[_step("a"), _step("b")], grounding="passed"),
        _turn(steps=[_step("a")], grounding="skipped"),
        _turn(steps=[], grounding="degraded"),
        _turn(steps=[_step("a"), _step("a")], grounding="aborted", error_code="TOOL_LOOP_REPEAT",
              guard={"max_calls": 15, "success_calls": 3, "business_errors": 0}),
    ]
    data = aggregate(rows, hours=24)
    assert data["turns"] == 4
    assert data["aborted_turns"] == 1
    assert data["abort_rate"] == 0.25
    assert data["tool_calls_per_turn"]["max"] == 2
    assert data["repeat_calls"] == 1
    assert data["turns_with_repeats"] == 1
    assert data["turns_with_guard"] == 1
    assert data["budget_utilization"] == 0.2     # 3/15
    assert data["error_codes"] == {"TOOL_LOOP_REPEAT": 1}
    assert data["top_tools"][0]["tool"] == "a"


def test_aggregate_multi_turn_success_only_counts_judged_followups():
    """追问成功率只对「有 grounding 状态」的追问计算；不可判定的单独计数。"""
    rows = [
        _turn(grounding="passed", session="s1", created="2026-01-01 00:00"),
        _turn(grounding="passed", session="s1", created="2026-01-01 00:05"),
        _turn(grounding="", session="s1", created="2026-01-01 00:10"),      # 老轮次：不可判定
        _turn(grounding="passed", session="s2", created="2026-01-02 00:00"),
        _turn(grounding="degraded", session="s2", created="2026-01-02 00:03"),  # 追问降级 → 失败
    ]
    multi = aggregate(rows, hours=24)["multi_turn"]
    assert multi["sessions"] == 2
    assert multi["followup_turns"] == 3
    assert multi["followup_judged"] == 2
    assert multi["followup_unjudged"] == 1
    assert multi["followup_success_rate"] == 0.5    # passed 算成功，degraded 算失败


def test_aggregate_empty_window_reports_zero_turns():
    data = aggregate([], hours=24)
    assert data["turns"] == 0
    assert data["abort_rate"] == 0.0
    assert data["tool_calls_per_turn"]["max"] == 0
    assert data["multi_turn"]["followup_success_rate"] is None


def test_aborted_turn_counts_tool_calls_from_ledger_column():
    """复审 F1：中止轮次的 payload 没有 steps，工具数要取自台账列，不能算成 0。"""
    rows = [
        {"session_id": "s1", "agent_id": "analyst", "grounding_status": "aborted",
         "tool_calls": 3,
         "payload": {"error_code": "TOOL_LOOP_REPEAT",
                     "guard": {"max_calls": 15, "success_calls": 3, "business_errors": 0}},
         "created_at": "2026-01-01 00:00"},
        _turn(steps=[_step("a")], grounding="passed", session="s2"),
    ]
    data = aggregate(rows, hours=24)
    assert data["turns"] == 2
    assert data["tool_calls_per_turn"]["max"] == 3
    assert data["tool_calls_per_turn"]["avg"] == 2.0        # (3 + 1) / 2
    assert data["aborted_turns"] == 1
    assert data["budget_utilization"] == 0.2               # 3/15，只算带 guard 的轮次


def test_trajectory_of_prefers_payload_steps_over_ledger_column():
    """台账列是下界：payload 里的 steps 更全时不能被它带偏。"""
    payload = {"steps": [_step("a"), _step("b"), _step("c"), _step("d")],
               "llm": {"calls": 2}, "agent": {"id": "analyst"}}
    metrics = trajectory_of(payload, row_tool_calls=1)
    assert metrics["tool_calls"] == 4


def test_followup_metric_treats_disabled_and_error_as_unjudged():
    """复审 N1：grounding 关掉（disabled）或校验器故障（error）是「没判」，不是失败。"""
    for status in ("disabled", "error"):
        rows = [
            _turn(grounding="passed", session="s1", created="2026-01-01 00:00"),
            _turn(grounding=status, session="s1", created="2026-01-01 00:05"),
        ]
        multi = aggregate(rows, hours=24)["multi_turn"]
        assert multi["followup_turns"] == 1, status
        assert multi["followup_judged"] == 0, status
        assert multi["followup_unjudged"] == 1, status
        assert multi["followup_success_rate"] is None, status


def test_followup_metric_still_counts_degraded_as_failure():
    """degraded 是「判了但没通过」——必须留在分母里当失败，不能当成未判定。"""
    rows = [
        _turn(grounding="passed", session="s1", created="2026-01-01 00:00"),
        _turn(grounding="degraded", session="s1", created="2026-01-01 00:05"),
    ]
    multi = aggregate(rows, hours=24)["multi_turn"]
    assert multi["followup_judged"] == 1
    assert multi["followup_unjudged"] == 0
    assert multi["followup_success_rate"] == 0.0


def test_per_agent_breakdown():
    rows = [
        _turn(agent="analyst", steps=[_step("a")]),
        _turn(agent="analyst", steps=[_step("a"), _step("b")]),
        _turn(agent="diagnostician", steps=[_step("a"), _step("a"), _step("a")]),
    ]
    per_agent = aggregate(rows, hours=24)["per_agent"]
    assert per_agent["analyst"]["turns"] == 2
    assert per_agent["analyst"]["avg_tools"] == 1.5
    assert per_agent["diagnostician"]["repeat_call_rate"] == round(2 / 3, 4)
