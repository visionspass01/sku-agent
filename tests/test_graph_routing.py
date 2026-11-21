"""Supervisor 路由是否真实生效：未派发的 source 不发起 MCP 调用。

背景：重构前三路 `Send` 恒 fan-out（LangGraph join 语义），"按需派发"只在节点内部跳过，
从 trace 与调用次数上都看不出路由是否真的生效，评审时无法自证。
改成 `collector` 单节点内按 `tasks` 并发采集后，`tasks=["erp"]` 时竞品工具调用数必须为 0。
"""

from __future__ import annotations

import asyncio

from fakes import FakeLLM

from app.db import repos
from app.db.engine import session_scope
from app.pipeline.patrol import inspect_sku


def _patch_llm(fake: FakeLLM) -> None:
    import app.llm as llm_mod

    llm_mod._llm = fake


def _count_mcp_calls(monkeypatch) -> dict:
    from app.mcp_client import client as mcp_client

    counter = {"erp": 0, "pailitao": 0, "tools": []}
    original = mcp_client.MCPHub.call

    async def counting(self, server, tool, arguments=None):
        counter[server] = counter.get(server, 0) + 1
        counter["tools"].append(f"{server}.{tool}")
        return await original(self, server, tool, arguments)

    monkeypatch.setattr(mcp_client.MCPHub, "call", counting)
    return counter


def _inspect(sku_id: str) -> dict:
    async def run():
        with session_scope() as session:
            run = repos.create_run(session, kind="single", params={"sku_id": sku_id})
            run_id = run.id
        return await inspect_sku(sku_id, run_id)

    return asyncio.run(run())


def test_supervisor_skip_market_really_skips_calls(seeded, monkeypatch):
    _patch_llm(FakeLLM(passed=True, supervisor_tasks=["erp"]))
    counter = _count_mcp_calls(monkeypatch)

    result = _inspect("ST-90-C1-M")
    assert result["status"] == "ok", result.get("error")
    assert counter["erp"] > 0, "ERP 必选，必须真实调用"
    assert counter["pailitao"] == 0, f"未派发竞品却调用了：{counter['tools']}"

    nodes = [t.get("node") for t in (result["final_state"].get("trace") or [])]
    assert "market_worker" not in nodes, nodes
    assert "erp_worker" in nodes and "rag_worker" in nodes, nodes
    # collector 的 trace 明确写出本轮采了哪些 source
    detail = next(t.get("detail") for t in result["final_state"]["trace"] if t.get("node") == "collector")
    assert "erp" in str(detail) and "market" not in str(detail), detail


def test_supervisor_full_dispatch_runs_market(seeded, monkeypatch):
    _patch_llm(FakeLLM(passed=True, supervisor_tasks=["erp", "market", "rag"]))
    counter = _count_mcp_calls(monkeypatch)

    result = _inspect("ST-90-C1-M")
    assert result["status"] == "ok", result.get("error")
    assert counter["pailitao"] > 0, "派发了竞品就必须真实调用"
    nodes = [t.get("node") for t in (result["final_state"].get("trace") or [])]
    assert "market_worker" in nodes, nodes


def test_collector_always_includes_rag_for_citations(seeded, monkeypatch):
    """即使 Supervisor 只派发 erp，RAG 也要跑：verifier 要求 ≥2 条 SOP 引用。"""
    _patch_llm(FakeLLM(passed=True, supervisor_tasks=["erp"]))
    counter = _count_mcp_calls(monkeypatch)
    result = _inspect("ST-90-C1-M")
    assert result["status"] == "ok"
    plan = result["final_state"].get("plan") or {}
    assert len(plan.get("citations") or []) >= 2
    assert counter["pailitao"] == 0


# ---------------------------------------------------------------- P2-6：逐源隔离
def _fail_source(monkeypatch, name: str, exc: Exception) -> None:
    """把某个采集源换成「一定抛错」的桩（_WORKER_FNS 在 import 时就绑定了函数引用）。"""
    from app.agents import graph as graph_mod

    async def _boom(_payload):
        raise exc

    monkeypatch.setitem(graph_mod._WORKER_FNS, name, _boom)


def test_market_failure_degrades_instead_of_failing_the_run(seeded, monkeypatch):
    """竞品源挂了：erp 已经采到的数据不能陪葬，整轮标 degraded 继续。"""
    _patch_llm(FakeLLM(passed=True, supervisor_tasks=["erp", "market", "rag"]))
    _count_mcp_calls(monkeypatch)
    _fail_source(monkeypatch, "market", RuntimeError("拍立淘不可用"))

    result = _inspect("ST-90-C1-M")
    assert result["status"] in {"ok", "degraded"}, result.get("error")
    final = result["final_state"]
    assert final.get("degraded") is True
    nodes = [(t.get("node"), t.get("status")) for t in (final.get("trace") or [])]
    assert ("market_worker", "failed") in nodes, nodes
    assert (final.get("data") or {}).get("erp"), "ERP 数据不该被竞品失败带走"


def test_rag_failure_degrades_and_verifier_flags_citations(seeded, monkeypatch):
    _patch_llm(FakeLLM(passed=True, supervisor_tasks=["erp", "rag"]))
    _count_mcp_calls(monkeypatch)
    _fail_source(monkeypatch, "rag", RuntimeError("知识库不可用"))

    result = _inspect("ST-90-C1-M")
    assert result["status"] == "degraded", result.get("error")
    final = result["final_state"]
    issues = " ".join((final.get("verification") or {}).get("issues") or [])
    assert "引用" in issues, issues


def test_erp_failure_keeps_run_failed_but_with_clear_reason(seeded, monkeypatch):
    """ERP 是基准数据：缺失时宁可不给结论，但错误要说清楚是哪个源、为什么。"""
    _patch_llm(FakeLLM(passed=True, supervisor_tasks=["erp", "market", "rag"]))
    _count_mcp_calls(monkeypatch)
    _fail_source(monkeypatch, "erp", ValueError("ERP 未返回 SKU 数据: ST-90-C1-M"))

    result = _inspect("ST-90-C1-M")
    assert result["status"] == "failed"
    assert "ERP 采集失败" in str(result.get("error") or ""), result.get("error")
