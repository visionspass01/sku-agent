"""生图 Agent 的账号读写：**委托共享 `user` 表**（`app.core.accounts`，与 SKU 同一套账号）。

历史沿革：v7 之前这里是独立的 `image_user` 表；现在只保留一次性迁移读取
（`accounts.migrate_image_user`），之后所有读写都落在共享表上。
"""

from __future__ import annotations

import datetime as dt

from app.core import accounts

# 预置账号（与共享层种子一致；保留常量是为了兼容测试与演示脚本文案）
SEED_USERS = tuple(
    {"username": spec["username"], "password": spec["password"],
     "role": spec["role"], "department": spec["department"]}
    for spec in accounts.DEFAULT_USERS
)


def ensure_seed_users(session) -> list[str]:
    """幂等播种共享账号（缺哪个补哪个，不覆盖已有密码）。"""
    return accounts.ensure_seed(session)


def migrate_legacy_users(session) -> int:
    """把旧 `image_user` 表的账号搬进共享表（幂等）。"""
    return accounts.migrate_image_user(session)


def get_user(session, username: str):
    return accounts.get_user(session, username)


def authenticate(session, username: str, password: str):
    return accounts.authenticate(session, username, password)


def create_user(session, *, username: str, password: str, role: str = "user",
                department: str | None = None, display_name: str = ""):
    return accounts.create_user(session, username=username, password=password, role=role,
                                department=department, display_name=display_name)


def set_password(session, username: str, password: str) -> bool:
    return accounts.set_password(session, username, password)


def set_enabled(session, username: str, enabled: bool) -> bool:
    return accounts.set_enabled(session, username, enabled)


def set_department(session, username: str, department: str | None) -> bool:
    return accounts.set_department(session, username, department)


def list_users(session) -> list[dict]:
    return accounts.list_users(session)


def touch_login(session, username: str) -> None:
    row = accounts.get_user(session, username)
    if row is not None:
        row.last_login_at = dt.datetime.now()
        session.flush()


__all__ = ["SEED_USERS", "ensure_seed_users", "migrate_legacy_users", "get_user",
           "authenticate", "create_user", "set_password", "set_enabled", "set_department",
           "list_users", "touch_login"]
