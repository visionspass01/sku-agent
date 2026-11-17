"""回归：外键强制开启时（MySQL 生产行为）演示数据必须能灌进去。

背景：SQLite 默认不校验外键，而 `Sku` / `InTransit` / `TransferOrder` / `EvalLabel`
之间只有 ForeignKey 列、没有 relationship，SQLAlchemy 不保证插入顺序——
线上 MySQL 因此曾出现 `in_transit` 先于 `sku` 落库、整个种子事务回滚（库里 0 行）。
本用例打开 `PRAGMA foreign_keys=ON`，把这个问题钉在离线测试里。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from app.db.models import Base, EvalLabel, InTransit, Sku, TransferOrder


def _engine_with_fk(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'seed_fk.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


def test_seed_succeeds_with_foreign_keys_enforced(tmp_path):
    from app.db.seed import seed_database

    engine = _engine_with_fk(tmp_path)

    with Session(engine) as session:
        # 外键真的生效（否则本用例失去意义）
        assert session.execute(select(func.count()).select_from(Sku)).scalar() == 0
        summary = seed_database(session, now=dt.datetime(2026, 3, 1, 10, 0), n_days=2)
        session.commit()

    with Session(engine) as session:
        skus = session.execute(select(func.count()).select_from(Sku)).scalar()
        in_transit = session.execute(select(func.count()).select_from(InTransit)).scalar()
        transfers = session.execute(select(func.count()).select_from(TransferOrder)).scalar()
        labels = session.execute(select(func.count()).select_from(EvalLabel)).scalar()

    assert skus == 300, "20 款 × 3 色 × 5 码"
    assert labels == 300, "每个 SKU 一条评估标签"
    assert in_transit > 0, "约 20% SKU 有在途，缺货款必然有"
    assert transfers > 0, "断码/缺货/积压款会注入调拨单"
    assert summary.skus == 300
