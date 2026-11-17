"""Agent 结构化输出模型（Pydantic 校验，LLM 输出必须符合 schema）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ActionName = Literal["补货", "调拨", "清仓", "促销", "观察", "下架"]
RiskLevel = Literal["高", "中", "低"]
TaskName = Literal["erp", "market", "rag"]


class PlanAction(BaseModel):
    action: ActionName = Field(description="处置动作")
    qty: int | None = Field(default=None, description="数量（件），观察类可为空")
    reason: str = Field(description="动作依据，必须引用数据或规则")
    eta_days: int | None = Field(default=None, description="预计完成天数")


class InspectionPlan(BaseModel):
    risk_level: RiskLevel
    verdict: str = Field(description="结论：一句话说明风险与建议")
    # 复审 A4：下限与策略对齐（reasoner.md 要求 ≥3 条、verifier 也按 3 条判）。
    # 早先这里是 min_length=1，只写 2 条证据的预案能过 schema、却在 verifier 被打回，
    # 白跑一轮 revision（多一次 LLM 调用）；现在在结构化输出层就失败，交给 JSON 修复重试。
    evidence: list[str] = Field(min_length=3, description="数据证据，至少 3 条")
    plan: list[PlanAction] = Field(min_length=1)
    citations: list[str] = Field(default_factory=list, description="引用的规则 chunk_id 列表")


class VerifierVerdict(BaseModel):
    passed: bool = Field(description="预案是否合规、满足 SOP")
    issues: list[str] = Field(default_factory=list)
    fixes: list[str] = Field(default_factory=list)
    compliance: dict[str, bool] = Field(default_factory=dict, description="检查项 → 是否通过")


class SupervisorPlan(BaseModel):
    tasks: list[TaskName] = Field(description="需要派发给哪些 Worker")
    priority: RiskLevel = "中"
    note: str = ""


class JudgeOutput(BaseModel):
    days_of_supply: float
    health_score: int
    risk_flags: list[str]
    zero_sizes: int
    active_season: bool
    promo_window_near: bool
    notes: list[str] = Field(default_factory=list)
    # 复审 A1：断码按「本码是主销码且可用为 0」判定；旧归档没有主销码标记时为 None
    broken_size_count: int | None = None      # 同款主销码断货个数（说明用）
    self_broken_size: bool | None = None      # 本码是否断码（判定用）
    size_semantics: str = ""          # main_size / legacy
    stock_source: str = ""            # available / on_hand
    available_stock: int = 0


class ReplenishOutput(BaseModel):
    daily_sales: float
    lead_time_days: int
    safety_stock: int
    reorder_point: int
    target_days: int
    suggested_qty: int
    moq: int
    in_transit_7d: int
    coverage_days_after: float
    notes: list[str] = Field(default_factory=list)
    stock_source: str = ""
    available_stock: int = 0
