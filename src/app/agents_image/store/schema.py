"""`image_*` 表的建表入口：**只建自己的表**，不动 SKU 的表。

D1 阶段还没有表（表在 M1/D2–D3 加入）；这里先提供稳定入口，后续里程碑只改 `_tables()`。
"""

from __future__ import annotations

import logging

from app.db.engine import get_engine

logger = logging.getLogger("sku-agent.image.schema")


def _tables() -> list:
    """返回需要 create_all 的表对象；模型还没定义时返回空列表。"""
    try:
        from app.agents_image.store import models  # noqa: F401 —— 导入即注册到 Base.metadata
    except ImportError:  # pragma: no cover —— D1 阶段还没有 models 模块
        return []
    names = (
        "image_user", "image_task", "image_asset", "image_memory",
        "image_quota", "image_usage", "image_outbox", "image_token_blacklist",
    )
    tables = []
    for name in names:
        table = models.Base.metadata.tables.get(name)
        if table is not None:
            tables.append(table)
    return tables


def ensure_image_schema() -> dict:
    """幂等建表；返回 {"created": [...], "skipped": [...]} 便于日志/测试断言。"""
    engine = get_engine()
    tables = _tables()
    if not tables:
        return {"created": [], "skipped": []}
    models_base = tables[0].metadata
    models_base.create_all(engine, tables=tables)
    return {"created": [t.name for t in tables], "skipped": []}
