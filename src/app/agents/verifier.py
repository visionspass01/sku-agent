"""校验 Agent 节点：确定性规则 + LLM 双重校验（合规 / 满足 SOP / 数据一致 / 引用有效）。"""

from __future__ import annotations

import json
import re
import time

from app.agents.context import format_data_bundle, format_rules, format_skill_outputs
from app.agents.prompt_loader import load_prompt
from app.agents.schemas import VerifierVerdict
from app.llm import get_llm

# 提示词按调用加载（有 mtime 缓存）：改 md 即生效，不需要重启进程
VERIFIER_PROMPT = "verifier"


def _det_checks(state: dict) -> tuple[dict[str, bool], list[str], list[str]]:
    plan = state.get("plan") or {}
    data = state.get("data") or {}
    judgment = state.get("judgment") or {}
    replenish = state.get("replenish") or {}
    rag = data.get("rag") or {}

    valid_ids = {c.get("chunk_id") for c in (rag.get("citations") or []) if c.get("chunk_id")}
    citations = [c for c in (plan.get("citations") or []) if c]
    actions = plan.get("plan") or []
    action_names = [a.get("action") for a in actions]
    issues: list[str] = []
    fixes: list[str] = []

    citation_ok = bool(citations) and all(c in valid_ids for c in citations) and len(citations) >= 2
    if not citation_ok:
        issues.append("引用无效：必须引用不少于 2 条检索到的规则 chunk_id，且不得编造编号")
        fixes.append("从给定规则片段中选取至少 2 个 [chunk_id] 作为 citations")

    evidence_ok = len([e for e in (plan.get("evidence") or []) if e]) >= 3
    if not evidence_ok:
        issues.append("证据不足：evidence 少于 3 条")
        fixes.append("补充数据包中的具体数字作为证据（库存/销量/在途/竞品等）")

    suggested = int(replenish.get("suggested_qty") or 0)
    replenish_actions = [a for a in actions if a.get("action") == "补货"]
    qty_ok = True
    if suggested > 0:
        total_qty = sum(int(a.get("qty") or 0) for a in replenish_actions)
        if not replenish_actions or not (0.9 * suggested <= total_qty <= 1.1 * suggested):
            qty_ok = False
            issues.append(f"补货量不合规：命中 {total_qty} 件，测算建议 {suggested} 件")
            fixes.append(f"将补货数量改为 {suggested} 件（或与补货测算一致的量）")
    elif replenish_actions and sum(int(a.get("qty") or 0) for a in replenish_actions) > 0:
        qty_ok = False
        issues.append("补货量不合规：测算建议不补货，但预案包含补货动作")
        fixes.append("移除补货动作，改为观察/促销动作")
    # 注：补货基准口径（stock_source=on_hand）属于**提醒**而不是"不合规"，
    # 由 `verifier_node` 写进 `warnings`——否则会出现「passed=true 但 issues 非空」（复审 F3）。

    discount_ok = True
    for a in actions:
        if a.get("action") == "清仓":
            reason = str(a.get("reason") or "")
            nums = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*折", reason)]
            if not nums:
                discount_ok = False
                issues.append("清仓折扣缺失：清仓动作必须说明折扣（平台下限 6 折）")
                fixes.append("在清仓 reason 中写明折扣，如「首轮 8 折测试 7 天」")
            elif min(nums) < 6:
                discount_ok = False
                issues.append(f"清仓折扣 {min(nums)} 折低于平台活动下限 6 折")
                fixes.append("清仓折扣不得低于 6 折")
            if "分批" not in reason and "40%" not in reason:
                discount_ok = False
                issues.append("清仓节奏缺失：需按清仓 SOP 说明分批放量（40%）")
                fixes.append("补充分批说明，如「首轮放量 40%」")

    promo_near = bool(judgment.get("promo_window_near"))
    days = float(judgment.get("days_of_supply") or 0)
    sop_ok = True
    if promo_near and days >= 60 and "清仓" in action_names:
        sop_ok = False
        issues.append("SOP 顺序不合规：大促窗口内的高库存属正常备货，不应执行清仓")
        fixes.append("改为「观察/维持备货」，并引用促销 SOP 与活动窗口说明")
    flags = judgment.get("risk_flags") or []
    if ("缺货" in flags or "断码" in flags) and not ({"补货", "调拨"} & set(action_names)):
        sop_ok = False
        issues.append("SOP 顺序不合规：缺货/断码风险必须有补货或调拨动作")
        fixes.append("补充补货或调拨动作，优先调拨兜底")

    compliance = {
        "引用有效性": citation_ok,
        "数字一致性": evidence_ok,
        "折扣合规": discount_ok,
        "补货合规": qty_ok,
        "SOP 顺序": sop_ok,
    }
    return compliance, issues, fixes


async def verifier_node(state: dict) -> dict:
    started = time.monotonic()
    compliance, issues, fixes = _det_checks(state)
    det_passed = all(compliance.values())

    llm_passed: bool | None = None
    llm_issues: list[str] = []
    llm_fixes: list[str] = []
    llm_unavailable = ""
    try:
        data = state.get("data") or {}
        user = "\n\n".join([
            format_data_bundle(data),
            format_skill_outputs(state.get("judgment") or {}, state.get("replenish") or {}),
            format_rules((data.get("rag") or {}).get("context") or ""),
            "## 待校验预案（JSON）",
            json.dumps(state.get("plan") or {}, ensure_ascii=False, indent=1),
        ])
        verdict = await get_llm().invoke_json(
            load_prompt(VERIFIER_PROMPT), user, VerifierVerdict, max_tokens=1500
        )
        llm_passed = verdict.passed
        llm_issues = verdict.issues
        llm_fixes = verdict.fixes
        compliance = {**verdict.compliance, **compliance}  # 规则校验结果覆盖 LLM 同名项
    except Exception as exc:  # noqa: BLE001
        # LLMError（JSON 修复失败）与网络/限流错误都只降级为「仅规则校验」：
        # 早先只捕 LLMError，LLM 网络故障会让整个巡检 run 失败，而不是降级出结论。
        # 复审 P4：这条是**提醒**不是"不合规"——写进 issues 会出现
        # 「passed=true 但 issues 非空」的自相矛盾（F3 的同一类问题，只是换了触发点）。
        llm_unavailable = f"LLM 校验不可用（{type(exc).__name__}），仅执行规则校验"

    passed = det_passed and (llm_passed is not False)
    all_issues = issues + llm_issues
    all_fixes = fixes + llm_fixes
    # 复审 F3：**提醒**与"不合规问题"分开。补货基准用现存库存时测算量会偏低，但预案本身
    # 不违规——放进 issues 会造成「passed=true 但 issues 非空」的自相矛盾展示。
    warnings: list[str] = []
    if llm_unavailable:
        warnings.append(llm_unavailable)
    stock_source = str((state.get("replenish") or {}).get("stock_source") or "")
    if stock_source == "on_hand":
        warnings.append(
            "补货基准用的是现存库存（缺 available 字段），测算量可能偏低，建议人工复核"
        )

    return {
        "verification": {
            "passed": passed,
            "issues": all_issues,
            "fixes": all_fixes,
            "warnings": warnings,
            "compliance": compliance,
            "llm_passed": llm_passed,
        },
        "trace": [{
            "node": "verifier",
            "status": "ok" if passed else "failed",
            "ms": int((time.monotonic() - started) * 1000),
            "detail": f"issues={len(all_issues)} compliance={compliance}",
        }],
    }
