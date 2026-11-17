"""数据模型（SQLAlchemy 2.0）。

设计要点：
- `stat_hour` 是简历中的「小时快照表」，按 (stat_hour, sku_id) 建联合索引；
- 本地 SQLite / docker 内 MySQL 共用同一套 metadata（表名、字段、索引一致）；
- JSON 列直接使用 SQLAlchemy JSON 类型，SQLite/MySQL 均可存储。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Warehouse(Base):
    __tablename__ = "warehouse"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    city: Mapped[str] = mapped_column(String(32))


class Supplier(Base):
    __tablename__ = "supplier"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    lead_time_days: Mapped[int] = mapped_column(Integer)
    moq: Mapped[int] = mapped_column(Integer)
    on_time_rate: Mapped[float] = mapped_column(Float, default=0.9)


class Sku(Base):
    __tablename__ = "sku"

    sku_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    style_id: Mapped[str] = mapped_column(String(32), index=True)
    style_name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(32), index=True)
    season: Mapped[str] = mapped_column(String(16))  # 秋冬 / 春夏 / 四季
    color: Mapped[str] = mapped_column(String(16))
    size: Mapped[str] = mapped_column(String(8))
    warehouse_id: Mapped[str] = mapped_column(String(16), ForeignKey("warehouse.id"))
    platform: Mapped[str] = mapped_column(String(16))  # 淘系 / 抖音
    price: Mapped[float] = mapped_column(Float)
    list_price: Mapped[float] = mapped_column(Float)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    return_rate: Mapped[float] = mapped_column(Float, default=0.05)
    supplier_id: Mapped[str] = mapped_column(String(16), ForeignKey("supplier.id"))
    status: Mapped[str] = mapped_column(String(16), default="在售")
    list_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)


class StatHour(Base):
    """小时销量快照：定时任务每小时读取 ERP 出库流水聚合写入。

    联合索引 idx_stat_hour_sku(stat_hour, sku_id)：支撑「按小时段批量拉取
    某品类/某仓全部 SKU 销量」的巡检查询，避免全表扫描。
    """

    __tablename__ = "stat_hour"
    __table_args__ = (
        Index("idx_stat_hour_sku", "stat_hour", "sku_id"),
        UniqueConstraint("stat_hour", "sku_id", name="uq_stat_hour_sku"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stat_hour: Mapped[dt.datetime] = mapped_column(DateTime)
    sku_id: Mapped[str] = mapped_column(String(32), ForeignKey("sku.sku_id"), index=True)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    amt: Mapped[float] = mapped_column(Float, default=0.0)
    orders: Mapped[int] = mapped_column(Integer, default=0)


class Flow(Base):
    """ERP 出库流水（原始业务数据，按 SKU 小时聚合后的流水行）。"""

    __tablename__ = "flow"
    __table_args__ = (Index("idx_flow_sku_ts", "sku_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku_id: Mapped[str] = mapped_column(String(32), ForeignKey("sku.sku_id"))
    warehouse_id: Mapped[str] = mapped_column(String(16), ForeignKey("warehouse.id"))
    qty: Mapped[int] = mapped_column(Integer)
    amt: Mapped[float] = mapped_column(Float, default=0.0)
    ts: Mapped[dt.datetime] = mapped_column(DateTime)
    flow_type: Mapped[str] = mapped_column(String(16), default="OUT")
    order_id: Mapped[str] = mapped_column(String(32), default="")


class InTransit(Base):
    """在途采购。"""

    __tablename__ = "in_transit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku_id: Mapped[str] = mapped_column(String(32), ForeignKey("sku.sku_id"), index=True)
    qty: Mapped[int] = mapped_column(Integer)
    eta: Mapped[dt.date] = mapped_column(Date)
    po_no: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="在途")


class TransferOrder(Base):
    """仓库调拨。"""

    __tablename__ = "transfer_order"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku_id: Mapped[str] = mapped_column(String(32), ForeignKey("sku.sku_id"), index=True)
    from_warehouse_id: Mapped[str] = mapped_column(String(16))
    to_warehouse_id: Mapped[str] = mapped_column(String(16))
    qty: Mapped[int] = mapped_column(Integer)
    eta: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="调拨中")


class CompetitorListing(Base):
    """竞品同款行情（拍立淘 MCP 数据源）。"""

    __tablename__ = "competitor_listing"
    __table_args__ = (Index("idx_competitor_style", "style_id", "platform"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    style_id: Mapped[str] = mapped_column(String(32))
    platform: Mapped[str] = mapped_column(String(16))
    shop_name: Mapped[str] = mapped_column(String(64))
    price: Mapped[float] = mapped_column(Float)
    sales_30d: Mapped[int] = mapped_column(Integer)
    sales_7d: Mapped[int] = mapped_column(Integer)
    rating: Mapped[float] = mapped_column(Float)
    trend_pct: Mapped[float] = mapped_column(Float)  # 近 7 日销量环比
    snapshot_ts: Mapped[dt.datetime] = mapped_column(DateTime)


class PlatformActivity(Base):
    """平台大促活动窗口（影响备货与清仓判断）。"""

    __tablename__ = "platform_activity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    platform: Mapped[str] = mapped_column(String(16))
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date] = mapped_column(Date)
    category: Mapped[str] = mapped_column(String(32), default="全部")
    note: Mapped[str] = mapped_column(String(256), default="")


class KbDoc(Base):
    """RAG 知识库文档元数据。"""

    __tablename__ = "kb_doc"

    doc_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(128))
    department: Mapped[str] = mapped_column(String(16), default="通用")  # 男装 / 女装 / 通用
    category: Mapped[str] = mapped_column(String(32), default="全部")
    season: Mapped[str] = mapped_column(String(16), default="全部")
    warehouse: Mapped[str] = mapped_column(String(32), default="全部")
    platform: Mapped[str] = mapped_column(String(16), default="全部")
    effective_start: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    effective_end: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    version: Mapped[str] = mapped_column(String(16), default="v1")
    source: Mapped[str] = mapped_column(String(128), default="")
    sha256: Mapped[str] = mapped_column(String(64), default="")
    # 块集合指纹（chunk_id 排序后取 sha1）：用于「只重建变动文档」的对账。
    # 没有它只能靠"总块数对不对"判断，而文档变短+另一篇变长时总数可能正好相等。
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    chunks_sha: Mapped[str] = mapped_column(String(64), default="")
    # 分块方案版本：升级分块逻辑后，即使文档没变也要重建（否则旧块一直留着）
    chunker_version: Mapped[str] = mapped_column(String(8), default="")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime)


class KbChunk(Base):
    """RAG 分块 + 本地哈希向量（演示用确定性向量；生产可替换 Embedding API）。"""

    __tablename__ = "kb_chunk"
    __table_args__ = (Index("idx_kb_chunk_doc", "doc_id", "ord"),)

    chunk_id: Mapped[str] = mapped_column(String(80), primary_key=True)  # doc_id#ord
    doc_id: Mapped[str] = mapped_column(String(64), ForeignKey("kb_doc.doc_id"), index=True)
    ord: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)
    vec: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime)


class KbSyncLog(Base):
    __tablename__ = "kb_sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16))  # add / update / delete
    at: Mapped[dt.datetime] = mapped_column(DateTime)
    note: Mapped[str] = mapped_column(String(256), default="")


class Anomaly(Base):
    """多维度量化筛选产出的异常 SKU 列表（每小时刷新）。"""

    __tablename__ = "anomaly"
    __table_args__ = (Index("idx_anomaly_hour", "hour"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku_id: Mapped[str] = mapped_column(String(32), ForeignKey("sku.sku_id"), index=True)
    hour: Mapped[dt.datetime] = mapped_column(DateTime)
    severity: Mapped[str] = mapped_column(String(8), default="中")  # 高/中/低
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)


class EvalLabel(Base):
    """演示用 ground-truth 标注：用于评估脚本对比筛查/研判效果。"""

    __tablename__ = "eval_label"

    sku_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    anomaly_type: Mapped[str] = mapped_column(String(32), default="normal")
    expected_risk: Mapped[str] = mapped_column(String(8), default="低")
    note: Mapped[str] = mapped_column(String(256), default="")


class InspectionRun(Base):
    __tablename__ = "inspection_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # single / hourly / daily
    status: Mapped[str] = mapped_column(String(16), default="pending")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)


class InspectionResult(Base):
    __tablename__ = "inspection_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("inspection_run.id"), index=True)
    sku_id: Mapped[str] = mapped_column(String(32), index=True)
    risk_level: Mapped[str] = mapped_column(String(8), default="中")
    plan: Mapped[dict] = mapped_column(JSON, default=dict)
    citations: Mapped[list] = mapped_column(JSON, default=list)
    verifier: Mapped[dict] = mapped_column(JSON, default=dict)
    data_bundle: Mapped[dict] = mapped_column(JSON, default=dict)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)


class AuthEvent(Base):
    """登录安全记录：用户名 / IP / 时间 / 结果（仅保留最近 N 小时，默认 4 小时）。

    `result` 区分成功与失败：连续失败次数是限流与「暴力破解是否正在发生」的依据。
    """

    __tablename__ = "auth_event"
    __table_args__ = (Index("idx_auth_event_at", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), index=True)
    ip: Mapped[str] = mapped_column(String(64), default="-")
    user_agent: Mapped[str] = mapped_column(String(200), default="")
    result: Mapped[str] = mapped_column(String(16), default="ok")
    at: Mapped[dt.datetime] = mapped_column(DateTime)


class ChatSession(Base):
    """Agent 多轮对话会话（按登录账号隔离）。"""

    __tablename__ = "chat_session"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(64), default="新会话")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime)


class ChatMessage(Base):
    """会话消息（user / assistant），tool_trace 记录工具调用轨迹。"""

    __tablename__ = "chat_message"
    __table_args__ = (Index("idx_chat_msg_session", "session_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("chat_session.id"))
    role: Mapped[str] = mapped_column(String(16))  # user / assistant
    content: Mapped[str] = mapped_column(Text, default="")
    tool_trace: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)


class PatrolLog(Base):
    """批量巡检日志与简报（记录操作人、数量、结果与本次巡检报告）。"""

    __tablename__ = "patrol_log"
    __table_args__ = (Index("idx_patrol_log_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    operator: Mapped[str] = mapped_column(String(32), default="system")
    mode: Mapped[str] = mapped_column(String(16), default="daily")
    department: Mapped[str | None] = mapped_column(String(16), nullable=True)
    screened: Mapped[int] = mapped_column(Integer, default=0)
    skipped_recent: Mapped[int] = mapped_column(Integer, default=0)
    candidates: Mapped[int] = mapped_column(Integer, default=0)
    selected: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[int] = mapped_column(Integer, default=0)
    degraded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)


class Notification(Base):
    """定时任务推送（模拟企业微信/钉钉）。"""

    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), default="")
    channel: Mapped[str] = mapped_column(String(16), default="企微")
    title: Mapped[str] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text)
    sent: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)


# ---------------------------------------------------------------- 四层记忆
class ToolResult(Base):
    """L0 工作记忆：每次工具返回的登记表（result_id / as_of / summary / artifact 路径）。

    回答必须引用 result_id，因此每次取数都可回溯到「口径版本 + 时间 + 明细文件」。
    """

    __tablename__ = "tool_result"
    __table_args__ = (
        Index("idx_tool_result_session", "session_id", "id"),
        Index("idx_tool_result_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    result_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    session_id: Mapped[str] = mapped_column(String(36), default="")
    username: Mapped[str] = mapped_column(String(32), default="")
    tool: Mapped[str] = mapped_column(String(48), default="")
    as_of: Mapped[str] = mapped_column(String(32), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    artifact_path: Mapped[str] = mapped_column(String(220), default="")
    pruned: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)


class ChatMemory(Base):
    """L1 会话记忆：事实卡（结论骨架）+ 工作集（实体表），与会话一对一。"""

    __tablename__ = "chat_memory"

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chat_session.id"), primary_key=True
    )
    facts_card: Mapped[str] = mapped_column(Text, default="")
    facts_version: Mapped[int] = mapped_column(Integer, default=0)
    facts_hash: Mapped[str] = mapped_column(String(40), default="")
    facts_trigger: Mapped[str] = mapped_column(String(32), default="")
    facts_as_of: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    working_set: Mapped[dict] = mapped_column(JSON, default=dict)
    working_version: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime)


class ChatQueryPlan(Base):
    """查询理解与规划结果（只存不默认展示；前端勾选 Query 重写时展示当轮计划）。"""

    __tablename__ = "chat_query_plan"
    __table_args__ = (Index("idx_chat_plan_session", "session_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), default="")
    message_id: Mapped[int] = mapped_column(Integer, default=0)
    username: Mapped[str] = mapped_column(String(32), default="")
    department: Mapped[str | None] = mapped_column(String(16), nullable=True)
    plan_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    intent: Mapped[str] = mapped_column(String(32), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    asked_clarify: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)


class AnalysisResult(Base):
    """L3 组织记忆：可复用的分析结论档案（cross-session / cross-week），部门隔离即权限。"""

    __tablename__ = "analysis_result"
    __table_args__ = (
        Index("idx_analysis_dept", "department", "created_at"),
        Index("idx_analysis_created", "created_at"),
        # 内容指纹：同一结论重复沉淀只保留一份（用普通索引 + 查询判重，
        # 因为历史档案里已经存在重复内容，加唯一约束会让老库迁移直接失败）
        Index("idx_analysis_hash", "content_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    department: Mapped[str] = mapped_column(String(16), default="")
    username: Mapped[str] = mapped_column(String(32), default="")
    session_id: Mapped[str] = mapped_column(String(36), default="")
    plan_id: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(16), default="chat")  # chat / patrol / manual
    question: Mapped[str] = mapped_column(Text, default="")
    conclusion: Mapped[str] = mapped_column(Text, default="")
    key_metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence: Mapped[list] = mapped_column(JSON, default=list)  # result_id 列表
    sql_fingerprints: Mapped[list] = mapped_column(JSON, default=list)
    metric_version: Mapped[str] = mapped_column(String(32), default="")
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str] = mapped_column(String(64), default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    markdown_path: Mapped[str] = mapped_column(String(220), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)


class ChatTurnLog(Base):
    """单轮对话运行详情（可观测性）：计划 / 工具步骤 / 记忆快照 / token / 权限。

    新表而非给 chat_message 加列：`create_all` 不会 ALTER 既有表，新表可直接生效，
    演示环境无需迁移脚本；每会话只保留最近 N 轮（见 chat_store.prune_turn_logs）。

    下面这些**扁平列**是给聚合查询用的（per-agent 看板）：JSON 里的 `payload` 也能算，
    但每次看板都要扫一遍 JSON，几万轮之后就是全表扫描。列 + 索引让
    「哪个 Agent 慢 / 贵 / 常降级」变成一次 GROUP BY。
    """

    __tablename__ = "chat_turn_log"
    __table_args__ = (
        Index("idx_turn_log_session", "session_id", "id"),
        Index("idx_turn_log_message", "message_id"),
        Index("idx_turn_log_agent", "agent_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), default="")
    message_id: Mapped[int] = mapped_column(Integer, default=0)  # 对应 chat_message.id
    turn_id: Mapped[str] = mapped_column(String(32), default="")
    username: Mapped[str] = mapped_column(String(32), default="")
    plan_id: Mapped[str] = mapped_column(String(64), default="")
    intent: Mapped[str] = mapped_column(String(32), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    # ---- 聚合维度（per-agent 看板 / 成本归因 / 降级追踪 / trace 对账）----
    agent_id: Mapped[str] = mapped_column(String(32), default="")
    agent_version: Mapped[str] = mapped_column(String(16), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_cny: Mapped[float] = mapped_column(Float, default=0.0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    grounding_status: Mapped[str] = mapped_column(String(16), default="")
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)


# ---------------------------------------------------------------- 订单 / 物流 / 售后（MCP mock）
class MockOrder(Base):
    """订单（演示数据；真实环境由订单中心 MCP 提供）。"""

    __tablename__ = "mock_order"
    __table_args__ = (Index("idx_mock_order_sku", "sku_id", "created_at"),)

    order_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    sku_id: Mapped[str] = mapped_column(String(32), index=True)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="paid")
    channel: Mapped[str] = mapped_column(String(16), default="淘系")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    shipped_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class MockLogistics(Base):
    """物流单（一单一物流）。"""

    __tablename__ = "mock_logistics"

    order_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    carrier: Mapped[str] = mapped_column(String(32), default="")
    tracking_no: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="已揽收")
    eta: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime)


class AfterSalesTicket(Base):
    """售后工单（写操作带幂等键：同订单 + 同原因 + 同数量只建一张）。"""

    __tablename__ = "after_sales_ticket"
    __table_args__ = (
        Index("idx_after_sales_order", "order_id"),
        Index("idx_after_sales_sku", "sku_id"),
        UniqueConstraint("idempotency_key", name="uq_after_sales_idem"),
    )

    ticket_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(32), index=True)
    sku_id: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(String(64), default="")
    qty: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="pending_confirmation")
    idempotency_key: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    # 人工确认结果（与补货审批同构：谁批的、什么时候批的、批注）
    approver: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)


class UserProfile(Base):
    """用户偏好画像（L1 长期记忆）：跨会话持久化"这个人怎么用"。

    与聊天历史（短期）、分析档案（L3 结论）区分：这里只存偏好与关注清单，
    每次对话注入系统提示（≤200 字），不进入事实卡/工作集。
    """

    __tablename__ = "user_profile"

    username: Mapped[str] = mapped_column(String(32), primary_key=True)
    department: Mapped[str] = mapped_column(String(16), default="")
    prefs: Mapped[dict] = mapped_column(JSON, default=dict)
    watch_skus: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime)

class CheckpointThread(Base):
    """分析线程使用记录（checkpointer 持久化 + 线程数上限淘汰用）。"""

    __tablename__ = "checkpoint_thread"

    thread_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime)


class RevokedToken(Base):
    """已注销 token 的持久化黑名单（重启后仍拒绝；只存 hash，不存原文）。"""

    __tablename__ = "revoked_token"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    exp: Mapped[dt.datetime] = mapped_column(DateTime)
    revoked_at: Mapped[dt.datetime] = mapped_column(DateTime)
