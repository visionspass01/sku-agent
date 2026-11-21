"""小时快照聚合：读取 ERP 出库流水 → 写入 stat_hour 快照表。

设计对应：定时任务每小时读取 ERP 出库流水，统计全量 SKU 过去 1 小时销量写入
MySQL 小时快照表（stat_hour + sku_id 联合索引）。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, func, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Flow, StatHour


def _hour_bucket_expr(engine: Engine):
    """SQLite / MySQL 的小时段截断表达式。"""
    dialect = engine.dialect.name
    if dialect == "mysql":
        return func.date_format(Flow.ts, "%Y-%m-%d %H:00:00")
    return func.strftime("%Y-%m-%d %H:00:00", Flow.ts)


def rebuild_stat_hours(session: Session, start: dt.datetime, end: dt.datetime) -> int:
    """重算 [start, end] 内的快照（幂等：先删后写）。返回写入行数。"""
    engine = session.get_bind()
    session.execute(
        delete(StatHour).where(StatHour.stat_hour >= start, StatHour.stat_hour <= end)
    )
    bucket = _hour_bucket_expr(engine)
    rows = session.execute(
        select(bucket.label("h"), Flow.sku_id, func.sum(Flow.qty), func.sum(Flow.amt))
        .where(Flow.ts >= start, Flow.ts <= end)
        .group_by(bucket, Flow.sku_id)
    ).all()
    batch: list[dict] = []
    for hour_str, sku_id, qty, amt in rows:
        batch.append({
            "stat_hour": dt.datetime.strptime(hour_str, "%Y-%m-%d %H:00:00"),
            "sku_id": sku_id,
            "qty": int(qty or 0),
            "amt": round(float(amt or 0), 2),
            "orders": max(1, int((qty or 0) // 2)),
        })
    for i in range(0, len(batch), 5000):
        session.execute(insert(StatHour), batch[i:i + 5000])
    return len(batch)


def aggregate_hour(session: Session, hour_start: dt.datetime) -> int:
    """聚合单个小时窗口并 upsert（定时任务入口）。"""
    hour_end = hour_start + dt.timedelta(hours=1) - dt.timedelta(seconds=1)
    session.execute(
        delete(StatHour).where(
            StatHour.stat_hour == hour_start
        )
    )
    engine = session.get_bind()
    bucket = _hour_bucket_expr(engine)
    rows = session.execute(
        select(bucket.label("h"), Flow.sku_id, func.sum(Flow.qty), func.sum(Flow.amt))
        .where(Flow.ts >= hour_start, Flow.ts <= hour_end)
        .group_by(bucket, Flow.sku_id)
    ).all()
    batch = [
        {
            "stat_hour": dt.datetime.strptime(h, "%Y-%m-%d %H:00:00"),
            "sku_id": sku_id,
            "qty": int(qty or 0),
            "amt": round(float(amt or 0), 2),
            "orders": max(1, int((qty or 0) // 2)),
        }
        for h, sku_id, qty, amt in rows
    ]
    for i in range(0, len(batch), 5000):
        session.execute(insert(StatHour), batch[i:i + 5000])
    return len(batch)
