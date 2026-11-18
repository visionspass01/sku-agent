"""`image_*` 表的建表 + **列/索引补齐**（老库就地升级，SQLite/MySQL 都能跑）。

为什么要补列：`create_all` 只建缺失的表，不会给已有表加列；本项目是"演示库就地升级"的用法，
所以按 `_COLUMNS` 清单做幂等 `ALTER TABLE ADD COLUMN`，并按 `_INDEXES` 建唯一索引。
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from app.db.engine import get_engine

logger = logging.getLogger("sku-agent.image.schema")

# (表, 列, DDL 片段) —— 只加列，不改类型、不删列
_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("image_task", "color_key", "VARCHAR(64) DEFAULT ''"),
    ("image_task", "variant_id", "VARCHAR(8) DEFAULT ''"),
    ("image_task", "slot_count", "INTEGER DEFAULT 9"),
    ("image_task", "image_count", "INTEGER DEFAULT 6"),
    ("image_task", "copy_json", "JSON"),
    ("image_task", "external_cost_cny", "FLOAT DEFAULT 0"),
    ("image_task", "provider_started_at", "DATETIME"),
    ("image_task", "cancel_requested", "BOOLEAN DEFAULT 0"),
    ("image_task", "base_task_id", "VARCHAR(32)"),
    ("image_task", "reuse_asset_ids", "JSON"),
    ("image_task", "reused_images", "INTEGER DEFAULT 0"),
    ("image_task", "listing_status", "VARCHAR(16) DEFAULT 'not_listed'"),
    ("image_task", "listing_url", "TEXT"),
    ("image_task", "listed_at", "DATETIME"),
    ("image_asset", "color_key", "VARCHAR(64) DEFAULT ''"),
    ("image_asset", "adopt_status", "VARCHAR(16) DEFAULT 'pending'"),
    ("image_asset", "blocked_reason", "VARCHAR(64) DEFAULT ''"),
    ("image_asset", "purged_at", "DATETIME"),
    ("image_asset", "adopt_reason", "TEXT"),
    ("image_quota", "reserved", "INTEGER DEFAULT 0"),
    ("image_usage", "adopted", "INTEGER DEFAULT 0"),
    ("image_usage", "rejected", "INTEGER DEFAULT 0"),
    ("image_memory", "sample_kind", "VARCHAR(16) DEFAULT 'auto'"),
    ("image_memory", "adopt_count", "INTEGER DEFAULT 0"),
)

# (索引名, 表, 列, 是否唯一)
_INDEXES: tuple[tuple[str, str, tuple[str, ...], bool], ...] = (
    ("uq_image_task_batch_target", "image_task",
     ("batch_id", "product_id", "color_key", "variant_id", "platform"), True),
    ("idx_image_task_color", "image_task", ("color_key",), False),
    ("uq_image_usage_user_period", "image_usage",
     ("username", "period_key", "granularity"), True),
    ("idx_image_asset_adopt", "image_asset", ("adopt_status",), False),
)


def _tables() -> list:
    """返回需要 create_all 的表对象；模型还没定义时返回空列表。"""
    try:
        from app.agents_image.store import models  # noqa: F401 —— 导入即注册到 Base.metadata
    except ImportError:  # pragma: no cover
        return []
    # `user` 是**共享人员账号表**（SKU 与生图共用，见 app.core.accounts）：
    # image-only 模式也要能独立建它，否则登录找不到账号。
    names = (
        "user", "image_request", "image_task", "image_asset", "image_memory",
        "image_quota", "image_usage", "image_outbox", "image_token_blacklist",
        "image_feedback",
    )
    tables = []
    for name in names:
        table = models.Base.metadata.tables.get(name)
        if table is not None:
            tables.append(table)
    return tables


def _existing_columns(engine, table: str) -> set[str]:
    from sqlalchemy import inspect

    try:
        return {column["name"] for column in inspect(engine).get_columns(table)}
    except Exception:  # noqa: BLE001 —— 表不存在/方言差异
        return set()


def _ensure_columns(engine) -> list[str]:
    added: list[str] = []
    for table, column, ddl in _COLUMNS:
        if column in _existing_columns(engine, table):
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            added.append(f"{table}.{column}")
        except Exception as exc:  # noqa: BLE001 —— 列已存在/并发升级
            logger.debug("补列跳过 %s.%s：%s", table, column, exc)
    return added


def _ensure_indexes(engine) -> list[str]:
    created: list[str] = []
    for name, table, columns, unique in _INDEXES:
        keyword = "UNIQUE INDEX" if unique else "INDEX"
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    f"CREATE {keyword} IF NOT EXISTS {name} ON {table} "
                    f"({', '.join(columns)})"))
            created.append(name)
        except Exception as exc:  # noqa: BLE001 —— 历史数据有重复时唯一索引会失败
            logger.warning("建索引失败 %s（可能有历史重复数据）：%s", name, exc)
    return created


def ensure_image_schema() -> dict:
    """幂等建表 + 补列/补索引；返回 {"created": [...], "columns": [...], "indexes": [...]}。"""
    engine = get_engine()
    tables = _tables()
    if not tables:
        return {"created": [], "columns": [], "indexes": []}
    models_base = tables[0].metadata
    models_base.create_all(engine, tables=tables)
    columns = _ensure_columns(engine)
    indexes = _ensure_indexes(engine)
    return {"created": [t.name for t in tables], "columns": columns, "indexes": indexes}
