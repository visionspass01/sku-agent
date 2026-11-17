"""复审 F1 / F3：可用库存字段的"缺失"语义、以及 verifier 的提醒与问题分离。

- F1：`_sku_dict` 不能把缺失的 `available` 默认成 0（否则「缺字段回落现存」永远不生效，
  还会把"上游没这个字段"误读成"可用库存为 0"）；
- F3：补货基准口径是**提醒**，不能塞进 `issues`——否则出现「passed=true 但 issues 非空」，
  前端会展示"校验问题 1 / 状态 ok"的自相矛盾。
"""

from __future__ import annotations

import asyncio

from app.agents.verifier import verifier_node


def test_sku_dict_preserves_missing_available_as_none():
    from app.mcp_servers.erp_server import _sku_dict

    legacy = _sku_dict({"sku_id": "ST-01-C1-M", "on_hand": 100})
    assert legacy["stock"] == 100          # 现存
    assert legacy["available"] is None     # 缺字段 → None（不是 0）

    zero = _sku_dict({"sku_id": "ST-01-C1-M", "on_hand": 100, "available": 0})
    assert zero["available"] == 0          # 真的 0 要保留

    from app.skills.stock import available_of, stock_source

    assert available_of(legacy) == 100 and stock_source(legacy) == "on_hand"
    assert available_of(zero) == 0 and stock_source(zero) == "available"


def _state(stock_source: str) -> dict:
    return {
        "sku_id": "ST-90-C1-M",
        "data": {"rag": {"context": "…", "citations": [
            {"chunk_id": "sop-clearance#aaaa1111"}, {"chunk_id": "sop-sizegap#bbbb2222"},
        ]}},
        "judgment": {"risk_flags": [], "days_of_supply": 30.0, "promo_window_near": False},
        "replenish": {"suggested_qty": 0, "lead_time_days": 10, "moq": 100,
                      "stock_source": stock_source},
        "plan": {
            "risk_level": "中",
            "evidence": ["可用库存 40 件", "同款有货尺码 3/3", "供应商交期 10 天"],
            "plan": [{"action": "观察", "qty": None, "reason": "无显著风险"}],
            "citations": ["sop-clearance#aaaa1111", "sop-sizegap#bbbb2222"],
        },
        "verification": None,
    }


def test_verifier_reports_on_hand_basis_as_warning_not_issue(monkeypatch):
    from fakes import FakeLLM

    import app.llm as llm_mod

    llm_mod._llm = FakeLLM(passed=True)
    out = asyncio.run(verifier_node(_state("on_hand")))
    verdict = out["verification"]
    assert verdict["passed"] is True
    assert verdict["issues"] == [], verdict["issues"]
    assert any("现存库存" in w for w in verdict["warnings"]), verdict["warnings"]


def test_verifier_has_no_warning_when_basis_is_available(monkeypatch):
    from fakes import FakeLLM

    import app.llm as llm_mod

    llm_mod._llm = FakeLLM(passed=True)
    out = asyncio.run(verifier_node(_state("available")))
    verdict = out["verification"]
    assert verdict["passed"] is True
    assert verdict["warnings"] == []
