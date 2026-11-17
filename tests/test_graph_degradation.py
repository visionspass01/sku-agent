"""图节点降级一致性（B3）：LLM 网络故障只能降级，不能把整条巡检打挂。

回归背景：`reasoner_node` 捕获 `Exception` 走规则模板降级（写得对），但
`supervisor_node` / `verifier_node` 只捕获 `LLMError`——而 `LLMError` 只在
「JSON 修复失败」时抛。于是 LLM 网络抖动会让这两个节点直接抛异常，
整个巡检 run 失败，与注释「LLM 校验不可用，仅执行规则校验」完全不符。
"""

from __future__ import annotations

import asyncio


class _TransportDown:
    async def invoke_json(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise ConnectionError("connection reset by peer")


def _patch_llm(monkeypatch, module):
    monkeypatch.setattr(module, "get_llm", lambda: _TransportDown())


def test_verifier_degrades_to_rules_only(monkeypatch, _settings):
    from app.agents import verifier

    _patch_llm(monkeypatch, verifier)
    state = {
        "plan": {"citations": ["c1"], "evidence": ["a"], "plan": [{"action": "观察"}]},
        "data": {"rag": {"citations": [{"chunk_id": "c1"}]}},
        "judgment": {},
        "replenish": {"suggested_qty": 0},
    }

    out = asyncio.run(verifier.verifier_node(state))

    verification = out["verification"]
    assert verification["llm_passed"] is None
    # 复审 P4：「LLM 校验不可用」是**提醒**（warnings），不是"不合规"（issues）——
    # 否则规则校验全过时会出现 passed=true 却 issues 非空的自相矛盾。
    assert any("LLM 校验不可用" in w for w in verification["warnings"])
    assert all("LLM 校验不可用" not in issue for issue in verification["issues"])
    assert verification["passed"] is False          # 规则校验没过（引用不足 2 条）
    assert out["trace"][0]["status"] == "failed"


def test_verifier_llm_down_but_rules_pass_stays_clean(monkeypatch, _settings):
    """规则全过 + LLM 不可用：passed=True 且 issues 必须为空（提醒只进 warnings）。"""
    from app.agents import verifier

    _patch_llm(monkeypatch, verifier)
    state = {
        "plan": {
            "citations": ["c1", "c2"],
            "evidence": ["可用库存 10 件", "日均 2 件", "在途 0 件"],
            "plan": [{"action": "观察", "qty": None, "reason": "无风险"}],
        },
        "data": {"rag": {"citations": [{"chunk_id": "c1"}, {"chunk_id": "c2"}]}},
        "judgment": {"risk_flags": []},
        "replenish": {"suggested_qty": 0},
    }

    out = asyncio.run(verifier.verifier_node(state))

    verification = out["verification"]
    assert verification["passed"] is True
    assert verification["issues"] == []
    assert any("LLM 校验不可用" in w for w in verification["warnings"])
    assert out["trace"][0]["status"] == "ok"


def test_supervisor_degrades_to_full_dispatch(seeded, monkeypatch, _settings):
    from app.agents import supervisor

    _patch_llm(monkeypatch, supervisor)
    state = {"sku_id": "ST-92-C1-M", "as_of": "2026-11-02", "run_id": "run-x"}

    out = asyncio.run(supervisor.supervisor_node(state))

    assert out["supervisor"]["tasks"] == ["erp", "market", "rag"]
    assert out["trace"][0]["status"] == "degraded"
    assert "ConnectionError" in out["trace"][0]["detail"]
    assert out["rag_query"]                             # 仍能给出检索词


def test_reasoner_degrades_to_template(monkeypatch, _settings):
    """对照：reasoner 一直是宽捕获 → 规则模板 + degraded 标记。"""
    from app.agents import reasoner

    _patch_llm(monkeypatch, reasoner)
    out = asyncio.run(reasoner.reasoner_node({
        "data": {"erp": {"sku": {"stock": 10}, "features": {}}},
        "judgment": {"risk_flags": ["缺货"]},
        "replenish": {"suggested_qty": 300},
        "attempts": 0,
    }))

    assert out["degraded"] is True
    assert "规则模板结论" in out["plan"]["verdict"]
    assert out["attempts"] == 1
