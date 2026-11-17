"""预案结构化输出的下限（复审 A4）：evidence 必须 ≥3 条，与提示词/校验器一致。

早先 `InspectionPlan.evidence` 是 `min_length=1`，而 `reasoner.md` 要求"至少 3 条"、
`verifier._det_checks` 也按 3 条判 —— 只写 2 条证据的预案能过 schema、却在 verifier 被打回，
白跑一整轮 revision（多一次 LLM 调用 + 拉长研判时间）。现在在 schema 层就失败，
交给 `llm_json_repair_retries` 的修复重试去补。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.schemas import InspectionPlan, PlanAction


def _plan(evidence: list[str]) -> dict:
    return {
        "risk_level": "中",
        "verdict": "库存偏高，建议促销",
        "evidence": evidence,
        "plan": [PlanAction(action="促销", qty=None, reason="季末需求疲软")],
        "citations": ["sop-x#abcd1234"],
    }


def test_two_evidence_items_are_rejected():
    with pytest.raises(ValidationError) as err:
        InspectionPlan(**_plan(["可用库存 10 件", "日均 2 件"]))
    assert "evidence" in str(err.value)


def test_three_evidence_items_pass():
    plan = InspectionPlan(**_plan(["可用库存 10 件", "日均 2 件", "在途 0 件"]))
    assert len(plan.evidence) == 3


def test_fallback_plan_always_satisfies_the_floor():
    """降级模板也必须自带 3 条证据，否则 LLM 不可用时预案会卡在校验上。"""
    from app.agents.reasoner import _fallback_plan

    plan = _fallback_plan(
        {"data": {"erp": {"sku": {"stock": 10}, "features": {}}},
         "judgment": {"risk_flags": ["缺货"]}, "replenish": {"suggested_qty": 300}},
        "LLM 不可用",
    )
    assert len(plan.evidence) >= 3
    # 校验器与提示词的口径也要一致（防止只改一边）
    from app.agents import verifier

    state = {
        "plan": plan.model_dump(),
        "data": {"rag": {"citations": [{"chunk_id": "sop-x#abcd1234"}]}},
        "judgment": {"risk_flags": ["缺货"]},
        "replenish": {"suggested_qty": 300},
    }
    _compliance, issues, _fixes = verifier._det_checks(state)
    assert all("证据不足" not in issue for issue in issues), issues
