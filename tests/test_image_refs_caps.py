"""第四轮 W6：`IMAGE_MAX_REFS` 生效上限（P1-7，裁决 ③）。

配置上限留弹性（可配到 12），但真正生效的上限 = `min(IMAGE_MAX_REFS, provider.caps.max_refs)`；
超额在**创建期**就报 `IMG_REFS_BUDGET`，而不是执行期才炸 `IMG_TOO_MANY_REFS`。
"""

from __future__ import annotations

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import get_image_settings, reset_image_settings
from app.agents_image.providers.image_provider import ProviderCaps, effective_max_refs
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


# ---------------------------------------------------------------- 生效上限计算
def test_effective_max_refs_takes_min_of_config_and_provider(monkeypatch):
    monkeypatch.setenv("IMAGE_MAX_REFS", "12")
    reset_image_settings()

    class _Capped:
        caps = ProviderCaps(max_refs=6)

    assert get_image_settings().max_refs == 12        # 配置保留弹性
    assert effective_max_refs(get_image_settings(), _Capped()) == 6

    monkeypatch.setenv("IMAGE_MAX_REFS", "3")
    reset_image_settings()
    assert effective_max_refs(get_image_settings(), _Capped()) == 3


def test_effective_max_refs_falls_back_to_config_without_caps(monkeypatch):
    monkeypatch.setenv("IMAGE_MAX_REFS", "5")
    reset_image_settings()

    class _NoCaps:
        pass

    assert effective_max_refs(get_image_settings(), _NoCaps()) == 5


def test_health_exposes_effective_and_configured_max_refs(env, monkeypatch):
    monkeypatch.setenv("IMAGE_MAX_REFS", "12")
    reset_image_settings()
    body = env.get("/api/image/health").json()
    assert body["max_refs"] == 12
    assert body["max_refs_effective"] == 6            # fake provider caps.max_refs=6
    assert body["max_prompt_chars"] >= 50


# ---------------------------------------------------------------- 创建期就报预算超限
def test_create_rejects_over_effective_budget_at_creation(env, monkeypatch):
    """配置放 12，但 provider 只支持 6：5 个「商品+颜色」× 2 张 = 10 > 6 → 创建期报错。"""
    monkeypatch.setenv("IMAGE_MAX_REFS", "12")
    monkeypatch.setenv("IMAGE_REFS_PER_ITEM", "2")
    reset_image_settings()
    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1001", "P-3002"],
        "all_colors": True,                       # P-1001 3 色 + P-3002 2 色 = 5 个目标
        "prompt": "白底主图", "image_count": 1,
    })
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error_code"] == "IMG_REFS_BUDGET"
    assert "上限 6 张" in body["detail"]


def test_create_reports_effective_limit_in_response(env, monkeypatch):
    monkeypatch.setenv("IMAGE_MAX_REFS", "12")
    reset_image_settings()
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1,
    })
    assert created.status_code == 201, created.text
    limits = created.json()["limits"]
    assert limits["max_refs"] == 6                 # 生效值
    assert limits["max_refs_configured"] == 12     # 配置值
