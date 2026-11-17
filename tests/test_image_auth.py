"""生图 Agent 独立账号：密码/令牌单测 + 登录/登出/限流/越权 的接口级用例。"""

from __future__ import annotations

import time

import pytest

from app.agents_image.auth import (
    AuthUser,
    LoginThrottle,
    get_throttle,
    hash_password,
    issue_token,
    parse_token,
    reset_throttle,
    verify_password,
)
from app.agents_image.store import tokens as token_store
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.agents_image.store.users import ensure_seed_users
from app.core.errors import ImageError
from app.db.engine import session_scope


# ------------------------------------------------------------------ 单测：密码与令牌
def test_password_hash_is_salted_and_verifiable():
    digest_a, salt_a = hash_password("hhw123")
    digest_b, salt_b = hash_password("hhw123")
    assert salt_a != salt_b and digest_a != digest_b          # 每用户独立盐
    assert len(digest_a) == 64 and len(salt_a) == 32
    assert verify_password("hhw123", digest_a, salt_a)
    assert not verify_password("wrong", digest_a, salt_a)      # 错密码
    assert not verify_password("hhw123", digest_a, "")         # 缺盐 → 不通过


def test_token_roundtrip_and_tamper_detection():
    user = AuthUser(username="worker1", role="user", department="男装")
    token = issue_token(user, ttl_hours=1, secret="unit-secret")
    parsed = parse_token(token, secret="unit-secret")
    assert parsed.username == "worker1" and parsed.department == "男装"
    assert parsed.is_admin is False and parsed.jti


def test_token_expired_rejected():
    token = issue_token(AuthUser("admin", "admin"), ttl_hours=1, secret="s", now=0.0)
    with pytest.raises(ImageError) as exc:
        parse_token(token, secret="s", now=3600 + 1)
    assert exc.value.code == "IMG_UNAUTHORIZED"


def test_token_wrong_secret_rejected():
    token = issue_token(AuthUser("admin", "admin"), secret="right")
    with pytest.raises(ImageError) as exc:
        parse_token(token, secret="wrong")
    assert exc.value.code == "IMG_UNAUTHORIZED"


def test_throttle_blocks_after_max_attempts():
    throttle = LoginThrottle(max_attempts=2, window_seconds=60)
    assert throttle.allowed("admin", "1.1.1.1", now=0)[0] is True
    throttle.record_failure("admin", "1.1.1.1", now=1)
    throttle.record_failure("admin", "1.1.1.1", now=2)
    allowed, retry_after = throttle.allowed("admin", "1.1.1.1", now=3)
    assert allowed is False and retry_after > 0
    assert throttle.allowed("admin", "2.2.2.2", now=3)[0] is True   # 换 IP 不受影响
    throttle.reset("admin", "1.1.1.1")
    assert throttle.allowed("admin", "1.1.1.1", now=4)[0] is True


# ------------------------------------------------------------------ 账号表
def test_seed_users_are_idempotent_and_password_preserved(_db):
    ensure_image_schema()
    with session_scope() as session:
        ensure_seed_users(session)
    with session_scope() as session:
        assert user_store.set_password(session, "worker1", "newpass123")
    with session_scope() as session:
        ensure_seed_users(session)                              # 再跑一次不该重置密码
        assert user_store.authenticate(session, "worker1", "newpass123") is not None
        assert user_store.authenticate(session, "worker1", "hhw111") is None
        assert user_store.authenticate(session, "nobody", "x") is None
        assert user_store.set_password(session, "worker1", "hhw111")   # 还原，避免污染其它用例


def test_disabled_user_cannot_authenticate(_db):
    ensure_image_schema()
    with session_scope() as session:
        ensure_seed_users(session)
        assert user_store.set_enabled(session, "worker2", False)
        assert user_store.authenticate(session, "worker2", "hhw222") is None
    with session_scope() as session:
        user_store.set_enabled(session, "worker2", True)


def test_token_blacklist_store(_db):
    ensure_image_schema()
    expired = time.time() - 10
    with session_scope() as session:
        token_store.revoke(session, "jti-unit-1", time.time() + 600)
        assert token_store.is_revoked(session, "jti-unit-1")
        assert token_store.is_revoked(session, "jti-none") is False
        token_store.revoke(session, "jti-unit-2", expired)
        assert token_store.sweep(session) == 1                  # 只清过期的
        assert token_store.is_revoked(session, "jti-unit-1")


# ------------------------------------------------------------------ 接口级
@pytest.fixture()
def auth_env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    from app.agents_image.config import reset_image_settings
    from app.agents_image.sources import product_source as ps

    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    ensure_image_schema()
    with session_scope() as session:
        ensure_seed_users(session)
    yield app_client
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()


def _login(client, username: str, password: str):
    return client.post("/api/image/auth/login",
                       json={"username": username, "password": password})


def test_login_ok_and_me(auth_env):
    resp = _login(auth_env, "worker1", "hhw111")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] > 0
    assert body["user"] == {"username": "worker1", "role": "user",
                            "department": "男装", "is_admin": False}
    headers = {"Authorization": f"Bearer {body['token']}"}
    me = auth_env.get("/api/image/auth/me", headers=headers)
    assert me.status_code == 200 and me.json()["username"] == "worker1"


def test_login_wrong_password_and_missing_fields(auth_env):
    assert _login(auth_env, "worker1", "bad").json()["error_code"] == "IMG_UNAUTHORIZED"
    assert _login(auth_env, "", "").json()["error_code"] == "IMG_INPUT_INVALID"
    assert _login(auth_env, "admin", "hhw123").status_code == 200


def test_login_throttled_returns_retry_after(auth_env, monkeypatch):
    monkeypatch.setenv("IMAGE_LOGIN_MAX_ATTEMPTS", "2")
    from app.agents_image.config import reset_image_settings

    reset_image_settings()
    get_throttle()
    for _ in range(2):
        assert _login(auth_env, "worker1", "bad").status_code == 401
    blocked = _login(auth_env, "worker1", "bad")
    assert blocked.status_code == 429
    assert blocked.json()["error_code"] == "IMG_LOGIN_THROTTLED"
    assert int(blocked.headers["Retry-After"]) > 0


def test_logout_invalidates_token(auth_env):
    token = _login(auth_env, "admin", "hhw123").json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert auth_env.get("/api/image/stats", headers=headers).status_code == 200
    assert auth_env.post("/api/image/auth/logout", headers=headers).status_code == 200
    after = auth_env.get("/api/image/stats", headers=headers)
    assert after.status_code == 401
    assert after.json()["error_code"] == "IMG_UNAUTHORIZED"


def test_bad_authorization_header_shapes(auth_env):
    for value in ("Token abc", "Bearer", "Bearer  "):
        resp = auth_env.get("/api/image/stats", headers={"Authorization": value})
        assert resp.status_code == 401, value


def test_user_cannot_read_others_task(auth_env):
    worker1 = {"Authorization": f"Bearer {_login(auth_env, 'worker1', 'hhw111').json()['token']}"}
    worker2 = {"Authorization": f"Bearer {_login(auth_env, 'worker2', 'hhw222').json()['token']}"}
    admin = {"Authorization": f"Bearer {_login(auth_env, 'admin', 'hhw123').json()['token']}"}

    created = auth_env.post("/api/image/tasks", headers=worker1, json={
        "product_ids": ["P-1002"], "prompt": "白底主图", "n": 1,
    })
    assert created.status_code == 201, created.text
    task_id = created.json()["tasks"][0]["task_id"]

    assert auth_env.get(f"/api/image/tasks/{task_id}", headers=worker1).status_code == 200
    other = auth_env.get(f"/api/image/tasks/{task_id}", headers=worker2)
    assert other.status_code == 403 and other.json()["error_code"] == "IMG_FORBIDDEN"
    assert auth_env.get(f"/api/image/tasks/{task_id}", headers=admin).status_code == 200

    # 参考图（中台商品图落地）同样按账号隔离
    detail = auth_env.get(f"/api/image/tasks/{task_id}", headers=worker1).json()
    ref_url = detail["refs"][0]["url"]
    assert auth_env.get(ref_url, headers=worker1).status_code == 200
    assert auth_env.get(ref_url, headers=worker2).status_code == 403

    # 任务列表按账号隔离
    mine = auth_env.get("/api/image/tasks", headers=worker2).json()["tasks"]
    assert all(item["username"] == "worker2" for item in mine)
    everyone = auth_env.get("/api/image/tasks?all=true", headers=admin).json()["tasks"]
    assert any(item["task_id"] == task_id for item in everyone)
