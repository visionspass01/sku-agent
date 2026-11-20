"""第五轮 W2：部门权限收口（P0-5）。

过去 `_ensure_department` 只在「有部门且不匹配」时拦截，于是**建号时没填部门**的普通账号
反而能看/出所有部门的商品——权限模型被"少填一个字段"绕过。现在：
- 普通账号没有部门 → `/products` 与建任务一律 403；
- 管理员建普通账号必须指定部门（白名单 男装/女装）；
- 管理员可改部门（把历史"无部门"账号修回来）；
- `/health` 暴露 `users_without_department`，运维能提前发现。
"""

from __future__ import annotations

import uuid

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    monkeypatch.delenv("IMAGE_PRODUCT_SOURCE", raising=False)
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()
    image_metrics.reset()
    ensure_image_schema()
    with session_scope() as session:
        user_store.ensure_seed_users(session)
    yield app_client
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()


def login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _make_user(department: str | None) -> tuple[str, str]:
    """建一个用户并直接 login 用的 (username, password)；department=None 模拟历史脏账号。"""
    username = f"u-dept-{uuid.uuid4().hex[:8]}"
    password = "pw-dept-2026"
    with session_scope() as session:
        user_store.create_user(session, username=username, password=password,
                               role="user", department=department)
    return username, password


# ---------------------------------------------------------------- 无部门账号必须 403
def test_user_without_department_cannot_list_products(env):
    username, password = _make_user(None)
    headers = login(env, username, password)
    resp = env.get("/api/image/products", headers=headers)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == "IMG_FORBIDDEN"
    assert "未绑定部门" in resp.json()["detail"]


def test_user_without_department_cannot_create_task(env):
    username, password = _make_user(None)
    headers = login(env, username, password)
    resp = env.post("/api/image/tasks", headers=headers,
                    json={"product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1})
    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == "IMG_FORBIDDEN"


def test_user_with_department_sees_only_its_department(env):
    username, password = _make_user("男装")
    headers = login(env, username, password)
    body = env.get("/api/image/products?limit=50", headers=headers).json()
    ids = {item["product_id"] for item in body["products"]}
    assert ids, "男装账号至少能看到一个男装商品"
    assert ids <= {"P-1002", "P-3002"}, ids


# ---------------------------------------------------------------- 建号 / 改部门
def test_admin_create_user_requires_department_for_normal_role(env):
    headers = login(env)
    username = f"u-new-{uuid.uuid4().hex[:8]}"
    missing = env.post("/api/image/admin/users", headers=headers,
                       json={"username": username, "password": "pw-new-2026"})
    assert missing.status_code == 422, missing.text
    assert missing.json()["error_code"] == "IMG_INPUT_INVALID"

    bad = env.post("/api/image/admin/users", headers=headers,
                   json={"username": username, "password": "pw-new-2026",
                         "department": "童装部"})
    assert bad.status_code == 422

    ok = env.post("/api/image/admin/users", headers=headers,
                  json={"username": username, "password": "pw-new-2026",
                        "department": "女装"})
    assert ok.status_code == 201, ok.text
    assert ok.json()["department"] == "女装"


def test_admin_can_bind_department_to_legacy_account(env):
    """历史"无部门"账号：改部门后立即可以正常出图。"""
    headers = login(env)
    username, _password = _make_user(None)
    resp = env.put(f"/api/image/admin/users/{username}", headers=headers,
                   json={"department": "女装"})
    assert resp.status_code == 200, resp.text
    with session_scope() as session:
        row = user_store.get_user(session, username)
        assert row.department == "女装"


def test_admin_cannot_clear_department_of_normal_user(env):
    headers = login(env)
    username, _password = _make_user("男装")
    resp = env.put(f"/api/image/admin/users/{username}", headers=headers,
                   json={"department": ""})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "IMG_INPUT_INVALID"


# ---------------------------------------------------------------- 健康位
def test_health_exposes_users_without_department(env):
    """只暴露计数，不暴露账号名（/health 匿名可读，名单会变成账号枚举面）。"""
    _username, _password = _make_user(None)
    body = env.get("/api/image/health").json()
    assert body["users_without_department"] >= 1
    assert "users_without_department_list" not in body, "健康检查不得泄露账号名单"


def test_health_department_count_is_cached(env, monkeypatch):
    """P2-3（第九轮）：/health 被高频探测，计数结果缓存 60s（不能每次都查账号表）。"""
    from app.agents_image.store import users as user_store

    calls = {"n": 0}
    real = user_store.list_users

    def spy(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(user_store, "list_users", spy)
    env.get("/api/image/health")
    env.get("/api/image/health")
    assert calls["n"] == 1, f"60s 内应只查一次账号表（实际 {calls['n']} 次）"


def test_admin_users_endpoint_lists_dirty_accounts(env):
    """具体是哪些账号走管理员接口（需要 admin 鉴权）。"""
    username, _password = _make_user(None)
    headers = login(env)
    rows = env.get("/api/image/admin/users", headers=headers).json()["users"]
    mine = [row for row in rows if row["username"] == username]
    assert mine and mine[0]["department"] is None
