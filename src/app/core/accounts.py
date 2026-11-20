"""共享账号层：**一张 `user` 表，SKU 与生图 Agent 共用同一套人账号**（M1）。

背景（为什么要统一）：
- 数据中台的鉴权是**机器令牌**（`X-Internal-Token` / `X-Api-Key` → Principal 带 scope + 部门），
  **没有人员账号**；人员账号过去两套都在 Agent 侧：SKU 是 `app/auth.py` 里的硬编码常量，
  生图 Agent 是 DB 表 `image_user`。同一家公司同一批运营，却要记两套密码、停用要改两处。
- 现在统一到 `user` 表：SKU 的 3 个演示账号作为**种子数据**入库，两个 Agent 都从这张表鉴权；
  `image_user` 表只保留一次性迁移读取，之后不再写入。

本模块只依赖 `app.db.*`（通用层），不 import 任何业务包，因此 image-only 模式也能用。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

LEGACY_SALT = "sku-demo-salt::"     # SKU 旧常量账号的哈希盐（只用于兼容校验）

# 种子账号：与 SKU/生图两侧历史口径保持一致（演示用，生产接 SSO）
DEFAULT_USERS: tuple[dict, ...] = (
    {"username": "admin", "password": "hhw123", "role": "admin", "department": None,
     "display_name": "管理员"},
    {"username": "worker1", "password": "hhw111", "role": "user", "department": "男装",
     "display_name": "男装运营"},
    {"username": "worker2", "password": "hhw222", "role": "user", "department": "女装",
     "display_name": "女装运营"},
)


@dataclass(frozen=True)
class Account:
    """跨模块统一的账号视图（SKU 的 AuthUser / image 的 current_user 都由它派生）。"""

    username: str
    role: str = "user"
    department: str | None = None
    display_name: str = ""
    enabled: bool = True

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def to_dict(self) -> dict:
        return {"username": self.username, "role": self.role,
                "department": self.department, "display_name": self.display_name}


# ---------------------------------------------------------------- 口令
def new_salt() -> str:
    return secrets.token_hex(16)


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """`sha256(随机盐 + 明文)`，与 SKU 历史算法同构（只是盐从常量换成了每用户随机盐）。"""
    salt = salt or new_salt()
    digest = hashlib.sha256((salt + (password or "")).encode("utf-8")).hexdigest()
    return digest, salt


def verify_password(password: str, digest: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt or "")
    return hmac.compare_digest(candidate, digest or "")


def legacy_hash(password: str) -> str:
    """SKU 旧常量账号的哈希（`sha256(LEGACY_SALT + 明文)`），仅用于兼容老 token/老库。"""
    return hashlib.sha256((LEGACY_SALT + (password or "")).encode("utf-8")).hexdigest()


def source_mode() -> str:
    """账号来源：`db`（默认，统一表）｜`legacy`（回退到 SKU 常量，用于故障回滚）。"""
    return (os.environ.get("ACCOUNTS_SOURCE") or "db").strip().lower()


# ---------------------------------------------------------------- 读写
def ensure_seed(session=None) -> list[str]:
    """幂等播种：表里缺哪个账号补哪个（**不覆盖**已存在的密码，避免把用户改过的密码重置）。"""
    from app.core import accounts_schema  # 复用建表逻辑，避免与 db.models 循环导入

    accounts_schema.ensure_user_table()
    created: list[str] = []
    own = session is None
    if own:
        from app.db.engine import session_scope

        ctx = session_scope()
        session = ctx.__enter__()
    try:
        from app.db.models import AppUser

        for spec in DEFAULT_USERS:
            if session.get(AppUser, spec["username"]) is not None:
                continue
            digest, salt = hash_password(spec["password"])
            session.add(AppUser(
                username=spec["username"], display_name=spec["display_name"],
                role=spec["role"], department=spec["department"],
                password_hash=digest, password_salt=salt, enabled=True,
                created_at=dt.datetime.now(),
            ))
            created.append(spec["username"])
        session.flush()
    finally:
        if own:
            ctx.__exit__(None, None, None)
    return created


def get_user(session, username: str):
    from app.db.models import AppUser

    return session.get(AppUser, username)


def authenticate(session, username: str, password: str) -> Account | None:
    """统一鉴权：先查 `user` 表；`ACCOUNTS_SOURCE=legacy` 时退回 SKU 常量口径。"""
    username = (username or "").strip()
    if not username or not password:
        return None
    if source_mode() == "legacy":
        return _legacy_authenticate(username, password)
    row = get_user(session, username)
    if row is not None:
        if not row.enabled:
            return None
        if verify_password(password, row.password_hash, row.password_salt):
            row.last_login_at = dt.datetime.now()
            session.flush()
            return Account(username=row.username, role=row.role or "user",
                           department=row.department, display_name=row.display_name or "",
                           enabled=bool(row.enabled))
        return None
    # 表里没有但账号在常量里（例如老库刚升级）→ 兼容登录并顺手入库
    account = _legacy_authenticate(username, password)
    if account is None:
        return None
    create_user(session, username=username, password=password, role=account.role,
                department=account.department, display_name=account.display_name)
    return account


def _legacy_authenticate(username: str, password: str) -> Account | None:
    for spec in DEFAULT_USERS:
        if spec["username"] != username:
            continue
        if hmac.compare_digest(legacy_hash(password), legacy_hash(spec["password"])):
            return Account(username=username, role=spec["role"],
                           department=spec["department"], display_name=spec["display_name"])
    return None


def create_user(session, *, username: str, password: str, role: str = "user",
                department: str | None = None, display_name: str = ""):
    from app.db.models import AppUser

    if get_user(session, username) is not None:
        from app.core.errors import raise_image

        raise_image("IMG_INPUT_INVALID", f"账号已存在：{username}")
    digest, salt = hash_password(password)
    row = AppUser(username=username, display_name=display_name or username, role=role,
                  department=department, password_hash=digest, password_salt=salt,
                  enabled=True, created_at=dt.datetime.now())
    session.add(row)
    session.flush()
    return row


def set_password(session, username: str, password: str) -> bool:
    row = get_user(session, username)
    if row is None:
        return False
    row.password_hash, row.password_salt = hash_password(password)
    session.flush()
    return True


def set_enabled(session, username: str, enabled: bool) -> bool:
    row = get_user(session, username)
    if row is None:
        return False
    row.enabled = bool(enabled)
    session.flush()
    return True


def set_department(session, username: str, department: str | None) -> bool:
    """改部门（第五轮 P0-5）：用于把"建号时没填部门"的历史账号修回可见范围。"""
    row = get_user(session, username)
    if row is None:
        return False
    row.department = (department or None)
    session.flush()
    return True


def list_users(session) -> list[dict]:
    from sqlalchemy import select

    from app.db.models import AppUser

    rows = session.execute(select(AppUser).order_by(AppUser.username)).scalars().all()
    return [{"username": row.username, "display_name": row.display_name, "role": row.role,
             "department": row.department, "enabled": bool(row.enabled),
             "last_login_at": row.last_login_at.isoformat() if row.last_login_at else None}
            for row in rows]


def migrate_image_user(session) -> int:
    """把旧 `image_user` 表的账号搬进 `user`（幂等；表不存在时静默返回 0）。"""
    from sqlalchemy import text

    from app.db.models import AppUser

    engine = session.get_bind()
    try:
        exists = session.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='image_user'"
        )).first() if engine.dialect.name == "sqlite" else session.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_name='image_user'"
        )).first()
    except Exception:  # noqa: BLE001 —— 方言差异，迁移失败不影响主流程
        return 0
    if not exists:
        return 0
    rows = session.execute(text(
        "SELECT username, display_name, department, role, password_salt, password_hash, "
        "enabled, created_at, last_login_at FROM image_user"
    )).all()
    moved = 0
    for row in rows:
        username = row[0]
        if session.get(AppUser, username) is not None:
            continue
        session.add(AppUser(
            username=username, display_name=row[1] or username, role=row[3] or "user",
            department=row[2], password_salt=row[4] or "", password_hash=row[5] or "",
            enabled=bool(row[6]), created_at=row[7] or dt.datetime.now(),
            last_login_at=row[8],
        ))
        moved += 1
    if moved:
        session.flush()
    return moved


__all__ = ["Account", "DEFAULT_USERS", "LEGACY_SALT", "hash_password", "verify_password",
           "legacy_hash", "source_mode", "ensure_seed", "get_user", "authenticate",
           "create_user", "set_password", "set_enabled", "set_department", "list_users",
           "migrate_image_user"]
