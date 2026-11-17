"""中台响应体的 pydantic 契约（每个端点都必须有一个 response_model）。

设计取舍：
1. `extra="allow"`：**校验声明的字段，但保留多出来的字段**。这样「加字段」不会打断老调用方
   （次版本向后兼容），而「删字段/改类型」会在 CI 里作为 ValidationError 直接挂掉。
   反过来若用默认的 `extra="ignore"`，忘声明的字段会被静默吞掉——那正是我们要避免的静默降质。
2. `Envelope.schema_version` 有默认值：路由仍然返回普通 dict，FastAPI 校验时会自动补齐版本号，
   不需要在 20 个端点里各写一遍。
3. 字段名就是对外契约，改名前请先读 `data_platform/contract.py` 的版本策略。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from data_platform.contract import CONTRACT_VERSION


class Envelope(BaseModel):
    """所有响应的公共外壳：契约版本 + 数据版本（缓存失效依据）。"""

    model_config = ConfigDict(extra="allow")

    schema_version: str = Field(default=CONTRACT_VERSION, description="中台对外契约版本")
    data_version: int | None = Field(default=None, description="mart 数据版本，用于缓存失效")


# ---------------------------------------------------------------- 探活 / 元数据
class HealthOut(Envelope):
    status: str
    time: str
    mart_rows: int = 0
    cache_keys: dict | int | None = None
    caller: dict | None = None


class MetricsMetaOut(Envelope):
    metrics: list[dict]
    prompt_context: str = ""
    desensitization: dict = Field(default_factory=dict)
    contract: dict = Field(default_factory=dict)


class DepartmentsOut(Envelope):
    departments: dict[str, list[str]]
    # 映射指纹（内容哈希）：Agent 侧缓存据此判断「中台改了部门映射」，不用重启
    version: str = ""
    note: str = ""


class CapabilitiesOut(Envelope):
    version: str = ""
    capabilities: list[dict] = Field(default_factory=list)
    groups: dict[str, list[str]] = Field(default_factory=dict)
    note: str = ""


class SynonymsOut(Envelope):
    version: int = 0
    synonyms: dict[str, list[str]] = Field(default_factory=dict)
    note: str = ""


class CatalogOut(Envelope):
    items: list[dict] = Field(default_factory=list)
    count: int = 0
    total: int = 0
    next_offset: int | None = None
    note: str = ""


# ---------------------------------------------------------------- 读接口（按 SKU）
class InventoryOut(Envelope):
    sku_id: str
    as_of: str = ""
    warehouse_id: str = ""
    on_hand: int = 0
    locked: int = 0
    reserved_crm: int = 0
    order_hold: int = 0
    available: int = 0
    cached: bool = False


class SkuBatchOut(Envelope):
    items: list[dict] = Field(default_factory=list)
    count: int = 0
    missing: list[str] = Field(default_factory=list)
    scope: list[str] | None = None
    scope_filtered: bool = False
    scope_dropped: int = 0


class SalesOut(Envelope):
    sku_id: str
    as_of: str = ""
    days: int = 7
    qty_7d: int = 0
    qty_30d: int = 0
    daily_avg_7d: float = 0.0
    daily_avg_30d: float = 0.0
    sold_30d_flag: int = 0
    cached: bool = False


class InTransitOut(Envelope):
    sku_id: str
    items: list[dict] = Field(default_factory=list)
    total: int = 0
    in_transit_7d: int = 0
    as_of: str = ""
    cached: bool = False


class ReservationsOut(Envelope):
    sku_id: str
    items: list[dict] = Field(default_factory=list)
    total_qty: int = 0
    cached: bool = False


class OrdersOut(Envelope):
    sku_id: str
    days: int = 7
    items: list[dict] = Field(default_factory=list)
    total_qty: int = 0
    pending_qty: int = 0
    count: int = 0
    cached: bool = False


class SkuMetricsOut(Envelope):
    sku_id: str
    as_of: str = ""
    style_id: str = ""
    category: str = ""
    department: str = ""
    warehouse_id: str = ""
    color: str = ""
    size: str = ""
    on_hand: int = 0
    locked: int = 0
    reserved_crm: int = 0
    order_hold: int = 0
    available: int = 0
    daily_avg_7d: float = 0.0
    daily_avg_30d: float = 0.0
    qty_30d: int = 0
    sold_30d_flag: int = 0
    in_transit_7d: int = 0
    safety_stock: int = 0
    reorder_point: int = 0
    sell_through_days: float | None = None
    stockout_risk: str = "低"
    lead_time_days: int = 10
    price: float = 0.0
    # 服装口径
    return_rate: float = 0.0
    size_share: float = 0.0
    main_size_flag: int = 0
    size_gap_flag: int = 0
    turnover_days: float | None = None
    sell_through_rate: float = 0.0
    lifecycle_stage: str = ""
    days_since_launch: int | None = None
    cached: bool = False


class SellThroughOut(Envelope):
    sku_id: str
    as_of: str = ""
    available: int = 0
    in_transit_7d: int = 0
    daily_avg_7d: float = 0.0
    sell_through_days: float | None = None
    stockout_risk: str = "低"
    formula: str = ""
    cached: bool = False


class SalesSeriesOut(Envelope):
    sku_id: str
    days: int = 7
    daily: list[dict] = Field(default_factory=list)
    note: str = ""
    cached: bool = False


class SalesRateOut(Envelope):
    department: str = "全部"
    scope: list[str] | None = None
    effective_departments: list[str] | None = None
    sold_30d_skus: int = 0
    total_skus: int = 0
    sales_rate: float = 0.0
    formula: str = ""
    cached: bool = False


# ---------------------------------------------------------------- 款级 / 尺码结构
class StyleMetricsOut(Envelope):
    style_id: str
    style_name: str = ""
    category: str = ""
    department: str = ""
    season: str = ""
    lifecycle_stage: str = ""
    launch_date: str | None = None
    days_since_launch: int | None = None
    sku_count: int = 0
    color_count: int = 0
    size_count: int = 0
    total_available: int = 0
    qty_30d: int = 0
    daily_avg_7d: float = 0.0
    sell_through_rate: float = 0.0
    turnover_days: float | None = None
    size_curve: list[dict] = Field(default_factory=list)
    size_gap: dict = Field(default_factory=dict)
    full_size_flag: bool = False
    size_breakdown: list[dict] = Field(default_factory=list)
    cached: bool = False


class SizeCurveOut(Envelope):
    style_id: str
    items: list[dict] = Field(default_factory=list)
    note: str = ""
    cached: bool = False


class SizeGapOut(Envelope):
    department: str | None = None
    items: list[dict] = Field(default_factory=list)
    count: int = 0
    full_size_rate: float = 0.0
    broken_size_rate: float = 0.0
    formula: dict = Field(default_factory=dict)
    cached: bool = False


class ApparelOverviewOut(Envelope):
    department: str | None = None
    style_count: int = 0
    full_size_styles: int = 0
    broken_size_styles: int = 0
    full_size_rate: float = 0.0
    broken_size_rate: float = 0.0
    avg_turnover_days: float | None = None
    avg_sell_through_rate: float = 0.0
    lifecycle_breakdown: dict = Field(default_factory=dict)
    formula: dict = Field(default_factory=dict)
    cached: bool = False


# ---------------------------------------------------------------- 订单域 / 供应链 / 竞品
class OrderDetailOut(Envelope):
    order_id: str
    found: bool = False
    order: dict = Field(default_factory=dict)
    logistics: dict = Field(default_factory=dict)
    after_sales: list[dict] = Field(default_factory=list)
    note: str = ""


class LogisticsOut(Envelope):
    order_id: str
    found: bool = False
    logistics: dict = Field(default_factory=dict)
    note: str = ""


class ReturnRiskOut(Envelope):
    sku_id: str
    found: bool = False
    return_rate: float = 0.0
    orders_last_days: int = 0
    after_sales_tickets: int = 0
    risk: str = "低"
    note: str = ""


class AfterSalesOut(Envelope):
    ticket_id: str
    order_id: str = ""
    sku_id: str = ""
    reason: str = ""
    qty: int = 0
    status: str = ""
    deduped: bool = False
    note: str = ""


class AfterSalesListOut(Envelope):
    status: str = "pending"
    items: list[dict] = Field(default_factory=list)
    count: int = 0
    scope: list[str] | None = None
    scope_filtered: bool = False
    scope_dropped: int = 0


class AfterSalesDecisionOut(Envelope):
    ticket_id: str
    status: str
    approver: str = ""
    decided_at: str | None = None
    caller: str = ""
    previous_status: str = ""


class SupplierOut(Envelope):
    supplier_id: str
    found: bool = False
    name: str = ""
    lead_time_days: int = 10
    moq: int = 0
    on_time_rate: float | None = None


class TransfersOut(Envelope):
    sku_id: str
    items: list[dict] = Field(default_factory=list)
    count: int = 0


class ActivitiesOut(Envelope):
    platform: str = ""
    items: list[dict] = Field(default_factory=list)
    count: int = 0


class CompetitorsOut(Envelope):
    style_id: str
    found: bool = False
    competitors: list[dict] = Field(default_factory=list)
    price_min: float | None = None
    price_max: float | None = None
    price_avg: float | None = None
    heat_index: float | None = None
    trend_pct: float | None = None
    note: str = ""


class PriceBandOut(Envelope):
    style_id: str
    found: bool = False
    count: int = 0
    price_min: float | None = None
    price_max: float | None = None
    price_median: float | None = None
    price_avg: float | None = None


class StyleHeatOut(Envelope):
    style_id: str
    found: bool = False
    competitor_sales_7d: int = 0
    avg_trend_pct: float = 0.0
    heat_index: float = 0.0


# ---------------------------------------------------------------- 动作
class ApprovalOut(Envelope):
    approval_id: str
    status: str
    sku_id: str = ""
    qty: int = 0
    amount: float = 0.0
    po_no: str | None = None
    deduped: bool = False
    high_risk: bool = False
    requested_by: str = ""
    message: str = ""
    thresholds: dict | None = None
    caller: str | None = None


class ApprovalsOut(Envelope):
    status: str = "pending"
    window_hours: int | None = None
    items: list[dict] = Field(default_factory=list)
    count: int = 0
    scope: list[str] | None = None
    scope_filtered: bool = False
    scope_dropped: int = 0


class ApprovalDecisionOut(Envelope):
    approval_id: str
    status: str
    po_no: str | None = None
    erp_status: str | None = None
    approver: str = ""
    caller: str = ""
    previous_status: str = ""


class TransferSuggestionOut(Envelope):
    sku_id: str
    suggestion: dict | None = None
    message: str = ""


# ---------------------------------------------------------------- 运维
class EtlRunsOut(Envelope):
    count: int = 0
    items: list[dict] = Field(default_factory=list)


class AuditOut(Envelope):
    count: int = 0
    by_decision: dict = Field(default_factory=dict)
    items: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------- Agent 注册（多 Agent 治理）
class AgentOut(Envelope):
    agent_id: str
    name: str = ""
    scopes: list[str] = Field(default_factory=list)
    departments: list[str] | None = None
    rate_limit: int = 0
    status: str = "active"
    contract_version: str = ""
    created_at: str | None = None
    rotated_at: str | None = None
    last_seen_at: str | None = None
    source: str = "db"


class AgentRegisterOut(Envelope):
    agent_id: str
    token: str = Field(description="明文令牌只在注册/轮换时返回一次")
    scopes: list[str] = Field(default_factory=list)
    departments: list[str] | None = None
    rate_limit: int = 0
    contract_version: str = ""
    note: str = ""


class AgentsOut(Envelope):
    count: int = 0
    items: list[dict] = Field(default_factory=list)
    note: str = ""
