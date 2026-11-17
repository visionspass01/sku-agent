"""独立账号表：`image_user`（与 SKU 的 user 表无关）+ 预置 3 个演示账号。"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.agents_image.auth import hash_password, verify_password
from app.core.errors import raise_image

# 预置账号：账号密码参照 SKU（admin / hhw123、worker1 / hhw111、worker2 / hhw222）
SEED_USERS = (
    {"username": "admin", "password": "hhw123", "role": "admin", "department": None},
    {"username": "worker1", "password": "hhw111", "role": "user", "department": "男装"},
    {"username": "worker2", "password": "hhw222", "role": "user", "department": "女装"},
)


def ensure_seed_users(session) -> list[str]:
    """幂等：缺失的账号补上；已存在的不覆盖密码（避免把用户改过的密码重置）。"""
    from app.agents_image.store.models import ImageUser

    created: list[str] = []
    for spec in SEED_USERS:
        exists = get_user(session, spec["username"])
        if exists is not None:
            continue
        digest, salt = hash_password(spec["password"])
        session.add(ImageUser(
            username=spec["username"], password_hash=digest, password_salt=salt,
            display_name=spec["username"], role=spec["role"],
            department=spec["department"], enabled=True,
        ))
        created.append(spec["username"])
    session.flush()
    return created


def get_user(session, username: str):
    from app.agents_image.store.models import ImageUser

    return session.execute(
        select(ImageUser).where(ImageUser.username == username)
    ).scalars().first()


def authenticate(session, username: str, password: str):
    user = get_user(session, username)
    if user is None or not user.enabled:
        return None
    if not verify_password(password, user.password_hash, user.password_salt):
        return None
    user.last_login_at = dt.datetime.now()
    session.flush()
    return user


def create_user(session, *, username: str, password: str, role: str = "user",
                department: str | None = None) -> object:
    from app.agents_image.store.models import ImageUser

    if get_user(session, username) is not None:
        raise_image("IMG_INPUT_INVALID", f"账号已存在：{username}")
    digest, salt = hash_password(password)
    user = ImageUser(username=username, password_hash=digest, password_salt=salt,
                     display_name=username, role=role, department=department, enabled=True)
    session.add(user)
    session.flush()
    return user


def set_password(session, username: str, password: str) -> bool:
    user = get_user(session, username)
    if user is None:
        return False
    user.password_hash, user.password_salt = hash_password(password)
    session.flush()
    return True


def set_enabled(session, username: str, enabled: bool) -> bool:
    user = get_user(session, username)
    if user is None:
        return False
    user.enabled = bool(enabled)
    session.flush()
    return True


def list_users(session) -> list[dict]:
    from app.agents_image.store.models import ImageUser

    rows = session.execute(select(ImageUser).order_by(ImageUser.username)).scalars().all()
    return [{"username": row.username, "display_name": row.display_name, "role": row.role,
             "department": row.department, "enabled": bool(row.enabled),
             "last_login_at": row.last_login_at.isoformat() if row.last_login_at else None}
            for row in rows]


__all__ = ["SEED_USERS", "ensure_seed_users", "get_user", "authenticate", "create_user",
           "set_password", "set_enabled", "list_users"]
