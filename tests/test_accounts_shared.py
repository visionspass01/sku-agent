"""M1：共享人员账号表（SKU 与生图 Agent 同一套账号）。"""

from __future__ import annotations

import pytest

from app.core import accounts
from app.core.errors import ImageError


@pytest.fixture()
def db(_db):
    from app.db.engine import session_scope

    with session_scope() as session:
        accounts.ensure_seed(session)
    return _db


def test_seed_is_idempotent_and_preserves_changed_password(db):
    from app.db.engine import session_scope

    with session_scope() as session:
        assert accounts.set_password(session, "worker1", "worker1-newpass")
    with session_scope() as session:
        accounts.ensure_seed(session)                      # 再播种不应覆盖已改密码
        assert accounts.authenticate(session, "worker1", "worker1-newpass") is not None
        assert accounts.authenticate(session, "worker1", "hhw111") is None
        accounts.set_password(session, "worker1", "hhw111")   # 还原，避免污染其它用例


def test_authenticate_and_disable_take_effect(db):
    from app.db.engine import session_scope

    with session_scope() as session:
        account = accounts.authenticate(session, "admin", "hhw123")
        assert account is not None and account.is_admin is True
        assert accounts.authenticate(session, "admin", "wrong") is None

        accounts.set_enabled(session, "worker2", False)
        assert accounts.authenticate(session, "worker2", "hhw222") is None
        accounts.set_enabled(session, "worker2", True)


def test_same_account_works_for_both_agents(db, app_client):
    """同一账号密码：SKU 的 /api/auth/login 与生图的 /api/image/auth/login 都能过。"""
    sku = app_client.post("/api/auth/login", json={"username": "worker1", "password": "hhw111"})
    assert sku.status_code == 200, sku.text
    image = app_client.post("/api/image/auth/login",
                            json={"username": "worker1", "password": "hhw111"})
    assert image.status_code == 200, image.text
    assert image.json()["user"]["username"] == "worker1"


def test_password_change_applies_to_both_agents(db, app_client):
    from app.db.engine import session_scope

    with session_scope() as session:
        accounts.set_password(session, "worker1", "both-newpass")
    try:
        assert app_client.post("/api/auth/login", json={
            "username": "worker1", "password": "both-newpass"}).status_code == 200
        assert app_client.post("/api/image/auth/login", json={
            "username": "worker1", "password": "both-newpass"}).status_code == 200
        assert app_client.post("/api/image/auth/login", json={
            "username": "worker1", "password": "hhw111"}).status_code == 401
    finally:
        with session_scope() as session:
            accounts.set_password(session, "worker1", "hhw111")


def test_disabled_account_is_rejected_by_both_agents(db, app_client):
    from app.db.engine import session_scope

    with session_scope() as session:
        accounts.set_enabled(session, "worker2", False)
    try:
        assert app_client.post("/api/auth/login", json={
            "username": "worker2", "password": "hhw222"}).status_code == 401
        assert app_client.post("/api/image/auth/login", json={
            "username": "worker2", "password": "hhw222"}).status_code == 401
    finally:
        with session_scope() as session:
            accounts.set_enabled(session, "worker2", True)


def test_legacy_constants_still_authenticate(db, monkeypatch):
    """ACCOUNTS_SOURCE=legacy 时可以回滚到常量账号（故障回退位）。"""
    from app.db.engine import session_scope

    monkeypatch.setenv("ACCOUNTS_SOURCE", "legacy")
    with session_scope() as session:
        account = accounts.authenticate(session, "admin", "hhw123")
        assert account is not None and account.role == "admin"
        assert accounts.authenticate(session, "admin", "nope") is None


def test_create_user_validation_and_listing(db):
    from app.db.engine import session_scope

    with session_scope() as session:
        accounts.create_user(session, username="worker9", password="hhw999",
                             department="男装", display_name="测试")
        with pytest.raises(ImageError):
            accounts.create_user(session, username="worker9", password="x" * 8)
        rows = {row["username"]: row for row in accounts.list_users(session)}
    assert rows["worker9"]["department"] == "男装"
    assert rows["worker9"]["enabled"] is True


def test_image_user_legacy_table_is_migrated(db):
    """旧 image_user 行会被搬进共享表（幂等，不重复）。"""
    from sqlalchemy import text

    from app.db.engine import session_scope

    with session_scope() as session:
        engine = session.get_bind()
        session.execute(text(
            "CREATE TABLE IF NOT EXISTS image_user ("
            "username VARCHAR(32) PRIMARY KEY, display_name VARCHAR(64), department VARCHAR(32), "
            "role VARCHAR(16), password_salt VARCHAR(32), password_hash VARCHAR(64), "
            "enabled BOOLEAN, created_at DATETIME, last_login_at DATETIME)"))
        digest, salt = accounts.hash_password("legacy-pass")
        session.execute(text(
            "INSERT INTO image_user VALUES "
            "('worker-legacy','老账号','女装','user',:salt,:digest,1,NULL,NULL)"),
            {"salt": salt, "digest": digest})
        session.flush()
    with session_scope() as session:
        moved = accounts.migrate_image_user(session)
        assert moved == 1
        assert accounts.authenticate(session, "worker-legacy", "legacy-pass") is not None
    with session_scope() as session:
        assert accounts.migrate_image_user(session) == 0      # 幂等
    assert engine is not None
