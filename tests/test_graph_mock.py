"""LangGraph 主图集成测试（FakeLLM + direct MCP + fakeredis，不依赖网络）。"""

from __future__ import annotations

import asyncio

from fakes import FakeLLM

from app.agents.graph import build_inspection_graph
from app.db import repos
from app.db.engine import session_scope
from app.pipeline.patrol import inspect_sku


def _patch_llm(fake: FakeLLM) -> None:
    import app.llm as llm_mod

    llm_mod._llm = fake


def test_single_sku_end_to_end_with_fake_llm(seeded):
    fake = FakeLLM(passed=True)
    _patch_llm(fake)

    async def run():
        with session_scope() as session:
            run = repos.create_run(session, kind="single", params={"sku_id": "ST-90-C1-M"})
            run_id = run.id
        result = await inspect_sku("ST-90-C1-M", run_id)
        with session_scope() as session:
            rows = repos.get_results(session, run_id)
            captured = [
                {"verifier": r.verifier, "plan": r.plan, "data_bundle": r.data_bundle}
                for r in rows
            ]
        return result, captured

    result, rows = asyncio.run(run())
    assert result["status"] == "ok", result.get("error")
    assert len(rows) == 1
    row = rows[0]
    assert row["verifier"]["passed"] is True
    assert row["plan"]["citations"] and len(row["plan"]["citations"]) >= 2
    trace = [t["node"] for t in (row["data_bundle"].get("trace") or [])]
    for node in ("supervisor", "erp_worker", "market_worker", "rag_worker", "merge", "reasoner", "verifier"):
        assert node in trace


def test_verifier_failure_leads_to_degraded(seeded):
    _patch_llm(FakeLLM(passed=False))

    async def run():
        with session_scope() as session:
            run = repos.create_run(session, kind="single", params={"sku_id": "ST-90-C1-M"})
            run_id = run.id
        return await inspect_sku("ST-90-C1-M", run_id)

    result = asyncio.run(run())
    assert result["status"] == "degraded"
    assert (result["final_state"].get("verification") or {}).get("passed") is False


def test_rag_worker_propagates_conflict_fields(seeded):
    """批量链路也要把冲突裁决带进 state（否则巡检 run_detail/审计看不到取舍依据）。"""
    from app.agents.workers import rag_worker

    state = {"sku_id": "ST-90-C1-M", "tasks": ["rag"], "rag_query": "清仓 SOP 触发条件"}
    out = asyncio.run(rag_worker(state))
    rag = out["data"]["rag"]
    assert "conflicts" in rag
    assert "conflict_check" in rag
    assert "conflict_diagnostics" in rag
    assert rag["context"]
    # trace 里能看出有没有冲突（排障时不用翻 context 全文）
    assert "conflicts=" in out["trace"][0]["detail"]


def test_graph_compiles_with_skill_subgraphs():
    graph = build_inspection_graph()
    assert graph is not None
