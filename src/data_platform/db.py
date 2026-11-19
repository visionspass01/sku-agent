"""数据中台存储模型（SQLite，raw → stg → dim/fct → mart + 治理/审批）。"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- 数据接入层（raw）
class RawErpInventory(Base):
    __tablename__ = "raw_erp_inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    snapshot_date: Mapped[dt.date] = mapped_column(Date)
    sku_id: Mapped[str] = mapped_column(String(32), index=True)
    style_id: Mapped[str] = mapped_column(String(32), default="")
    style_name: Mapped[str] = mapped_column(String(128), default="")
    category: Mapped[str] = mapped_column(String(32), default="")
    season: Mapped[str] = mapped_column(String(16), default="")
    color: Mapped[str] = mapped_column(String(16), default="")
    size: Mapped[str] = mapped_column(String(8), default="")
    warehouse_id: Mapped[str] = mapped_column(String(16), default="")
    platform: Mapped[str] = mapped_column(String(16), default="")
    price: Mapped[float] = mapped_column(Float, default=0.0)
    list_price: Mapped[float] = mapped_column(Float, default=0.0)
    # 上架天数（ERP 的主数据属性）：入湖时装成天数，业务日期语义由 dim 层落成 launch_date
    launch_days_ago: Mapped[int] = mapped_column(Integer, default=0)
    on_hand: Mapped[int] = mapped_column(Integer, default=0)
    locked: Mapped[int] = mapped_column(Integer, default=0)
    return_rate: Mapped[float] = mapped_column(Float, default=0.0)
    supplier_id: Mapped[str] = mapped_column(String(16), default="")
    lead_time_days: Mapped[int] = mapped_column(Integer, default=10)


class RawErpInTransit(Base):
    __tablename__ = "raw_erp_in_transit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    snapshot_date: Mapped[dt.date] = mapped_column(Date)
    po_no: Mapped[str] = mapped_column(String(32), index=True)
    sku_id: Mapped[str] = mapped_column(String(32), index=True)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    eta: Mapped[dt.date] = mapped_column(Date)
    supplier_id: Mapped[str] = mapped_column(String(16), default="")
    status: Mapped[str] = mapped_column(String(16), default="在途")


class RawCrmCustomer(Base):
    __tablename__ = "raw_crm_customers"

    customer_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name_masked: Mapped[str] = mapped_column(String(32))
    phone_masked: Mapped[str] = mapped_column(String(32))
    tier: Mapped[str] = mapped_column(String(8), default="普通")
    region: Mapped[str] = mapped_column(String(32), default="")


class RawCrmReservation(Base):
    __tablename__ = "raw_crm_reservations"

    reservation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sku_id: Mapped[str] = mapped_column(String(32), index=True)
    customer_id: Mapped[str] = mapped_column(String(32), ForeignKey("raw_crm_customers.customer_id"))
    qty: Mapped[int] = mapped_column(Integer, default=0)
    reserved_at: Mapped[dt.datetime] = mapped_column(DateTime)
    expected_ship: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="待发货")


class RawOrder(Base):
    __tablename__ = "raw_orders"

    order_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sku_id: Mapped[str] = mapped_column(String(32), index=True)
    customer_id: Mapped[str] = mapped_column(String(32), default="")
    qty: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    order_date: Mapped[dt.date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(16), default="已发货")
    receiver_masked: Mapped[str] = mapped_column(String(32), default="")


class RawLogistics(Base):
    """物流单（一单一物流）。属于业务事实，中台是唯一来源，Agent 不再持有副本。"""

    __tablename__ = "raw_logistics"

    order_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    carrier: Mapped[str] = mapped_column(String(32), default="")
    tracking_no: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="已揽收")
    eta: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime)


class RawAfterSalesTicket(Base):
    """售后工单（含人工确认语义）。

    和 `ApprovalRequest` 一样用「幂等键 + 条件更新」：同订单同原因同数量重复提交只建一单，
    两个 Agent 同时确认只有一条 rowcount=1（另一条 409）。
    """

    __tablename__ = "raw_after_sales"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_after_sales_idem"),
        Index("idx_after_sales_order", "order_id"),
    )

    ticket_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(36), default="")
    sku_id: Mapped[str] = mapped_column(String(32), index=True, default="")
    reason: Mapped[str] = mapped_column(String(32), default="")
    qty: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="pending_confirmation")
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    approver: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)


class RawCompetitorListing(Base):
    """竞品同款行情（原拍立淘 MCP 的数据源）。"""

    __tablename__ = "raw_competitor"
    __table_args__ = (Index("idx_competitor_style", "style_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    style_id: Mapped[str] = mapped_column(String(32), index=True)
    shop_name: Mapped[str] = mapped_column(String(64), default="")
    platform: Mapped[str] = mapped_column(String(16), default="淘系")
    price: Mapped[float] = mapped_column(Float, default=0.0)
    sales_30d: Mapped[int] = mapped_column(Integer, default=0)
    sales_7d: Mapped[int] = mapped_column(Integer, default=0)
    rating: Mapped[float] = mapped_column(Float, default=0.0)
    trend_pct: Mapped[float] = mapped_column(Float, default=0.0)
    snapshot_ts: Mapped[dt.datetime] = mapped_column(DateTime)


class RawSupplier(Base):
    __tablename__ = "raw_supplier"

    supplier_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    lead_time_days: Mapped[int] = mapped_column(Integer, default=10)
    moq: Mapped[int] = mapped_column(Integer, default=0)
    on_time_rate: Mapped[float] = mapped_column(Float, default=0.0)


class RawPlatformActivity(Base):
    __tablename__ = "raw_platform_activity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    platform: Mapped[str] = mapped_column(String(16), default="全部")
    start_date: Mapped[dt.date] = mapped_column(Date, index=True)
    end_date: Mapped[dt.date] = mapped_column(Date)
    category: Mapped[str] = mapped_column(String(32), default="全部")
    note: Mapped[str] = mapped_column(String(128), default="")


class RawTransferOrder(Base):
    """仓间在途调拨（跨仓补位）。"""

    __tablename__ = "raw_transfer"
    __table_args__ = (Index("idx_transfer_sku", "sku_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transfer_no: Mapped[str] = mapped_column(String(32), default="")
    sku_id: Mapped[str] = mapped_column(String(32), index=True)
    from_warehouse_id: Mapped[str] = mapped_column(String(16), default="")
    to_warehouse_id: Mapped[str] = mapped_column(String(16), default="")
    qty: Mapped[int] = mapped_column(Integer, default=0)
    eta: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="在途")



# ---------------------------------------------------------------- 转换层（stg / dim / fct / mart）
class StgErpInventory(Base):
    __tablename__ = "stg_erp_inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku_id: Mapped[str] = mapped_column(String(32), index=True)
    warehouse_id: Mapped[str] = mapped_column(String(16))
    on_hand: Mapped[int] = mapped_column(Integer, default=0)
    locked: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[str] = mapped_column(String(32))
    lead_time_days: Mapped[int] = mapped_column(Integer, default=10)


class StgOrder(Base):
    __tablename__ = "stg_orders"

    order_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sku_id: Mapped[str] = mapped_column(String(32), index=True)
    customer_id: Mapped[str] = mapped_column(String(32), default="")
    qty: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    order_date: Mapped[dt.date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(16), default="已发货")


class DimSku(Base):
    __tablename__ = "dim_sku"

    sku_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    style_id: Mapped[str] = mapped_column(String(32), default="")
    style_name: Mapped[str] = mapped_column(String(128), default="")
    category: Mapped[str] = mapped_column(String(32), default="")
    department: Mapped[str] = mapped_column(String(16), default="")
    season: Mapped[str] = mapped_column(String(16), default="")
    color: Mapped[str] = mapped_column(String(16), default="")
    size: Mapped[str] = mapped_column(String(8), default="")
    platform: Mapped[str] = mapped_column(String(16), default="")
    price: Mapped[float] = mapped_column(Float, default=0.0)
    list_price: Mapped[float] = mapped_column(Float, default=0.0)
    supplier_id: Mapped[str] = mapped_column(String(16), default="")
    lead_time_days: Mapped[int] = mapped_column(Integer, default=10)
    # 服装行业必备：上新日期决定生命周期阶段（上新/主推/清仓/长尾）
    launch_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    size_rank: Mapped[int] = mapped_column(Integer, default=0)
    color_rank: Mapped[int] = mapped_column(Integer, default=0)


class DimStyle(Base):
    """款（style）维度：服装的最小企划单元，SKU 是「款 × 色 × 码」的笛卡尔积。"""

    __tablename__ = "dim_style"

    style_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    style_name: Mapped[str] = mapped_column(String(128), default="")
    category: Mapped[str] = mapped_column(String(32), default="")
    department: Mapped[str] = mapped_column(String(16), default="")
    season: Mapped[str] = mapped_column(String(16), default="")
    launch_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    size_spec: Mapped[list] = mapped_column(JSON, default=list)   # 该款应有尺码集合
    color_count: Mapped[int] = mapped_column(Integer, default=0)
    size_count: Mapped[int] = mapped_column(Integer, default=0)
    sku_count: Mapped[int] = mapped_column(Integer, default=0)



class DimCustomer(Base):
    __tablename__ = "dim_customer"

    customer_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name_masked: Mapped[str] = mapped_column(String(32))
    phone_masked: Mapped[str] = mapped_column(String(32))
    tier: Mapped[str] = mapped_column(String(8), default="普通")
    region: Mapped[str] = mapped_column(String(32), default="")


class FctInventoryDaily(Base):
    __tablename__ = "fct_inventory_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of: Mapped[dt.date] = mapped_column(Date, index=True)
    sku_id: Mapped[str] = mapped_column(String(32), index=True)
    warehouse_id: Mapped[str] = mapped_column(String(16), default="")
    on_hand: Mapped[int] = mapped_column(Integer, default=0)
    locked: Mapped[int] = mapped_column(Integer, default=0)
    reserved_crm: Mapped[int] = mapped_column(Integer, default=0)
    order_hold: Mapped[int] = mapped_column(Integer, default=0)


class FctSalesDaily(Base):
    __tablename__ = "fct_sales_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of: Mapped[dt.date] = mapped_column(Date, index=True)
    sku_id: Mapped[str] = mapped_column(String(32), index=True)
    qty_7d: Mapped[int] = mapped_column(Integer, default=0)
    qty_30d: Mapped[int] = mapped_column(Integer, default=0)
    daily_avg_7d: Mapped[float] = mapped_column(Float, default=0.0)
    daily_avg_30d: Mapped[float] = mapped_column(Float, default=0.0)


class FctInTransit(Base):
    __tablename__ = "fct_in_transit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of: Mapped[dt.date] = mapped_column(Date, index=True)
    sku_id: Mapped[str] = mapped_column(String(32), index=True)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    eta: Mapped[dt.date] = mapped_column(Date)
    days_to_arrive: Mapped[int] = mapped_column(Integer, default=0)


class MartSkuMetrics(Base):
    """治理后指标宽表（动销率/安全库存/预计售罄等）。"""

    __tablename__ = "mart_sku_metrics"

    as_of: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    sku_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    style_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    category: Mapped[str] = mapped_column(String(32), default="")
    department: Mapped[str] = mapped_column(String(16), default="")
    warehouse_id: Mapped[str] = mapped_column(String(16), default="")
    color: Mapped[str] = mapped_column(String(16), default="")
    size: Mapped[str] = mapped_column(String(8), default="")
    on_hand: Mapped[int] = mapped_column(Integer, default=0)
    locked: Mapped[int] = mapped_column(Integer, default=0)
    reserved_crm: Mapped[int] = mapped_column(Integer, default=0)
    order_hold: Mapped[int] = mapped_column(Integer, default=0)
    available: Mapped[int] = mapped_column(Integer, default=0)
    daily_avg_7d: Mapped[float] = mapped_column(Float, default=0.0)
    daily_avg_30d: Mapped[float] = mapped_column(Float, default=0.0)
    qty_30d: Mapped[int] = mapped_column(Integer, default=0)
    sold_30d_flag: Mapped[int] = mapped_column(Integer, default=0)  # 动销率（SKU 级：近 30 天是否有销量）
    in_transit_7d: Mapped[int] = mapped_column(Integer, default=0)
    safety_stock: Mapped[int] = mapped_column(Integer, default=0)
    reorder_point: Mapped[int] = mapped_column(Integer, default=0)
    sell_through_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    stockout_risk: Mapped[str] = mapped_column(String(8), default="低")
    lead_time_days: Mapped[int] = mapped_column(Integer, default=10)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    # ---- 服装行业口径（见 governance.py 的 METRIC_DEFINITIONS）----
    platform: Mapped[str] = mapped_column(String(16), default="")
    list_price: Mapped[float] = mapped_column(Float, default=0.0)
    return_rate: Mapped[float] = mapped_column(Float, default=0.0)
    # 该码销量占同款 30 天销量的比例（识别主销码：≥15% 视为主销）
    size_share: Mapped[float] = mapped_column(Float, default=0.0)
    main_size_flag: Mapped[int] = mapped_column(Integer, default=0)      # 是否主销码
    size_gap_flag: Mapped[int] = mapped_column(Integer, default=0)       # 主销码却无可用库存 → 断码
    # 库存周转天数 = 可用库存 ÷ 日均销量(近30天)；与「售罄天数(7天口径)」是两个不同指标
    turnover_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    sell_through_rate: Mapped[float] = mapped_column(Float, default=0.0)  # 近30天销量 ÷ (销量+可用)
    lifecycle_stage: Mapped[str] = mapped_column(String(8), default="")
    days_since_launch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime)


class MartStyleMetrics(Base):
    """款级指标宽表：服装的最小决策单元（多款 × 多色 × 多码）。

    为什么必须有款级：单 SKU 指标答不了「这个款要不要追单 / 是不是断码了」——
    断码是**款级**现象（同款某码卖完，其余码还压着），只看 SKU 会得出「库存充足」的错误结论。
    """

    __tablename__ = "mart_style_metrics"

    as_of: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    style_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    style_name: Mapped[str] = mapped_column(String(128), default="")
    category: Mapped[str] = mapped_column(String(32), default="")
    department: Mapped[str] = mapped_column(String(16), default="", index=True)
    season: Mapped[str] = mapped_column(String(16), default="")
    launch_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    days_since_launch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lifecycle_stage: Mapped[str] = mapped_column(String(8), default="")
    sku_count: Mapped[int] = mapped_column(Integer, default=0)
    color_count: Mapped[int] = mapped_column(Integer, default=0)
    size_count: Mapped[int] = mapped_column(Integer, default=0)
    listed_size_count: Mapped[int] = mapped_column(Integer, default=0)   # 有货尺码数
    total_available: Mapped[int] = mapped_column(Integer, default=0)
    qty_30d: Mapped[int] = mapped_column(Integer, default=0)
    daily_avg_7d: Mapped[float] = mapped_column(Float, default=0.0)
    sell_through_rate: Mapped[float] = mapped_column(Float, default=0.0)
    turnover_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    full_size_flag: Mapped[int] = mapped_column(Integer, default=0)      # 齐码：所有应有尺码都可用
    broken_size_count: Mapped[int] = mapped_column(Integer, default=0)   # 主销码缺货的个数
    size_gap_rate: Mapped[float] = mapped_column(Float, default=0.0)     # 断码率 = 断码尺码数/应有尺码数
    size_curve: Mapped[list] = mapped_column(JSON, default=list)         # 尺码曲线（含占比）
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime)



# ---------------------------------------------------------------- 治理层
class MetaMetricDefinition(Base):
    __tablename__ = "meta_metric_definitions"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    formula: Mapped[str] = mapped_column(String(256))
    unit: Mapped[str] = mapped_column(String(32), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    synonyms: Mapped[list] = mapped_column(JSON, default=list)
    owner: Mapped[str] = mapped_column(String(32), default="数据治理组")
    version: Mapped[str] = mapped_column(String(16), default="v1")


class MetaDesensitizationRule(Base):
    __tablename__ = "meta_desensitization_rules"

    field: Mapped[str] = mapped_column(String(64), primary_key=True)
    rule: Mapped[str] = mapped_column(String(256))
    example: Mapped[str] = mapped_column(String(64), default="")


class MetaDataVersion(Base):
    __tablename__ = "meta_data_version"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime)


class EtlRunLog(Base):
    __tablename__ = "etl_run_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dag_id: Mapped[str] = mapped_column(String(64), default="")
    task_id: Mapped[str] = mapped_column(String(64), default="")
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    rows: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------- 审批 / ERP Mock
class ApprovalRequest(Base):
    __tablename__ = "approval_request"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_approval_idem"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    type: Mapped[str] = mapped_column(String(16), default="replenish")
    sku_id: Mapped[str] = mapped_column(String(32), index=True)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(24), default="pending_confirmation")
    requested_by: Mapped[str] = mapped_column(String(32), default="agent")
    approver: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    erp_order_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class ErpReplenishOrder(Base):
    __tablename__ = "erp_replenish_order"

    po_no: Mapped[str] = mapped_column(String(32), primary_key=True)
    sku_id: Mapped[str] = mapped_column(String(32), index=True)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    approval_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


# ---------------------------------------------------------------- 治理：调用审计
class ApiAuditLog(Base):
    """每次中台调用的审计行：谁（agent_id）、要什么权限、结果如何、被拒原因。

    为什么放在中台：身份/作用域/配额下沉之后，"谁在什么时候调了什么、为什么被拒"
    必须由被调用方自己记录，否则多 Agent 场景无法追责（Agent 侧日志不可信）。
    """

    __tablename__ = "api_audit_log"
    __table_args__ = (Index("idx_audit_at", "at"), Index("idx_audit_agent", "agent_id"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime)
    agent_id: Mapped[str] = mapped_column(String(32), default="")
    scopes: Mapped[str] = mapped_column(String(64), default="")
    method: Mapped[str] = mapped_column(String(8), default="")
    path: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    request_id: Mapped[str] = mapped_column(String(64), default="")
    decision: Mapped[str] = mapped_column(String(24), default="allow")
    detail: Mapped[str] = mapped_column(String(200), default="")


class PlatformAgent(Base):
    """注册到中台的调用方（多 Agent 插拔的落地形态）。

    为什么不只用环境变量：`PLATFORM_API_KEYS` 改一次要重启中台，而"新 Agent 接入"
    本来就是一个运行时动作。有了这张表，注册 → 拿到令牌 → 立刻能调；改 scope / 轮换令牌 /
    禁用也都是运行时操作，且**令牌只存 sha256**（库被读走也拿不到明文）。
    """

    __tablename__ = "platform_agent"
    __table_args__ = (
        Index("idx_platform_agent_hash", "token_hash", unique=True),
        Index("idx_platform_agent_status", "status"),
    )

    agent_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    token_hash: Mapped[str] = mapped_column(String(64), default="")
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    departments: Mapped[list | None] = mapped_column(JSON, nullable=True)
    rate_limit: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active")
    contract_version: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    rotated_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


# ---------------------------------------------------------------- image 模块（生图 Agent）
class ImageCatalog(Base):
    """构图 / 场景风格 / 图位 / 平台规则的可热更新目录（Agent 不再硬编码）。"""

    __tablename__ = "image_catalog"
    __table_args__ = (Index("idx_image_catalog_kind", "kind", "enabled"),)

    preset_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), default="composition")
    code: Mapped[str] = mapped_column(String(48), default="")
    label: Mapped[str] = mapped_column(String(64), default="")
    desc: Mapped[str] = mapped_column(Text, default="")
    attrs_json: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime)


class ImageRequest(Base):
    """一次批量生图请求的需求快照（`request_id` 幂等）。"""

    __tablename__ = "image_request"

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    product_ids: Mapped[list] = mapped_column(JSON, default=list)
    compositions: Mapped[list] = mapped_column(JSON, default=list)
    scene_styles: Mapped[list] = mapped_column(JSON, default=list)
    count: Mapped[int] = mapped_column(Integer, default=1)
    aspect: Mapped[str] = mapped_column(String(8), default="1:1")
    platform: Mapped[str] = mapped_column(String(16), default="淘系")
    requester: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)


class ImageResult(Base):
    """结果元数据（**不含图片二进制**）；`(task_id, slot)` 唯一 → 回写天然幂等。"""

    __tablename__ = "image_result"
    __table_args__ = (
        UniqueConstraint("task_id", "slot", name="uq_image_result_task_slot"),
        Index("idx_image_result_product", "product_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    batch_id: Mapped[str] = mapped_column(String(32), default="")
    product_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    task_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    slot: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(32), default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    params_json: Mapped[dict] = mapped_column(JSON, default=dict)
    prompt_pack_json: Mapped[list] = mapped_column(JSON, default=list)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    mime: Mapped[str] = mapped_column(String(32), default="image/png")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    storage: Mapped[str] = mapped_column(String(16), default="local")
    storage_key: Mapped[str] = mapped_column(String(256), default="")
    storage_url_expire_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    critic_score: Mapped[int] = mapped_column(Integer, default=0)
    critic_issues: Mapped[list] = mapped_column(JSON, default=list)
    # W4：图内文案契约（逐图位）、是否复用历史成品、变体与参考图来源
    copy_json: Mapped[dict] = mapped_column(JSON, default=dict)
    reused: Mapped[int] = mapped_column(Integer, default=0)
    # 第三轮 P2-16：门禁拦截与部分成功也要回写，中台侧才能解释"为什么少了图"
    blocked_json: Mapped[list] = mapped_column(JSON, default=list)
    warnings_json: Mapped[list] = mapped_column(JSON, default=list)
    partial: Mapped[int] = mapped_column(Integer, default=0)
    expected_images: Mapped[int] = mapped_column(Integer, default=0)
    variant_id: Mapped[str] = mapped_column(String(8), default="")
    variant_direction: Mapped[str] = mapped_column(String(64), default="")
    ref_source: Mapped[str] = mapped_column(String(16), default="mcp")
    cost_cny: Mapped[float] = mapped_column(Float, default=0.0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="done")
    trace_id: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now)



def make_engine(url: str | None = None):
    from sqlalchemy import event

    from data_platform.config import DB_URL

    target = url or DB_URL
    kwargs = {"pool_pre_ping": True}
    if target.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(target, **kwargs)
    if target.startswith("sqlite"):
        # 与 Agent 库一致：WAL + NORMAL，避免每次写都 fsync 拖慢取数接口
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            for pragma in (
                "PRAGMA journal_mode=WAL",
                "PRAGMA synchronous=NORMAL",
                "PRAGMA busy_timeout=5000",
            ):
                try:
                    cursor.execute(pragma)
                except Exception:  # noqa: BLE001
                    pass
            cursor.close()

    return engine


def init_schema(engine=None) -> None:
    eng = engine or make_engine()
    Base.metadata.create_all(eng)
    ensure_columns(eng)


_EXTRA_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "approval_request": [("idempotency_key", "VARCHAR(160) NULL")],
    "raw_erp_inventory": [
        ("platform", "VARCHAR(16) DEFAULT ''"),
        ("list_price", "FLOAT DEFAULT 0"),
        ("launch_days_ago", "INTEGER DEFAULT 0"),
    ],
    # 服装口径上移后补列：老库里 dim_sku / mart_sku_metrics 没有这些字段，
    # 不补的话 SELECT 会直接报 "no such column"（create_all 不会 ALTER 既有表）。
    # 生图回写的新字段（v8）：文案契约 / 是否复用 / 变体 / 参考图来源。
    # 老库直接 SELECT 会报 "no such column: image_result.copy_json"，所以必须在迁移里补。
    "image_result": [
        ("copy_json", "JSON"),
        ("reused", "INTEGER DEFAULT 0"),
        ("variant_id", "VARCHAR(8) DEFAULT ''"),
        ("variant_direction", "VARCHAR(64) DEFAULT ''"),
        ("ref_source", "VARCHAR(16) DEFAULT 'mcp'"),
        ("blocked_json", "JSON"),
        ("warnings_json", "JSON"),
        ("partial", "INTEGER DEFAULT 0"),
        ("expected_images", "INTEGER DEFAULT 0"),
    ],
    "dim_sku": [
        ("platform", "VARCHAR(16) DEFAULT ''"),
        ("list_price", "FLOAT DEFAULT 0"),
        ("launch_date", "DATE NULL"),
        ("size_rank", "INTEGER DEFAULT 0"),
        ("color_rank", "INTEGER DEFAULT 0"),
    ],
    "mart_sku_metrics": [
        ("style_id", "VARCHAR(32) DEFAULT ''"),
        ("platform", "VARCHAR(16) DEFAULT ''"),
        ("list_price", "FLOAT DEFAULT 0"),
        ("color", "VARCHAR(16) DEFAULT ''"),
        ("size", "VARCHAR(8) DEFAULT ''"),
        ("return_rate", "FLOAT DEFAULT 0"),
        ("size_share", "FLOAT DEFAULT 0"),
        ("main_size_flag", "INTEGER DEFAULT 0"),
        ("size_gap_flag", "INTEGER DEFAULT 0"),
        ("turnover_days", "FLOAT NULL"),
        ("sell_through_rate", "FLOAT DEFAULT 0"),
        ("lifecycle_stage", "VARCHAR(8) DEFAULT ''"),
        ("days_since_launch", "INTEGER NULL"),
    ],
}


def ensure_columns(engine) -> list[str]:
    """轻量迁移：给既有演示库补列（create_all 不会 ALTER 既有表）。

    SQLite / MySQL 都支持 `ALTER TABLE ADD COLUMN`；唯一索引用 IF NOT EXISTS /
    SHOW INDEX 判断后创建。整个过程幂等，重复跑无害。
    """
    from sqlalchemy import inspect

    added: list[str] = []
    insp = inspect(engine)
    tables = set(insp.get_table_names())

    for table, columns in _EXTRA_COLUMNS.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for name, ddl in columns:
            if name in existing:
                continue
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
            added.append(f"{table}.{name}")

    if "approval_request" not in tables:
        return added
    index_names = {i.get("name") for i in insp.get_indexes("approval_request")}
    if "uq_approval_idem" not in index_names:
        with engine.begin() as conn:
            if engine.dialect.name == "mysql":
                existing = {row[1] for row in conn.execute(text("SHOW INDEX FROM approval_request"))}
                if "uq_approval_idem" in existing:
                    return added
                conn.execute(text(
                    "CREATE UNIQUE INDEX uq_approval_idem ON approval_request (idempotency_key)"
                ))
            else:
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_idem "
                    "ON approval_request (idempotency_key)"
                ))
    return added


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def set_engine(engine) -> None:
    global _engine
    _engine = engine

def session_scope(engine=None):
    from contextlib import contextmanager

    from sqlalchemy.orm import Session

    @contextmanager
    def _scope():
        session = Session(engine or get_engine())
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return _scope()
