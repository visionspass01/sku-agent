"""使用量：按小时流水记录交付/尝试/任务/失败/成本，管理员面板按周期聚合。

为什么按小时落：面板要支持「月/周/日/小时」四种粒度 + 近 30 个桶。
小时粒度是最细的原子事实，其它粒度都可以从它聚合，避免维护四份计数互相打架。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select, update

from app.agents_image.store import quota as quota_store
from app.agents_image.store.models import ImageUsage

GRANULARITIES = ("hour", "day", "week", "month")


def hour_key(now: dt.datetime | None = None) -> str:
    return quota_store.now_local(now).strftime("%Y-%m-%dT%H")


def record(session, *, username: str, department: str | None = None,
           delivered: int = 0, attempted: int = 0, tasks: int = 0, failed: int = 0,
           tokens: int = 0, cost: float = 0.0, llm_cost: float = 0.0,
           now: dt.datetime | None = None,
           **kwargs: int) -> ImageUsage:
    """累加当前小时的计数（同一人同一小时只有一行）。

    P2-3：并发下"先查后改"会写重复行/丢计数，这里改**原子 UPDATE + 冲突再 INSERT**
    （唯一索引 `uq_image_usage_user_period` 兜底），SQLite/MySQL 语义一致。
    """
    key = hour_key(now)
    increments = {
        "images": max(0, int(delivered)),
        "images_attempted": max(0, int(attempted)),
        "provider_calls": max(0, int(kwargs.pop("provider_calls", 0) or 0)),
        "agent_passed": max(0, int(kwargs.pop("agent_passed", 0) or 0)),
        "tasks": max(0, int(tasks)),
        "failed": max(0, int(failed)),
        "tokens": max(0, int(tokens)),
        "adopted": max(0, int(kwargs.pop("adopted", 0) or 0)),
        "reused_adopted": max(0, int(kwargs.pop("reused_adopted", 0) or 0)),
        "rejected": max(0, int(kwargs.pop("rejected", 0) or 0)),
    }
    del kwargs
    updated = session.execute(
        update(ImageUsage)
        .where(ImageUsage.username == username,
               ImageUsage.period_key == key,
               ImageUsage.granularity == "hour")
        .values(**{field: getattr(ImageUsage, field) + value
                   for field, value in increments.items() if value},
                cost_cny=ImageUsage.cost_cny + max(0.0, float(cost)),
                llm_cost_cny=ImageUsage.llm_cost_cny + max(0.0, float(llm_cost)),
                updated_at=dt.datetime.now())
    )
    if updated.rowcount:
        session.flush()
        return session.execute(
            select(ImageUsage).where(ImageUsage.username == username,
                                     ImageUsage.period_key == key,
                                     ImageUsage.granularity == "hour")
        ).scalars().first()
    row = ImageUsage(username=username, department=department, period_key=key,
                     granularity="hour", updated_at=dt.datetime.now(),
                     images=increments["images"], images_attempted=increments["images_attempted"],
                     provider_calls=increments["provider_calls"],
                     agent_passed=increments["agent_passed"],
                     tasks=increments["tasks"], failed=increments["failed"],
                     tokens=increments["tokens"], adopted=increments["adopted"],
                     reused_adopted=increments["reused_adopted"],
                     rejected=increments["rejected"], cost_cny=max(0.0, float(cost)),
                     llm_cost_cny=max(0.0, float(llm_cost)))
    session.add(row)
    try:
        session.flush()
    except Exception:  # noqa: BLE001 —— 并发插入撞唯一索引：回退成更新
        session.rollback()
        session.execute(
            update(ImageUsage)
            .where(ImageUsage.username == username,
                   ImageUsage.period_key == key,
                   ImageUsage.granularity == "hour")
            .values(**{field: getattr(ImageUsage, field) + value
                       for field, value in increments.items() if value},
                    cost_cny=ImageUsage.cost_cny + max(0.0, float(cost)),
                    llm_cost_cny=ImageUsage.llm_cost_cny + max(0.0, float(llm_cost)),
                    updated_at=dt.datetime.now())
        )
        session.flush()
        return session.execute(
            select(ImageUsage).where(ImageUsage.username == username,
                                     ImageUsage.period_key == key,
                                     ImageUsage.granularity == "hour")
        ).scalars().first()
    return row


def shift(session, *, username: str, department: str | None = None,
          now: dt.datetime | None = None, **deltas: int) -> bool:
    """对当前小时行做**可正可负**的增量（字段下限 0），用于「同一张图结论翻转」的回退。

    为什么需要它（第四轮 P1-2）：adopt → reject → adopt 过去每换一次态就 +1，
    `adopted` 被重复计成 2，采纳数不再等于「当前处于采纳态的图」，上架率随之虚高。
    现在统一按**当前终态**记账：先减旧态、再加新态；减到 0 为止（`CASE WHEN`，非负）。

    只更新已存在的小时行：没有行说明当时那一桶没记过账，负增量无处可退（返回 False）；
    但正增量仍会补建行，避免"新态的账没落"。
    """
    from sqlalchemy import case

    key = hour_key(now)
    raw = {field: int(deltas.get(field, 0) or 0)
           for field in ("adopted", "rejected", "reused_adopted")}
    if not any(raw.values()):
        return False
    values = {}
    for field, delta in raw.items():
        if not delta:
            continue
        column = getattr(ImageUsage, field)
        values[field] = case((column + delta < 0, 0), else_=column + delta)
    updated = session.execute(
        update(ImageUsage)
        .where(ImageUsage.username == username,
               ImageUsage.period_key == key,
               ImageUsage.granularity == "hour")
        .values(**values, updated_at=dt.datetime.now())
    )
    if updated.rowcount:
        session.flush()
        return True
    positive = {field: delta for field, delta in raw.items() if delta > 0}
    if not positive:
        return False
    record(session, username=username, department=department, now=now, **positive)
    return True


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
            username: str | None = None, department: str | None = None,
            now: dt.datetime | None = None) -> list[dict]:
    """近 `count` 个周期桶（含当前桶），每桶交付/尝试/任务/失败/成本 + 按账户细分。

    P2-2：支持按部门过滤（`image_usage.department`），与管理端卡片的部门视图同口径。
    第五轮 P2：**窗口下推**——只读首个桶起的小时行（过去把整表读进内存再分组，
    管理端轮询越久越慢），并且只取需要的列（不再构造 ORM 对象）。
    """
    granularity = granularity if granularity in GRANULARITIES else "day"
    count = max(1, min(int(count or 30), 120))
    moment = quota_store.now_local(now)
    keys: list[str] = []
    cursor = _bucket_start(moment, granularity)
    for _ in range(count):
        keys.append(_bucket_key(cursor, granularity))
        cursor = _advance(cursor, granularity)
    keys = list(reversed(keys))          # 时间升序（图表从左到右）

    # 首桶的起点 = 当前桶往前 (count-1) 个；据此算出最早的小时键（period_key 是字符串，可直接比较）
    first_bucket = _bucket_start(moment, granularity)
    for _ in range(count - 1):
        first_bucket = _bucket_start(first_bucket - dt.timedelta(seconds=1), granularity)
    window_start = first_bucket.strftime("%Y-%m-%dT%H")

    statement = (
        select(ImageUsage.username, ImageUsage.department, ImageUsage.period_key,
               ImageUsage.images, ImageUsage.images_attempted, ImageUsage.provider_calls,
               ImageUsage.agent_passed, ImageUsage.tasks, ImageUsage.failed,
               ImageUsage.cost_cny, ImageUsage.llm_cost_cny)
        .where(ImageUsage.granularity == "hour", ImageUsage.period_key >= window_start)
    )
    if username:
        statement = statement.where(ImageUsage.username == username)
    if department:
        statement = statement.where(ImageUsage.department == department)
    rows = session.execute(statement).all()

    buckets: dict[str, dict] = {key: {"key": key, "images": 0, "images_attempted": 0,
                                      "provider_calls": 0, "agent_passed": 0,
                                      "tasks": 0, "failed": 0, "cost_cny": 0.0,
                                      "llm_cost_cny": 0.0,
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
        provider_calls = int(getattr(row, "provider_calls", 0) or 0)
        agent_passed = int(getattr(row, "agent_passed", 0) or 0)
        bucket["images"] += images
        bucket["images_attempted"] += attempted
        bucket["provider_calls"] += provider_calls
        bucket["agent_passed"] += agent_passed
        bucket["tasks"] += int(row.tasks or 0)
        bucket["failed"] += int(row.failed or 0)
        bucket["cost_cny"] = round(bucket["cost_cny"] + float(row.cost_cny or 0.0), 4)
        bucket["llm_cost_cny"] = round(
            bucket["llm_cost_cny"] + float(getattr(row, "llm_cost_cny", 0.0) or 0.0), 4)
        user = bucket["by_user"].setdefault(row.username, {"images": 0, "tasks": 0,
                                                           "images_attempted": 0,
                                                           "provider_calls": 0,
                                                           "agent_passed": 0})
        user["images"] += images
        user["tasks"] += int(row.tasks or 0)
        user["images_attempted"] += attempted
        user["provider_calls"] += provider_calls
        user["agent_passed"] += agent_passed
    return [buckets[key] for key in keys]


def totals(session, *, username: str | None = None,
           department: str | None = None) -> dict:
    statement = select(ImageUsage)
    if username:
        statement = statement.where(ImageUsage.username == username)
    if department:
        statement = statement.where(ImageUsage.department == department)
    rows = session.execute(statement).scalars().all()
    return {
        "images": sum(int(row.images or 0) for row in rows),
        "images_attempted": sum(int(row.images_attempted or 0) for row in rows),
        "tasks": sum(int(row.tasks or 0) for row in rows),
        "failed": sum(int(row.failed or 0) for row in rows),
        "cost_cny": round(sum(float(row.cost_cny or 0.0) for row in rows), 4),
        "llm_cost_cny": round(sum(float(getattr(row, "llm_cost_cny", 0.0) or 0.0)
                                  for row in rows), 4),
        "tokens": sum(int(row.tokens or 0) for row in rows),
        "adopted": sum(int(row.adopted or 0) for row in rows),
        "reused_adopted": sum(int(getattr(row, "reused_adopted", 0) or 0) for row in rows),
        "rejected": sum(int(row.rejected or 0) for row in rows),
        "provider_calls": sum(int(getattr(row, "provider_calls", 0) or 0) for row in rows),
        "agent_passed": sum(int(getattr(row, "agent_passed", 0) or 0) for row in rows),
    }


__all__ = ["GRANULARITIES", "hour_key", "record", "shift", "buckets", "totals"]
