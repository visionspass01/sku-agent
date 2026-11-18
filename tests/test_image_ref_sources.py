"""W3：参考图三源（中台商品图 / 素材库 / 拍立淘）的接口与编排用例。"""

from __future__ import annotations

import base64
import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.png import placeholder_png
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.sources.fake_catalog import FakeCatalog
from app.agents_image.store import quota as quota_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
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


def _png_b64(text: str = "素材图") -> str:
    return base64.b64encode(placeholder_png(text, 640, 640)).decode("ascii")


def _wait(env, headers, task_id, timeout=25.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail.get("status") in {"done", "failed", "canceled"} and \
                detail.get("backfill_status") != "pending":
            return detail
        time.sleep(0.05)
    return detail


# ---------------------------------------------------------------- 素材库 CRUD
def test_library_upload_list_delete(env):
    headers = login(env)
    created = env.post("/api/image/library", headers=headers,
                       json={"data_b64": _png_b64("lib-1")})
    assert created.status_code == 201, created.text
    asset = created.json()
    assert asset["kind"] == "library"
    assert asset["width"] == 640
    assert asset["signed_url"].startswith("/api/image/assets/")

    listed = env.get("/api/image/library", headers=headers).json()
    assert asset["asset_id"] in {item["asset_id"] for item in listed["items"]}

    deleted = env.delete(f"/api/image/library/{asset['asset_id']}", headers=headers)
    assert deleted.status_code == 200
    after = env.get("/api/image/library", headers=headers).json()
    assert asset["asset_id"] not in {item["asset_id"] for item in after["items"]}


def test_library_rejects_bad_payload(env):
    headers = login(env)
    bad = env.post("/api/image/library", headers=headers, json={"data_b64": ""})
    assert bad.status_code == 422
    assert bad.json()["error_code"] == "IMG_REFERENCE_INVALID"

    not_image = env.post("/api/image/library", headers=headers,
                         json={"data_b64": base64.b64encode(b"not-an-image").decode()})
    assert not_image.status_code == 422

    too_big = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * (9 * 1024 * 1024)).decode()
    huge = env.post("/api/image/library", headers=headers, json={"data_b64": too_big})
    assert huge.status_code == 422
    assert "MB" in huge.json()["detail"]


def test_library_isolated_per_account(env):
    admin = login(env)
    worker = login(env, "worker1", "hhw111")
    mine = env.post("/api/image/library", headers=admin,
                    json={"data_b64": _png_b64("admin-lib")}).json()
    seen = {item["asset_id"] for item in env.get("/api/image/library", headers=worker).json()["items"]}
    assert mine["asset_id"] not in seen
    other = env.delete(f"/api/image/library/{mine['asset_id']}", headers=worker)
    assert other.status_code == 403


# ---------------------------------------------------------------- ref_source=library
def test_library_source_uses_selected_assets(env):
    headers = login(env)
    first = env.post("/api/image/library", headers=headers,
                     json={"data_b64": _png_b64("lib-a")}).json()
    second = env.post("/api/image/library", headers=headers,
                      json={"data_b64": _png_b64("lib-b")}).json()
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1,
        "ref_source": "library",
        "library_asset_ids": [first["asset_id"], second["asset_id"]],
    })
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["ref_source"] == "library"
    detail = _wait(env, headers, body["tasks"][0]["task_id"])
    assert detail["status"] == "done"
    assert detail["ref_source"] == "library"
    ref_ids = {item["asset_id"] for item in detail["refs"]}
    assert ref_ids == {first["asset_id"], second["asset_id"]}


def test_library_source_requires_selection(env):
    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1,
        "ref_source": "library",
    })
    assert resp.status_code == 422, resp.text
    assert resp.json()["error_code"] == "IMG_REFERENCE_REQUIRED"


def test_library_source_rejects_other_users_asset(env):
    admin = login(env)
    asset = env.post("/api/image/library", headers=admin,
                     json={"data_b64": _png_b64("admin-only")}).json()
    worker = login(env, "worker2", "hhw222")
    with session_scope() as session:
        quota_store.grant(session, "worker2", period_type="day", period_limit=20)
    resp = env.post("/api/image/tasks", headers=worker, json={
        "product_ids": ["P-1001"], "prompt": "白底主图", "image_count": 1,
        "ref_source": "library", "library_asset_ids": [asset["asset_id"]],
    })
    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == "IMG_FORBIDDEN"


# ---------------------------------------------------------------- ref_source=pailitao
def test_pailitao_source_takes_top_similar(env):
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1001"], "prompt": "白底主图", "image_count": 1,
        "ref_source": "pailitao",
    })
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["ref_source"] == "pailitao"
    detail = _wait(env, headers, body["tasks"][0]["task_id"])
    assert detail["status"] == "done", detail
    assert detail["ref_source"] == "pailitao"
    refs = detail["refs"]
    assert 1 <= len(refs) <= 5          # 销量 >100 的商品最多 5 个，每个 1 张主图


def test_pailitao_reports_when_no_product_passes_sales_gate(env):
    """没有销量超门槛的相似款 → 422 IMG_SIMILAR_NOT_FOUND，任务零残留。"""

    class _NoSimilarCatalog(FakeCatalog):
        async def find_similar(self, image_bytes, *, min_sales=100, top_n=5):
            return []

    ps.register_catalog("fake", _NoSimilarCatalog())
    batch_context.reset_product_batcher()
    headers = login(env)
    before = _task_count()
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1001"], "prompt": "白底主图", "image_count": 1,
        "ref_source": "pailitao",
    })
    assert resp.status_code == 422, resp.text
    assert resp.json()["error_code"] == "IMG_SIMILAR_NOT_FOUND"
    assert "未搜到销量超100的近似商品" in resp.json()["detail"]
    assert _task_count() == before


def test_pailitao_filters_low_sales_items(env):
    """相似款里销量 ≤100 的必须被丢掉，只保留 >100 的前 5 个。"""
    from app.agents_image.sources.product_source import SimilarProduct

    class _MixedCatalog(FakeCatalog):
        async def find_similar(self, image_bytes, *, min_sales=100, top_n=5):
            base = await super().find_similar(image_bytes, min_sales=0, top_n=6)
            low = SimilarProduct(product_id="P-LOW", title="低销量款", sales=12,
                                 image=base[0].image if base else None)
            return [low, *base]

    ps.register_catalog("fake", _MixedCatalog())
    batch_context.reset_product_batcher()
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3003"], "prompt": "白底主图", "image_count": 1,
        "ref_source": "pailitao",
    })
    assert created.status_code == 201, created.text
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert detail["status"] == "done"
    # 混合候选里 >100 的只有 4 个（860/320/240/150），低销量那个必须被丢掉
    assert len(detail["refs"]) == 4


def test_unknown_ref_source_is_rejected(env):
    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白底主图", "ref_source": "upload",
    })
    assert resp.status_code == 422
    assert "不支持的参考图来源" in resp.json()["detail"]


def test_default_ref_source_stays_mcp(env):
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1,
    })
    assert created.status_code == 201
    assert created.json()["ref_source"] == "mcp"
    assert created.json()["tasks"][0]["ref_source"] == "mcp"


def _task_count() -> int:
    with session_scope() as session:
        return len(task_store.list_tasks(session, limit=500))


# ---------------------------------------------------------------- 中台相似款接口
@pytest.fixture(scope="module")
def platform_env(tmp_path_factory):
    from tests.image_platform_support import init_platform_db, reset_platform_engine

    tmp = tmp_path_factory.mktemp("ref_sources_platform")
    init_platform_db(tmp)
    yield tmp
    reset_platform_engine()


@pytest.fixture()
def platform_api(platform_env):
    from tests.image_platform_support import platform_client

    with platform_client() as client:
        yield client


def test_platform_similar_endpoint_filters_and_sorts(platform_api):
    from tests.image_platform_support import TOKEN

    resp = platform_api.post("/api/v1/image/similar-by-image",
                             headers=TOKEN,
                             json={"image_b64": "cXVlcnk=", "min_sales": 100, "top_n": 5})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sales = [item["sales"] for item in body["items"]]
    assert sales == sorted(sales, reverse=True)
    assert all(value > 100 for value in sales)
    assert len(body["items"]) <= 5
    assert body["min_sales"] == 100


def test_platform_similar_endpoint_high_gate_returns_empty(platform_api):
    from tests.image_platform_support import TOKEN

    resp = platform_api.post("/api/v1/image/similar-by-image", headers=TOKEN,
                             json={"image_b64": "cXVlcnk=", "min_sales": 1000, "top_n": 5})
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_platform_similar_requires_token(platform_api):
    resp = platform_api.post("/api/v1/image/similar-by-image", json={"image_b64": "eA=="})
    assert resp.status_code == 401
