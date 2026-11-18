"""配额：按人按自然周期（Asia/Shanghai）计数，**交付张数**才占用额度。

设计要点：
- 条件更新 `UPDATE ... WHERE used + n <= period_limit` 防并发超发（SQLite/MySQL 语义一致）；
- 管理员不受配额限制（返回 `unlimited`）；
- 周期跨了就重置 `period_key` + `used`（自然周期，不依赖定时任务）；
- 尝试张数只进使用量（`image_usage`），不占配额。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select, update

from app.agents_image.store.models import ImageQuota

_TZ_OFFSET_HOURS = 8


def now_local(now: dt.datetime | None = None) -> dt.datetime:
    """Asia/Shanghai 的当前时间（用固定 +8 偏移，避免依赖 tz 数据库）。"""
    base = now or dt.datetime.now()
    return base + dt.timedelta(hours=_TZ_OFFSET_HOURS)


def period_key(period_type: str, now: dt.datetime | None = None) -> str:
    """day → 2026-11-18；week → 2026-W47（ISO 周）；month → 2026-11。"""
    moment = now_local(now)
    kind = (period_type or "day").strip().lower()
    if kind == "month":
        return moment.strftime("%Y-%m")
    if kind == "week":
        year, week, _ = moment.isocalendar()
        return f"{year}-W{week:02d}"
    if kind == "unlimited":
        return "unlimited"
    return moment.strftime("%Y-%m-%d")


def get_or_create(session, username: str, *, period_type: str = "day",
                  period_limit: int | None = None, granted_by: str = "") -> ImageQuota:
    row = session.get(ImageQuota, username)
    if row is not None:
        return row
    from app.agents_image.config import get_image_settings

    settings = get_image_settings()
    limit = int(period_limit if period_limit is not None else settings.default_quota_images)
    row = ImageQuota(
        username=username, period_type=period_type or settings.default_quota_period,
        period_limit=limit, period_key=period_key(period_type or settings.default_quota_period),
        used=0, granted_by=granted_by, updated_at=dt.datetime.now(),
    )
    session.add(row)
    session.flush()
    return row


def _rollover(session, row: ImageQuota) -> ImageQuota:
    """周期已切换 → 归零（自然周期，跨天/周/月自动发生）。"""
    current = period_key(row.period_type)
    if row.period_type == "unlimited":
        return row
    if row.period_key != current:
        row.period_key = current
        row.used = 0
        row.updated_at = dt.datetime.now()
        session.flush()
    return row


def remaining(row: ImageQuota) -> int:
    if row.period_type == "unlimited":
        return 10 ** 9
    return max(0, int(row.period_limit or 0) - int(row.used or 0) - int(row.reserved or 0))


def check(session, username: str, need: int = 1, *, is_admin: bool = False) -> dict:
    """能否再出 `need` 张：返回 {allowed, remaining, limit, period_type, period_key}。"""
    if is_admin:
        return {"allowed": True, "remaining": None, "limit": None,
                "period_type": "unlimited", "period_key": "unlimited"}
    row = _rollover(session, get_or_create(session, username))
    left = remaining(row)
    return {"allowed": left >= max(0, int(need or 0)), "remaining": left,
            "limit": int(row.period_limit or 0), "period_type": row.period_type,
            "period_key": row.period_key}


def reserve(session, username: str, images: int) -> bool:
    """**预留**额度（创建任务时调用）：`used + reserved + n <= limit` 才成功。

    与"事后扣"不同：预留后就无法被其它并发请求抢走，交付成功再转成 used，
    失败/取消则 `release` 回滚——保证"计划多少张就占多少张"。
    """
    count = max(0, int(images or 0))
    if count == 0:
        return True
    row = _rollover(session, get_or_create(session, username))
    if row.period_type == "unlimited":
        return True
    result = session.execute(
        update(ImageQuota)
        .where(ImageQuota.username == username,
               ImageQuota.period_key == row.period_key,
               ImageQuota.used + ImageQuota.reserved + count <= ImageQuota.period_limit)
        .values(reserved=ImageQuota.reserved + count, updated_at=dt.datetime.now())
    )
    return bool(result.rowcount)


def commit_reserve(session, username: str, images: int) -> bool:
    """交付结算：把 `images` 张预留转成 used，其余预留释放（少出的张数不占额度）。"""
    row = _rollover(session, get_or_create(session, username))
    if row.period_type == "unlimited":
        return True
    count = max(0, int(images or 0))
    result = session.execute(
        update(ImageQuota)
        .where(ImageQuota.username == username,
               ImageQuota.period_key == row.period_key,
               ImageQuota.reserved >= count)
        .values(used=ImageQuota.used + count,
                reserved=ImageQuota.reserved - count,
                updated_at=dt.datetime.now())
    )
    return bool(result.rowcount)


def release(session, username: str, images: int) -> bool:
    """释放预留（失败/取消）：预留不足时清零，不会把 used 变成负数。"""
    row = _rollover(session, get_or_create(session, username))
    if row.period_type == "unlimited":
        return True
    count = max(0, int(images or 0))
    if count == 0:
        return True
    result = session.execute(
        update(ImageQuota)
        .where(ImageQuota.username == username,
               ImageQuota.period_key == row.period_key)
        .values(reserved=func.max(ImageQuota.reserved - count, 0),
                updated_at=dt.datetime.now())
    )
    return bool(result.rowcount)


def consume(session, username: str, images: int) -> bool:
    """直接占用额度（不带预留，历史接口保留）：条件更新防超发。"""
    count = max(0, int(images or 0))
    if count == 0:
        return True
    row = _rollover(session, get_or_create(session, username))
    if row.period_type == "unlimited":
        return True
    result = session.execute(
        update(ImageQuota)
        .where(ImageQuota.username == username,
               ImageQuota.period_key == row.period_key,
               ImageQuota.used + ImageQuota.reserved + count <= ImageQuota.period_limit)
        .values(used=ImageQuota.used + count, updated_at=dt.datetime.now())
    )
    return bool(result.rowcount)


def grant(session, username: str, *, period_type: str, period_limit: int,
          granted_by: str = "") -> ImageQuota:
    """管理员给某人设/改配额（周期切换时 used 归零）。"""
    row = get_or_create(session, username, granted_by=granted_by)
    if period_type and period_type != row.period_type:
        row.period_type = period_type
        row.used = 0
        row.period_key = period_key(period_type)
    row.period_limit = int(period_limit)
    row.granted_by = granted_by or row.granted_by
    row.updated_at = dt.datetime.now()
    session.flush()
    return row


def list_quotas(session) -> list[dict]:
    rows = session.execute(select(ImageQuota).order_by(ImageQuota.username)).scalars().all()
    return [{"username": row.username, "period_type": row.period_type,
             "period_limit": int(row.period_limit or 0), "period_key": row.period_key,
             "used": int(row.used or 0), "remaining": remaining(row),
             "granted_by": row.granted_by} for row in rows]


__all__ = ["now_local", "period_key", "get_or_create", "remaining", "check",
           "reserve", "commit_reserve", "release", "consume", "grant", "list_quotas"]
