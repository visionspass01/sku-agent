"""Agent 交接契约测试：字段校验 + trace 输出 + supervisor 真的会写进 trace。"""

from __future__ import annotations

import asyncio

import pytest


def test_contract_requires_reason_and_expected():
    from app.agents import handoff

    with pytest.raises(handoff.HandoffError):
        handoff.start("supervisor", "collector", reason="", expected="x")
    with pytest.raises(handoff.HandoffError):
        handoff.start("supervisor", "collector", reason="派发", expected="")


def test_contract_rejects_self_handoff():
    from app.agents import handoff

    with pytest.raises(handoff.HandoffError):
        handoff.start("reasoner", "reasoner", reason="自问自答", expected="x")


def test_contract_trace_shape():
    from app.agents import handoff

    contract = handoff.start(
        "supervisor", "collector", reason="派发采集子集",
        payload={"tasks": ["erp"], "sku_id": "ST-01-C1-M"},
        expected="erp 原始数据", trace_id="run-1",
    )
    trace = contract.to_trace(elapsed_ms=12)
    assert trace["node"] == "handoff:supervisor->collector"
    assert "派发采集子集" in trace["detail"] and "erp 原始数据" in trace["detail"]
    assert trace["trace_id"] == "run-1"
    assert trace["ms"] == 12


def test_supervisor_emits_handoff_in_state(seeded, monkeypatch):
    """supervisor 节点要真的把 handoff 写进 state["trace"]（不是摆设）。"""
    from app.agents import supervisor as sup
    from app.agents.schemas import SupervisorPlan

    class _LLM:
        async def invoke_json(self, system, user, schema, max_tokens=None):
            return SupervisorPlan(tasks=["erp"], priority="中", note="ok")

    monkeypatch.setattr(sup, "get_llm", lambda: _LLM())
    state = {"sku_id": "ST-92-C1-M", "run_id": "run-x", "as_of": "2026-11-04"}
    update = asyncio.run(sup.supervisor_node(state))
    nodes = [t["node"] for t in update["trace"]]
    assert "handoff:supervisor->collector" in nodes
    assert update["supervisor"]["tasks"]
