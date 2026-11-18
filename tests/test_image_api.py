"""生图 API 端到端（fake 中台替身 + 本地调度 + Fake Provider）。

链路：登录 → 取商品清单 → 批量建任务（参考图来自中台商品图）→ 出图 → 取图。
"""

from __future__ import annotations

import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.registry import reset_registry
from app.agents_image.sources import product_source as ps
from app.agents_image.sources.fake_catalog import FakeCatalog
from app.agents_image.store.schema import ensure_image_schema
from app.agents_image.store.users import ensure_seed_users
from app.db.engine import session_scope
from app.main import app


@pytest.fixture()
def image_env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.delenv("IMAGE_STORAGE", raising=False)
    monkeypatch.delenv("IMAGE_PROVIDER", raising=False)
    monkeypatch.delenv("IMAGE_PRODUCT_SOURCE", raising=False)
    reset_image_settings()
    reset_registry()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    image_metrics.reset()
    ensure_image_schema()
    with session_scope() as session:
        ensure_seed_users(session)
    yield app_client
    reset_image_settings()
    reset_registry()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    image_metrics.reset()


def login_headers(client, username: str = "admin", password: str = "hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture()
def auth_headers(image_env):
    return login_headers(image_env)


def _create(client, headers: dict, product_ids: list[str], **overrides) -> dict:
    body = {"product_ids": product_ids, "prompt": "白底主图，正面全身像上装突出"}
    body.update(overrides)
    resp = client.post("/api/image/tasks", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _wait_done(client, headers: dict, task_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        payload = client.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if payload.get("status") in {"done", "failed", "canceled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"任务未在 {timeout}s 内完成：{payload}")


# ---------------------------------------------------------------- 商品清单 / 预设
def test_products_and_catalog_come_from_platform(image_env, auth_headers):
    products = image_env.get("/api/image/products", headers=auth_headers)
    assert products.status_code == 200, products.text
    ids = [item["product_id"] for item in products.json()["products"]]
    assert "P-1001" in ids

    filtered = image_env.get("/api/image/products?keyword=针织", headers=auth_headers)
    assert [item["product_id"] for item in filtered.json()["products"]] == ["P-1001"]

    catalog = image_env.get("/api/image/catalog?platform=淘系", headers=auth_headers).json()
    assert any(item["label"] == "正面全身像·上装突出" for item in catalog["compositions"])
    assert any(item["label"] == "影棚正式风" for item in catalog["scene_styles"])
    assert catalog["plan_policy"]["default_count"] == 6
    assert catalog["platform_rules"]["aspect"] == "1:1"


# ---------------------------------------------------------------- 建任务 / 出图
def test_batch_creates_one_task_per_product(image_env, auth_headers):
    client = image_env
    created = _create(client, auth_headers, ["P-1001", "P-1002"], n=1)
    assert created["batch_id"] and len(created["tasks"]) == 2
    assert {item["product_id"] for item in created["tasks"]} == {"P-1001", "P-1002"}
    assert all(item["refs"] >= 1 for item in created["tasks"])

    task_ids = [item["task_id"] for item in created["tasks"]]
    assert len(set(task_ids)) == 2

    for task_id in task_ids:
        task = _wait_done(client, auth_headers, task_id)
        assert task["status"] == "done", task
        assert task["delivered_images"] == 1
        assert task["refs"], "参考图应来自中台商品图"
        assert task["refs"][0]["kind"] == "ref"
        assert task["product_id"] in {"P-1001", "P-1002"}

        got = client.get(task["assets"][0]["url"], headers=auth_headers)
        assert got.status_code == 200
        assert got.headers["content-type"].startswith("image/png")
        assert got.content.startswith(b"\x89PNG")

    listed = client.get(f"/api/image/tasks?batch_id={created['batch_id']}",
                        headers=auth_headers).json()["tasks"]
    assert {item["task_id"] for item in listed} == set(task_ids)


def test_slots_and_prompts_use_platform_presets(image_env, auth_headers):
    created = _create(image_env, auth_headers, ["P-1001"], n=2)
    slots = created["slots"]
    assert [item["slot"] for item in slots] == [1, 2]
    assert slots[0]["composition_id"] == "C-FULL-FRONT-TOP"
    assert slots[0]["composition_label"] == "正面全身像·上装突出"
    assert slots[0]["scene_style_label"] == "白底电商风"
    assert slots[1]["role"] == "上装卖点"


def test_task_payload_defaults_from_platform_policy(image_env, auth_headers):
    created = _create(image_env, auth_headers, ["P-2001"])
    # M3：图位数默认 9（中台模板），出图张数 = generate 图位数（6）
    assert len(created["slots"]) == 9
    assert created["image_count"] == 6
    assert created["limits"]["max_batch_products"] == 5


# ---------------------------------------------------------------- 错误路径
def test_missing_product_ids_rejected(image_env, auth_headers):
    resp = image_env.post("/api/image/tasks", headers=auth_headers,
                          json={"product_ids": [], "prompt": "白底主图"})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "IMG_INPUT_INVALID"


def test_batch_too_large(image_env, auth_headers):
    resp = image_env.post("/api/image/tasks", headers=auth_headers,
                          json={"product_ids": [f"P-{i}" for i in range(1, 7)], "prompt": "x"})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "IMG_BATCH_TOO_LARGE"


def test_unknown_product_is_404(image_env, auth_headers):
    resp = image_env.post("/api/image/tasks", headers=auth_headers,
                          json={"product_ids": ["P-9999"], "prompt": "白底主图"})
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "IMG_PRODUCT_NOT_FOUND"


def test_product_without_images_is_422(image_env, auth_headers):
    resp = image_env.post("/api/image/tasks", headers=auth_headers,
                          json={"product_ids": ["P-4001"], "prompt": "白底主图"})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "IMG_REFERENCE_REQUIRED"
    assert resp.headers["X-Error-Code"] == "IMG_REFERENCE_REQUIRED"
    assert resp.headers["X-Trace-Id"]


def test_platform_unavailable_fails_fast_without_tasks(image_env, auth_headers):
    """中台不可用 → 503，任务表零残留（不建任务、不入队）。"""
    catalog = ps.get_catalog()
    assert isinstance(catalog, FakeCatalog)
    before = image_env.get("/api/image/stats", headers=auth_headers).json()["tasks_total"]
    catalog.fail_next = "unavailable"
    resp = image_env.post("/api/image/tasks", headers=auth_headers,
                          json={"product_ids": ["P-1001"], "prompt": "白底主图"})
    assert resp.status_code == 503
    assert resp.json()["error_code"] == "IMG_UPSTREAM_MCP_UNAVAILABLE"
    assert resp.json()["retryable"] is True

    after = image_env.get("/api/image/stats", headers=auth_headers).json()["tasks_total"]
    assert after == before            # 事务回滚：一条任务都没多


def test_unknown_preset_rejected(image_env, auth_headers):
    resp = image_env.post("/api/image/tasks", headers=auth_headers, json={
        "product_ids": ["P-1001"], "prompt": "白底主图", "compositions": ["C-NOT-EXIST"],
    })
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "IMG_INPUT_INVALID"


def test_upload_endpoint_is_gone(image_env, auth_headers):
    """v6：用户不能上传参考图 —— 接口从 OpenAPI 里消失。"""
    paths = app.openapi()["paths"]
    assert not any("/upload" in path for path in paths)
    resp = image_env.post("/api/image/upload", content=b"x",
                          headers={**auth_headers, "Content-Type": "image/png"})
    assert resp.status_code in {404, 405}


# ---------------------------------------------------------------- 健康 / 统计 / 鉴权
def test_health_and_stats(image_env, auth_headers):
    health = image_env.get("/api/image/health")
    body = health.json()
    assert body["status"] == "ok"
    assert body["provider"] == "fake"
    assert body["product_source"] == "fake"
    assert body["max_refs"] == 6
    assert health.headers["X-Trace-Id"]
    stats = image_env.get("/api/image/stats", headers=auth_headers).json()
    assert "tasks_created" in stats and "in_flight_images" in stats


def test_endpoints_require_login(image_env):
    """除 /health 外都必须登录：不带 Authorization → 401 IMG_UNAUTHORIZED。"""
    assert image_env.get("/api/image/health").status_code == 200
    for path in ("/api/image/stats", "/api/image/tasks", "/api/image/products",
                 "/api/image/catalog", "/api/image/tasks/abc", "/api/image/assets/abc"):
        resp = image_env.get(path)
        assert resp.status_code == 401, path
        assert resp.json()["error_code"] == "IMG_UNAUTHORIZED"
    resp = image_env.post("/api/image/tasks", json={"product_ids": ["P-1001"], "prompt": "x"})
    assert resp.status_code == 401


def test_unknown_task_and_asset_are_404(image_env, auth_headers):
    assert image_env.get("/api/image/tasks/20261117000000abcd",
                         headers=auth_headers).status_code == 404
    assert image_env.get("/api/image/assets/nope", headers=auth_headers).status_code == 404
