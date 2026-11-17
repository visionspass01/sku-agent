"""令牌黑名单：登出 / 踢下线后立即失效（内存优先，DB 兜底，重启不丢）。"""

from __future__ import annotations

import datetime as dt
import time

from sqlalchemy import select


def revoke(session, jti: str, exp: float, *, reason: str = "logout") -> None:
    from app.agents_image.store.models import ImageTokenBlacklist

    if not jti:
        return
    if session.execute(
        select(ImageTokenBlacklist).where(ImageTokenBlacklist.jti == jti)
    ).scalars().first() is not None:
        return
    session.add(ImageTokenBlacklist(
        jti=jti, exp=dt.datetime.fromtimestamp(exp or time.time() + 3600), reason=reason,
    ))
    session.flush()


def is_revoked(session, jti: str) -> bool:
    from app.agents_image.store.models import ImageTokenBlacklist

    if not jti:
        return False
    return session.execute(
        select(ImageTokenBlacklist).where(ImageTokenBlacklist.jti == jti)
    ).scalars().first() is not None


def sweep(session, now: dt.datetime | None = None) -> int:
    from app.agents_image.store.models import ImageTokenBlacklist

    moment = now or dt.datetime.now()
    rows = session.execute(
        select(ImageTokenBlacklist).where(ImageTokenBlacklist.exp < moment)
    ).scalars().all()
    for row in rows:
        session.delete(row)
    session.flush()
    return len(rows)
