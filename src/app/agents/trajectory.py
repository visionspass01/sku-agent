"""Trajectory 指标（E6）：把「一轮对话怎么走的」变成可统计的数字。

为什么要有这一层：早先 agent 侧的能量化只有 `avg_tools_per_turn`（`chat_store.agent_turn_stats`），
回答不了评测/排障真正要问的几个问题——
「模型是不是在反复调同一个工具？」「这轮用掉了多少预算？」「循环守护中止了多少轮？」
「多轮追问到底成不成功？」。

指标来源：`chat_turn_log.payload`（就是每轮的 `run_detail`），**纯函数推导**、不额外落库、
不依赖模型——所以能像检索/冲突题集一样当回归口径。

口径与代理指标（诚实标注）：
- `repeat_calls` 用 `steps[].args`（tool_contract 里 200 字截断的入参串）做键 → 长参数可能被截成
  同串，属**代理指标**；工具循环守护用的是完整入参指纹（`tool_guard.fingerprint`），两者不同源。
- `llm_calls` 优先取 `payload.llm.calls`，缺失时退回 `calls_detail` 条数 / timeline 中 `kind=llm` 的条数。
- `aborted` 轮次来自 `chat_turn_log.grounding_status == "aborted"`（循环/预算中止时由路由补写一行），
  因此能跨重启统计；进程内 `tool_stats.outcomes.loop_aborted` 是另一份口径，展示时并列。
- 多轮成功率的分母是「非首轮」的轮次；成功 = `grounding_status ∈ {passed, skipped, repaired}`
  且未中止。它是**流程成功**，不是"业务上答对了"（后者需要人工标注，不在本指标里）。
"""

from __future__ import annotations

from collections import Counter

# 流程意义上的「成功」出口状态（与 grounding 的三态一致）
SUCCESS_GROUNDING = frozenset({"passed", "skipped", "repaired"})
ABORTED_GROUNDING = "aborted"
# 「没判」而不是「失败」的出口状态：空串=老轮次没有该字段；disabled=本轮关掉 grounding；
# error=校验器故障。三者都进 `followup_unjudged`，不进成功率分母（复审 N1）。
UNJUDGED_GROUNDING = frozenset({"", "disabled", "error"})


def _percentile(values: list[float], ratio: float) -> float:
    """排序取分位（与 chat_store._percentile 同口径，避免两套算法）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(float(ordered[0]), 2)
    import math

    idx = max(0, math.ceil(ratio * len(ordered)) - 1)
    return round(float(ordered[idx]), 2)


def _timeline_llm_calls(payload: dict) -> int:
    timeline = payload.get("timeline") or []
    return sum(1 for item in timeline if isinstance(item, dict) and item.get("kind") == "llm")


def trajectory_of(payload: dict | None, *, grounding_status: str = "",
                  row_tool_calls: int | None = None) -> dict:
    """单轮轨迹：工具/步数/重复调用/预算/中止/耗时成本。缺字段一律容错。

    `row_tool_calls`：`chat_turn_log.tool_calls` 列的值。**中止轮次**的 payload 是
    "中止台账"样式（只有 `error_code` + `guard`，没有 `steps`），只数 payload 会把它们
    算成"没用过工具"——而中止前实际已经调过 N 次。所以取 `max(len(steps), 台账数)`
    （台账数只算成功，是下界，不会夸大）。
    """
    payload = payload or {}
    steps = [s for s in (payload.get("steps") or []) if isinstance(s, dict)]
    tools = [str(s.get("tool") or "") for s in steps if s.get("tool")]
    keys = [(str(s.get("tool") or ""), str(s.get("args") or "")) for s in steps if s.get("tool")]
    repeated = sum(count - 1 for count in Counter(keys).values() if count > 1)
    tool_calls = max(len(tools), int(row_tool_calls or 0))
    llm = payload.get("llm") or {}
    llm_calls = int(llm.get("calls") or 0)
    if not llm_calls:
        llm_calls = len(llm.get("calls_detail") or []) or _timeline_llm_calls(payload)
    guard = payload.get("guard") or {}
    answer = payload.get("answer") or {}
    aborted = str(grounding_status or "") == ABORTED_GROUNDING
    return {
        "tool_calls": tool_calls,
        "distinct_tools": len(set(tools)),
        "llm_calls": llm_calls,
        "repeat_calls": repeated,
        "has_repeats": repeated > 0,
        "success_calls": int(guard.get("success_calls") or 0) if guard else None,
        "max_calls": int(guard.get("max_calls") or 0) if guard else None,
        "business_errors": int(guard.get("business_errors") or 0) if guard else None,
        "aborted": aborted,
        # 复审 R3：模型没给正文（兜底文案）的轮次。grounding 会是 skipped，
        # 单看 grounding 会把它误判成"成功"，所以必须单独标记。
        "is_fallback_answer": bool(answer.get("is_fallback")),
        "error_code": str(payload.get("error_code") or ""),
        "elapsed_ms": int(payload.get("elapsed_ms") or 0),
        "cost_cny": float(llm.get("cost_cny") or 0.0),
        "agent_id": str((payload.get("agent") or {}).get("id") or ""),
        "tools": tools,
    }


def aggregate(rows: list[dict], *, hours: int = 24) -> dict:
    """窗口聚合：入参是 `{payload, grounding_status, agent_id?, created_at?, session_id?}` 列表。

    行可以由 ORM 行转换（见 `chat_store.load_turn_rows`）或手工构造（单测）。
    """
    turns: list[dict] = []
    for row in rows:
        payload = row.get("payload") or {}
        metrics = trajectory_of(payload, grounding_status=str(row.get("grounding_status") or ""),
                                row_tool_calls=row.get("tool_calls"))
        metrics["agent_id"] = metrics["agent_id"] or str(row.get("agent_id") or "")
        metrics["session_id"] = str(row.get("session_id") or "")
        metrics["created_at"] = row.get("created_at")
        metrics["grounding_status"] = str(row.get("grounding_status") or "")
        turns.append(metrics)

    total = len(turns)
    tool_counts = [t["tool_calls"] for t in turns]
    llm_counts = [t["llm_calls"] for t in turns]
    total_tools = sum(tool_counts)
    total_repeat = sum(t["repeat_calls"] for t in turns)
    aborted = [t for t in turns if t["aborted"]]
    with_guard = [t for t in turns if t["max_calls"]]
    utilization = [min(1.0, t["success_calls"] / t["max_calls"]) for t in with_guard
                   if t["max_calls"]]

    tool_name_counter: Counter[str] = Counter()
    for turn in turns:
        tool_name_counter.update(turn["tools"])

    per_agent: dict[str, dict] = {}
    for turn in turns:
        key = turn["agent_id"] or "(unrouted)"
        bucket = per_agent.setdefault(key, {"turns": 0, "tools": 0, "p95_values": [],
                                            "repeats": 0, "aborted": 0})
        bucket["turns"] += 1
        bucket["tools"] += turn["tool_calls"]
        bucket["p95_values"].append(turn["tool_calls"])
        bucket["repeats"] += turn["repeat_calls"]
        bucket["aborted"] += 1 if turn["aborted"] else 0
    per_agent_out = {
        key: {
            "turns": value["turns"],
            "avg_tools": round(value["tools"] / max(1, value["turns"]), 2),
            "p95_tools": _percentile(value["p95_values"], 0.95),
            "repeat_call_rate": round(value["repeats"] / max(1, value["tools"]), 4),
            "abort_rate": round(value["aborted"] / max(1, value["turns"]), 4),
        }
        for key, value in sorted(per_agent.items())
    }

    sessions: dict[str, list[dict]] = {}
    for turn in turns:
        turned = sessions.setdefault(turn["session_id"] or "-", [])
        turned.append(turn)
    multi_sessions = {sid: items for sid, items in sessions.items() if len(items) >= 2}
    followups = []
    for items in multi_sessions.values():
        ordered = sorted(items, key=lambda t: str(t.get("created_at") or ""))
        followups.extend(ordered[1:])
    # 追问成功率只看**可判定**的轮次：老轮次没有 grounding 状态（空串）、本轮 grounding 被关掉
    # （`disabled`）、校验器自身故障（`error`）都属于"没判"，不能算成失败——否则关掉
    # grounding 的部署会看到"追问成功率 0%"，那是误导（复审 N1）。
    judged = [t for t in followups if t["grounding_status"] not in UNJUDGED_GROUNDING]
    # 复审 R3：模型没给正文（兜底文案）的轮次不算成功——即使 grounding 判了 skipped。
    # 之前这类轮次被算成"追问成功"，把追问成功率抬高了。
    followup_fallback = [t for t in judged if t["is_fallback_answer"]]
    followup_ok = [t for t in judged
                   if not t["aborted"] and not t["is_fallback_answer"]
                   and t["grounding_status"] in SUCCESS_GROUNDING]

    return {
        "window_hours": int(hours),
        "turns": total,
        "aborted_turns": len(aborted),
        "abort_rate": round(len(aborted) / total, 4) if total else 0.0,
        # 复审 R3：模型没给出正文（兜底文案）的轮次——它不计入成功，也不算"正常轮次"
        "answer_fallback_turns": sum(1 for t in turns if t["is_fallback_answer"]),
        "tool_calls_per_turn": {
            "avg": round(total_tools / total, 2) if total else 0.0,
            "p50": _percentile(tool_counts, 0.50),
            "p95": _percentile(tool_counts, 0.95),
            "max": max(tool_counts) if tool_counts else 0,
        },
        "llm_calls_per_turn": {
            "avg": round(sum(llm_counts) / total, 2) if total else 0.0,
            "p50": _percentile(llm_counts, 0.50),
            "p95": _percentile(llm_counts, 0.95),
            "max": max(llm_counts) if llm_counts else 0,
        },
        "repeat_calls": total_repeat,
        "repeat_call_rate": round(total_repeat / total_tools, 4) if total_tools else 0.0,
        "turns_with_repeats": sum(1 for t in turns if t["has_repeats"]),
        "turns_with_guard": len(with_guard),
        "budget_utilization": round(sum(utilization) / len(utilization), 4) if utilization else 0.0,
        "business_errors": sum(t["business_errors"] or 0 for t in with_guard),
        "error_codes": dict(Counter(t["error_code"] for t in aborted if t["error_code"])),
        "per_agent": per_agent_out,
        "top_tools": [{"tool": name, "calls": calls,
                       "share": round(calls / total_tools, 4) if total_tools else 0.0}
                      for name, calls in tool_name_counter.most_common(8)],
        "multi_turn": {
            "sessions": len(multi_sessions),
            "followup_turns": len(followups),
            "followup_judged": len(judged),
            "followup_unjudged": len(followups) - len(judged),
            "followup_fallback": len(followup_fallback),
            "followup_success_rate": (round(len(followup_ok) / len(judged), 4)
                                      if judged else None),
        },
        "cost_cny": round(sum(t["cost_cny"] for t in turns), 4),
        "note": ("按 chat_turn_log 聚合：repeat_calls 用截断入参做键（代理指标）；"
                 "aborted_turns 只统计已持久化的循环/预算中止；"
                 "followup_success_rate 是流程成功率（grounding 通过且未中止、且模型真的给出了正文），"
                 "不是业务正确率；answer_fallback_turns 记「模型没给正文/兜底文案」的轮次，"
                 "它们不进成功分子；grounding=disabled/error 视为未判定（进 followup_unjudged，不进分母）"),
    }
