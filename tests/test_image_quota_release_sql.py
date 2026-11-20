"""第八轮 P0-1：配额释放的 SQL 必须两种方言都能编译/执行。

MySQL 的 `MAX()` 只有聚合形态（标量函数是 `GREATEST`），`func.max(a, b)` 在 MySQL 8
上会编译成 `max(a, b)` 并直接报 ERROR 1582；而 compose 部署默认用 MySQL，
后果是交付/失败/取消三条结算路径全部抛错、额度不释放。
现在改用 `CASE WHEN`：SQLite 与 MySQL 都编译成标准 SQL。
"""

from __future__ import annotations

import sqlalchemy
from sqlalchemy import func, update
from sqlalchemy.dialects import mysql, sqlite

from app.agents_image.store import quota as quota_store
from app.agents_image.store.models import ImageQuota


def _release_sql(dialect) -> str:
    """把与 `release()` 等价的 UPDATE 编译成目标方言的 SQL。"""
    remaining_after = ImageQuota.reserved - 2
    statement = (
        update(ImageQuota)
        .where(ImageQuota.username == "u")
        .values(reserved=sqlalchemy.case((remaining_after < 0, 0), else_=remaining_after))
    )
    return str(statement.compile(dialect=dialect))


def test_release_sql_is_portable_across_dialects():
    for dialect in (sqlite.dialect(), mysql.dialect()):
        sql = _release_sql(dialect)
        assert "CASE WHEN" in sql.upper(), sql
        assert "MAX(" not in sql.upper(), f"不得再出现双参数 max()：{sql}"
        # 反向确认：旧写法在 MySQL 方言下确实会编出双参数 max()
        legacy = str(
            update(ImageQuota)
            .where(ImageQuota.username == "u")
            .values(reserved=func.max(ImageQuota.reserved - 2, 0))
            .compile(dialect=mysql.dialect())
        )
        assert "max(image_quota.reserved" in legacy.lower(), legacy


def test_release_clamps_to_zero_sqlite(_db):
    """行为回归（SQLite 真跑）：预留 3 张 → 释放 2 张 → 1 张；再释放 10 张 → 0 张。"""
    from app.agents_image.store.schema import ensure_image_schema
    from app.db.engine import session_scope

    ensure_image_schema()
    user = "u-release-sql"
    with session_scope() as session:
        quota_store.grant(session, user, period_type="day", period_limit=10)
        assert quota_store.reserve(session, user, 3)
        row = session.get(ImageQuota, user)
        assert int(row.reserved) == 3
        assert quota_store.release(session, user, 2)
        row = session.get(ImageQuota, user)
        assert int(row.reserved) == 1
        assert quota_store.release(session, user, 10)
        row = session.get(ImageQuota, user)
        assert int(row.reserved) == 0
        assert int(row.used) == 0
