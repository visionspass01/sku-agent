"""使用量：按小时流水记录交付/尝试/任务/失败/成本，管理员面板按周期聚合。

为什么按小时落：面板要支持「月/周/日/小时」四种粒度 + 近 30 个桶。
小时粒度是最细的原子事实，其它粒度都可以从它聚合，避免维护四份计数互相打架。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.agents_image.store import quota as quota_store
from app.agents_image.store.models import ImageUsage

GRANULARITIES = ("hour", "day", "week", "month")


def hour_key(now: dt.datetime | None = None) -> str:
    return quota_store.now_local(now).strftime("%Y-%m-%dT%H")


def record(session, *, username: str, department: str | None = None,
           delivered: int = 0, attempted: int = 0, tasks: int = 0, failed: int = 0,
           tokens: int = 0, cost: float = 0.0, now: dt.datetime | None = None) -> ImageUsage:
    """累加当前小时的计数（同一人同一小时只有一行）。"""
    key = hour_key(now)
    row = session.execute(
        select(ImageUsage).where(ImageUsage.username == username,
                                 ImageUsage.period_key == key,
                                 ImageUsage.granularity == "hour")
    ).scalars().first()
    if row is None:
        row = ImageUsage(username=username, department=department, period_key=key,
                         granularity="hour", updated_at=dt.datetime.now())
        session.add(row)
    row.images = int(row.images or 0) + max(0, int(delivered))
    row.images_attempted = int(row.images_attempted or 0) + max(0, int(attempted))
    row.tasks = int(row.tasks or 0) + max(0, int(tasks))
    row.failed = int(row.failed or 0) + max(0, int(failed))
    row.tokens = int(row.tokens or 0) + max(0, int(tokens))
    row.cost_cny = float(row.cost_cny or 0.0) + max(0.0, float(cost))
    row.updated_at = dt.datetime.now()
    session.flush()
    return row


def _bucket_key(moment: dt.datetime, granularity: str) -> str:
    if granularity == "hour":
        return moment.strftime("%Y-%m-%dT%H")
    if granularity == "week":
        year, week, _ = moment.isocalendar()
        return f"{year}-W{week:02d}"
    if granularity == "month":
        return moment.strftime("%Y-%m")
    return moment.strftime("%Y-%m-%d")


def _bucket_start(moment: dt.datetime, granularity: str) -> dt.datetime:
    if granularity == "hour":
        return moment.replace(minute=0, second=0, microsecond=0)
    if granularity == "week":
        return (moment - dt.timedelta(days=moment.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
    if granularity == "month":
        return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _advance(moment: dt.datetime, granularity: str) -> dt.datetime:
    if granularity == "hour":
        return moment + dt.timedelta(hours=1)
    if granularity == "week":
        return moment + dt.timedelta(days=7)
    if granularity == "month":
        year = moment.year + (1 if moment.month == 12 else 0)
        month = 1 if moment.month == 12 else moment.month + 1
        return moment.replace(year=year, month=month, day=1)
    return moment + dt.timedelta(days=1)


def _row_moment(period_key: str) -> dt.datetime | None:
    try:
        if "T" in period_key:
            return dt.datetime.strptime(period_key, "%Y-%m-%dT%H")
        if "-W" in period_key:
            year, week = period_key.split("-W")
            return dt.datetime.fromisocalendar(int(year), int(week), 1)
        if len(period_key) == 7:
            return dt.datetime.strptime(period_key, "%Y-%m")
        return dt.datetime.strptime(period_key, "%Y-%m-%d")
    except (ValueError, AttributeError):
        return None


def buckets(session, *, granularity: str = "day", count: int = 30,
            username: str | None = None, now: dt.datetime | None = None) -> list[dict]:
    """近 `count` 个周期桶（含当前桶），每桶交付/尝试/任务/失败/成本 + 按账户细分。"""
    granularity = granularity if granularity in GRANULARITIES else "day"
    count = max(1, min(int(count or 30), 120))
    moment = quota_store.now_local(now)
    start = _bucket_start(moment, granularity)
    for _ in range(count - 1):
        start = _advance(_bucket_start(start - dt.timedelta(seconds=1), granularity), granularity)
    # 上面的推进可能落到上一周期，统一重算成「首桶 = 当前桶往前 (count-1) 个」
    keys: list[str] = []
    cursor = _bucket_start(moment, granularity)
    for _ in range(count):
        keys.append(_bucket_key(cursor, granularity))
        cursor = _advance(cursor, granularity)
    keys = list(reversed(keys))          # 时间升序（图表从左到右）

    statement = select(ImageUsage).where(ImageUsage.granularity == "hour")
    if username:
        statement = statement.where(ImageUsage.username == username)
    rows = session.execute(statement).scalars().all()

    buckets: dict[str, dict] = {key: {"key": key, "images": 0, "images_attempted": 0,
                                      "tasks": 0, "failed": 0, "cost_cny": 0.0,
                                      "by_user": {}} for key in keys}
    for row in rows:
        moment_of_row = _row_moment(row.period_key or "")
        if moment_of_row is None:
            continue
        key = _bucket_key(moment_of_row, granularity)
        bucket = buckets.get(key)
        if bucket is None:
            continue
        images = int(row.images or 0)
        attempted = int(row.images_attempted or 0)
        bucket["images"] += images
        bucket["images_attempted"] += attempted
        bucket["tasks"] += int(row.tasks or 0)
        bucket["failed"] += int(row.failed or 0)
        bucket["cost_cny"] = round(bucket["cost_cny"] + float(row.cost_cny or 0.0), 4)
        user = bucket["by_user"].setdefault(row.username, {"images": 0, "tasks": 0,
                                                           "images_attempted": 0})
        user["images"] += images
        user["tasks"] += int(row.tasks or 0)
        user["images_attempted"] += attempted
    return [buckets[key] for key in keys]


def totals(session, *, username: str | None = None) -> dict:
    statement = select(ImageUsage)
    if username:
        statement = statement.where(ImageUsage.username == username)
    rows = session.execute(statement).scalars().all()
    return {
        "images": sum(int(row.images or 0) for row in rows),
        "images_attempted": sum(int(row.images_attempted or 0) for row in rows),
        "tasks": sum(int(row.tasks or 0) for row in rows),
        "failed": sum(int(row.failed or 0) for row in rows),
        "cost_cny": round(sum(float(row.cost_cny or 0.0) for row in rows), 4),
        "tokens": sum(int(row.tokens or 0) for row in rows),
        "adopted": sum(int(row.adopted or 0) for row in rows),
        "rejected": sum(int(row.rejected or 0) for row in rows),
    }


__all__ = ["GRANULARITIES", "hour_key", "record", "buckets", "totals"]
