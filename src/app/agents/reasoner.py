"""业务预案生成节点：调用 DeepSeek 输出结构化预案；失败时走确定性降级模板并标记 degraded。"""

from __future__ import annotations

import time

from app.agents.context import format_data_bundle, format_rules, format_skill_outputs
from app.agents.prompt_loader import load_prompt
from app.agents.schemas import InspectionPlan, PlanAction
from app.llm import LLMError, get_llm

# 提示词按调用加载（有 mtime 缓存）：改 md 即生效，不需要重启进程
REASONER_PROMPT = "reasoner"


def _fallback_plan(state: dict, reason: str) -> InspectionPlan:
    """LLM 不可用时的规则模板预案：显式标记降级，不伪装成 AI 结论。"""
    data = state.get("data") or {}
    erp = data.get("erp") or {}
    sku = erp.get("sku") or {}
    features = erp.get("features") or {}
    judgment = state.get("judgment") or {}
    replenish = state.get("replenish") or {}
    rag = data.get("rag") or {}
    citations = [c.get("chunk_id") for c in (rag.get("citations") or []) if c.get("chunk_id")][:3]

    flags = judgment.get("risk_flags") or []
    actions: list[PlanAction] = []
    # 复审 A1：证据里的库存用**可用**口径（现存只作对照），断码用主销码数
    available = int(judgment.get("available_stock") if judgment.get("available_stock") is not None
                    else features.get("available_stock") or 0)
    broken = judgment.get("broken_size_count", features.get("style_broken_size_count"))
    self_broken = judgment.get("self_broken_size", features.get("self_broken_size"))
    broken_text = (
        "本码断码（主销码可用为 0）" if self_broken else
        (f"同款主销码断货 {broken} 个" if broken is not None
         else f"同款缺货尺码 {features.get('zero_sizes')} 个（无主销码标记）")
    )
    evidence = [
        f"可用库存 {available} 件（现存 {sku.get('stock')}），近 7 天日均销量 "
        f"{features.get('base_daily')} 件",
        f"可售天数 {features.get('days_of_supply')} 天，销量斜率 {features.get('slope_norm')}",
        broken_text,
    ]
    risk = "中"

    if "缺货" in flags or "断码" in flags:
        risk = "高"
        qty = int(replenish.get("suggested_qty") or 0)
        actions.append(PlanAction(action="补货", qty=qty, reason=(
            f"补货测算建议 {qty} 件（交期 {replenish.get('lead_time_days')} 天，"
            f"MOQ {replenish.get('moq')}）"), eta_days=replenish.get("lead_time_days")))
        actions.append(PlanAction(action="调拨", qty=None, reason="优先同款有货仓调拨兜底，缩短断货窗口"))
    elif "积压候选" in flags or "换季季末" in flags:
        actions.append(PlanAction(action="促销", qty=None, reason="季末需求疲软，先做搭配购促销减少独立清仓"))
        first_batch = max(1, int(available * 0.4))
        actions.append(PlanAction(action="清仓", qty=first_batch,
                                  reason="按清仓 SOP 首轮 8 折测试、分批放量 40%"))
    elif "大促备货" in flags:
        actions.append(PlanAction(action="观察", qty=None, reason="处于大促备货窗口，高库存属正常备货，维持观察"))
    else:
        actions.append(PlanAction(action="观察", qty=None, reason="无显著风险信号，维持每日巡检"))

    return InspectionPlan(
        risk_level=risk,
        verdict=f"规则模板结论（LLM 不可用降级）：{reason}",
        evidence=evidence,
        plan=actions,
        citations=citations,
    )


def _plan_prompt(state: dict) -> str:
    data = state.get("data") or {}
    judgments = format_skill_outputs(state.get("judgment") or {}, state.get("replenish") or {})
    rules = format_rules((data.get("rag") or {}).get("context") or "")
    parts = [format_data_bundle(data), judgments, rules]
    verification = state.get("verification") or {}
    if verification.get("issues"):
        parts.append(
            "## 上一轮校验未通过\nissues: " + "；".join(verification.get("issues") or [])
            + "\nfixes: " + "；".join(verification.get("fixes") or [])
            + "\n请修正预案后重新输出完整 JSON。"
        )
    followup = (state.get("followup") or "").strip()
    if followup:
        parts.append(
            "## 本轮是多轮追问（数据已在上文，不要重复全量结论）\n"
            f"运营追问：{followup}\n"
            "请基于上文已有的库存/销量/在途/同款/规则数据，直接回答这个追问；"
            "若上文数据不足，请在 evidence 中说明缺什么，不要编造。"
        )
    return "\n\n".join(parts)


async def reasoner_node(state: dict) -> dict:
    started = time.monotonic()
    attempts = int(state.get("attempts") or 0)
    degraded = False
    try:
        plan = await get_llm().invoke_json(
            load_prompt(REASONER_PROMPT), _plan_prompt(state), InspectionPlan, max_tokens=4000
        )
    except LLMError as exc:
        plan = _fallback_plan(state, str(exc)[:120])
        degraded = True
    except Exception as exc:  # 网络/限流等
        plan = _fallback_plan(state, f"{type(exc).__name__}: {exc}"[:120])
        degraded = True

    return {
        "plan": plan.model_dump(),
        "attempts": attempts + 1,
        "degraded": degraded,
        "trace": [{
            "node": "reasoner",
            "status": "degraded" if degraded else "ok",
            "ms": int((time.monotonic() - started) * 1000),
            "detail": f"attempt={attempts + 1}, actions={len(plan.plan)}, "
                      f"citations={len(plan.citations)}",
        }],
    }
