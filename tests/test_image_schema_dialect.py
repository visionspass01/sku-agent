"""第六轮 P0-1：索引补齐必须兼容 MySQL（`CREATE INDEX IF NOT EXISTS` 只有 SQLite/MariaDB 支持）。

上一版在 MySQL 8 上会让 `_INDEXES` 里**每条**索引都报语法错 → 索引一个都建不上，
`/health.degraded` 永久为真（真降级告警被淹没），本轮新增的 dedupe 唯一索引也全缺失。
现在改成「先查已有索引名（按方言）→ 再普通 CREATE」，两种方言都能跑。
"""

from __future__ import annotations

import sqlalchemy
from sqlalchemy import text

from app.agents_image.store import schema as schema_module
from app.agents_image.store.schema import (
    _INDEXES,
    _duplicate_index_error,
    _existing_index_names,
    ensure_image_schema,
    schema_status,
)


def test_no_if_not_exists_sql_anymore():
    """源码里不应再拼 `CREATE ... IF NOT EXISTS`（MySQL 不支持该语法）。"""
    import pathlib

    source = pathlib.Path(schema_module.__file__).read_text(encoding="utf-8")
    assert "IF NOT EXISTS {name}" not in source
    assert "CREATE {keyword} IF NOT EXISTS" not in source
    assert "information_schema.statistics" in source


def test_ensure_indexes_is_idempotent_on_second_call(_db):
    """第二次调用必须识别"索引已存在"并跳过，不产生 degraded。"""
    ensure_image_schema()
    before = schema_status()["degraded"]
    result = ensure_image_schema()
    assert result["indexes"] == [], result["indexes"]
    assert schema_status()["degraded"] is before


def test_missing_index_is_recreated(_db):
    """把某条索引手工删掉后再 ensure，应被重新建出来。"""
    engine = sqlalchemy.create_engine("sqlite://")
    # 直接用项目 engine：先确保结构在
    from app.db.engine import get_engine

    real_engine = get_engine()
    ensure_image_schema()
    name, table, columns, unique = _INDEXES[-1]
    with real_engine.begin() as conn:
        conn.execute(text(f"DROP INDEX IF EXISTS {name}"))
        remaining = _existing_index_names(conn, table, "sqlite")
    assert name not in remaining

    result = ensure_image_schema()
    assert name in result["indexes"], result
    with real_engine.begin() as conn:
        assert name in _existing_index_names(conn, table, "sqlite")
    engine.dispose()


def test_existing_index_names_reads_both_indexes(_db):
    """`_existing_index_names` 必须能列出模型声明 + _INDEXES 两边的索引。"""
    from app.db.engine import get_engine

    ensure_image_schema()
    with get_engine().begin() as conn:
        names = _existing_index_names(conn, "image_usage", "sqlite")
    assert "uq_image_usage_user_period" in names       # models.__table_args__
    assert "idx_image_usage_scope" in names            # 只在 _INDEXES 里


def test_duplicate_index_error_is_recognized():
    assert _duplicate_index_error(Exception("(1061, 'Duplicate key name')"))
    assert _duplicate_index_error(Exception("index uq_x already exists"))
    assert not _duplicate_index_error(Exception("syntax error near IF"))
