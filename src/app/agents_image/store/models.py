"""生图 Agent 的表模型（挂在 SKU 的 `Base` 上，但表名全部 `image_*`，互不干扰）。

已建：任务表 / 资产表（D2–D3）、账号表 / 令牌黑名单（D4）；配额 / 使用量 / 记忆 /
outbox 在后续里程碑加入（`ensure_image_schema()` 只创建实际存在的表，所以可以逐个加）。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


class ImageTask(Base):
    """一次「参考图 → 目标图」任务；状态全在 DB（无状态任务设计）。"""

    __tablename__ = "image_task"

    task_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), default="")
    batch_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    username: Mapped[str] = mapped_column(String(32), default="anonymous", index=True)
    department: Mapped[str | None] = mapped_column(String(32), nullable=True)

    product_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    ref_source: Mapped[str] = mapped_column(String(16), default="mcp")   # mcp（唯一来源）
    ref_asset_ids: Mapped[list] = mapped_column(JSON, default=list)
    prompt: Mapped[str] = mapped_column(Text, default="")
    prompt_final: Mapped[str] = mapped_column(Text, default="")
    platform: Mapped[str] = mapped_column(String(16), default="淘系")
    aspect: Mapped[str] = mapped_column(String(8), default="1:1")
    n: Mapped[int] = mapped_column(Integer, default=1)
    compositions: Mapped[list] = mapped_column(JSON, default=list)
    scene_styles: Mapped[list] = mapped_column(JSON, default=list)
    attrs_json: Mapped[dict] = mapped_column(JSON, default=dict)   # 中台商品身份锁定字段快照
    slots_json: Mapped[list] = mapped_column(JSON, default=list)
    prompt_pack_json: Mapped[list] = mapped_column(JSON, default=list)

    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    attempted_images: Mapped[int] = mapped_column(Integer, default=0)
    delivered_images: Mapped[int] = mapped_column(Integer, default=0)
    cost_cny: Mapped[float] = mapped_column(default=0.0)
    error_code: Mapped[str] = mapped_column(String(48), default="")
    error_detail: Mapped[str] = mapped_column(Text, default="")
    mcp_degraded: Mapped[bool] = mapped_column(default=False)   # 仅历史兼容，v6 起恒为 False
    backfill_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/sent/failed

    trace_id: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_image_task_user_created", "username", "created_at"),
        Index("idx_image_task_status", "status"),
    )


class ImageAsset(Base):
    """图片资产元数据（上传件 / 产出 / 缩略图）；sha256 去重。"""

    __tablename__ = "image_asset"

    asset_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("image_task.task_id"), nullable=True, index=True
    )
    slot: Mapped[int] = mapped_column(Integer, default=0)
    username: Mapped[str] = mapped_column(String(32), default="anonymous", index=True)
    department: Mapped[str | None] = mapped_column(String(32), nullable=True)
    product_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="ref")   # ref | output | thumb
    source: Mapped[str] = mapped_column(String(16), default="mcp")  # mcp（v6：参考图只来自中台）
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage: Mapped[str] = mapped_column(String(16), default="local")
    key: Mapped[str] = mapped_column(String(256), default="")
    mime: Mapped[str] = mapped_column(String(32), default="image/png")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now)

    __table_args__ = (
        Index("idx_image_asset_sha_kind", "sha256", "kind"),
        Index("idx_image_asset_task", "task_id", "slot"),
    )


# 说明：人员账号已统一到共享表 user（app.core.accounts + app.db.models.AppUser），
# 旧的 image_user 表只做一次性迁移读取（见 store/users.py::migrate_legacy_users）。


class ImageTokenBlacklist(Base):
    """已登出/已踢下线的令牌（jti），到期后由清理任务删除。"""

    __tablename__ = "image_token_blacklist"

    jti: Mapped[str] = mapped_column(String(32), primary_key=True)
    exp: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    reason: Mapped[str] = mapped_column(String(32), default="logout")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now)


class ImageMemory(Base):
    """失败签名 → 修复经验（Critic 不通过时归档，下次同因优先复用）。"""

    __tablename__ = "image_memory"

    memory_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    department: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_signature: Mapped[str] = mapped_column(String(120), default="", index=True)
    fix_summary: Mapped[str] = mapped_column(Text, default="")
    prompt_patch: Mapped[str] = mapped_column(Text, default="")
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now)


class ImageQuota(Base):
    """按人按周期的配额（自然周期 Asia/Shanghai；管理员不受限）。"""

    __tablename__ = "image_quota"

    username: Mapped[str] = mapped_column(String(32), primary_key=True)
    period_type: Mapped[str] = mapped_column(String(16), default="day")   # day|week|month|unlimited
    period_limit: Mapped[int] = mapped_column(Integer, default=50)
    period_key: Mapped[str] = mapped_column(String(16), default="")
    used: Mapped[int] = mapped_column(Integer, default=0)
    granted_by: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now)


class ImageUsage(Base):
    """使用量流水（按小时粒度落，面板按周期聚合；交付与尝试分开记）。"""

    __tablename__ = "image_usage"
    __table_args__ = (
        Index("idx_image_usage_user_period", "username", "period_key"),
        Index("idx_image_usage_period", "period_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), default="", index=True)
    department: Mapped[str | None] = mapped_column(String(32), nullable=True)
    period_key: Mapped[str] = mapped_column(String(20), default="")   # 2026-11-18T15（Asia/Shanghai）
    granularity: Mapped[str] = mapped_column(String(8), default="hour")
    images: Mapped[int] = mapped_column(Integer, default=0)            # 交付张数
    images_attempted: Mapped[int] = mapped_column(Integer, default=0)  # 尝试张数
    tasks: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_cny: Mapped[float] = mapped_column(default=0.0)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now)


class ImageOutbox(Base):
    """回写中台失败的补偿队列（D12）：可重放、可观测，不丢数据。"""

    __tablename__ = "image_outbox"
    __table_args__ = (Index("idx_image_outbox_status", "status", "next_retry_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|sent|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now)
