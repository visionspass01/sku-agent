"""第三轮 W4：参考图两阶段（取字节/落库）+ 复用窗口 + 拍立淘选色与归属 + 素材库去重 + 引用过期。"""

from __future__ import annotations

import asyncio
import base64
import time

import pytest

from app.agents_image import graph
from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import get_image_settings, reset_image_settings
from app.agents_image.png import placeholder_compliant_png
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.sources import refs as refs_source
from app.agents_image.sources.fake_catalog import FakeCatalog
from app.agents_image.sources.product_source import (
    ProductData,
    ProductImage,
    ProductSku,
    SimilarProduct,
)
from app.agents_image.store import assets as asset_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.core.errors import ImageError
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


def _wait(env, headers, task_id, timeout=45.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail.get("status") in {"done", "failed", "canceled"} and \
                detail.get("backfill_status") != "pending":
            return detail
        time.sleep(0.05)
    return detail


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------- P1-5：两阶段（取字节 / 落库）
def test_plan_refs_does_not_write_assets(env):
    catalog = ps.get_catalog()
    targets = [refs_source.RefTarget(product_id="P-1001", color="米色", color_hex="#C8B7A6")]
    with session_scope() as session:
        plan = asyncio.run(refs_source.plan_refs(catalog, targets, refs_per_item=2, max_refs=6))
        # 计划阶段只下载，不写资产表
        assert plan.total >= 1
        assert plan.refs_of(targets[0].color_key)[0].data.startswith(b"\x89PNG")
        created = asset_store.list_refs(session, "no-such-task")
        assert created == []
        resolution = refs_source.persist_refs(session, plan, username="worker9")
        assert resolution.total == plan.total
        again = refs_source.persist_refs(session, plan, username="worker9")
    # 落库按 (sha256, 账号, 商品, 颜色) 去重：第二次不再新增行
    assert [item.asset_id for item in resolution.refs_of(targets[0].color_key)] == \
           [item.asset_id for item in again.refs_of(targets[0].color_key)]


# ---------------------------------------------------------------- P1-2：复用窗口
def test_iterate_reuse_is_limited_to_slot_window(env, monkeypatch):
    headers = login(env)
    base = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白底主图", "slot_count": 9, "image_count": 6,
    })
    base_id = base.json()["tasks"][0]["task_id"]
    base_detail = _wait(env, headers, base_id)
    assert base_detail["status"] == "done"
    assert {asset["slot"] for asset in base_detail["assets"]} == {1, 2, 3, 4, 5, 6}

    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "只改主图", "mode": "iterate",
        "base_task_id": base_id, "slot_count": 2, "image_count": 1,
    })
    assert created.status_code == 201, created.text
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert detail["status"] == "done"
    assert detail["slot_count"] == 2
    # 只有 slot 2 在窗口内可复用；3..6 不能挂进来
    assert detail["reused_images"] == 1
    assert {asset["slot"] for asset in detail["assets"]} == {1, 2}


# ---------------------------------------------------------------- P1-7：拍立淘按商品自身颜色选图
class _ColorCatalog(FakeCatalog):
    """两个商品、每个商品两种颜色；主图字节不同，用来断言"搜图用的是哪个颜色的图"。"""

    def __init__(self) -> None:
        super().__init__()
        self.queries: list[str] = []

    def _product(self, product_id: str, colors: list[tuple[str, str]]) -> ProductData:
        images = [
            ProductImage(image_id=f"{product_id}-{color}", is_main=True, mime="image/png",
                         color=color, color_hex=hex_value,
                         bytes_b64=_b64(placeholder_compliant_png(f"{product_id}:{color}", 64, 64)))
            for color, hex_value in colors
        ]
        skus = [ProductSku(sku_id=f"{product_id}-{color}", color=color, color_hex=hex_value)
                for color, hex_value in colors]
        return ProductData(product_id=product_id, title=f"商品{product_id}", category="女装",
                           attrs={"color_names": [colors[0][0]], "color_hex": colors[0][1]},
                           images=images, skus=skus)

    async def get_product(self, product_id: str):
        if product_id == "P-A":
            return self._product("P-A", [("米色", "#C8B7A6"), ("黑色", "#111111")])
        return self._product("P-B", [("藏蓝", "#2C5D8F"), ("雾蓝", "#5A8FBF")])

    async def find_similar(self, image_bytes: bytes, *, min_sales: int = 100, top_n: int = 5):
        self.queries.append(base64.b64encode(image_bytes).decode("ascii"))
        return [SimilarProduct(product_id="P-SIM", title="相似款", sales=500,
                               image=ProductImage(image_id="sim-1", is_main=True,
                                                  mime="image/png",
                                                  bytes_b64=_b64(placeholder_compliant_png("sim", 64, 64))))]


def test_pailitao_uses_each_products_own_color():
    catalog = _ColorCatalog()
    targets = [
        refs_source.RefTarget(product_id="P-A", color="黑色", color_hex="#111111"),
        refs_source.RefTarget(product_id="P-B", color="雾蓝", color_hex="#5A8FBF"),
    ]
    products = {pid: asyncio.run(catalog.get_product(pid)) for pid in ("P-A", "P-B")}
    plan = asyncio.run(refs_source.plan_pailitao_refs(
        catalog, targets, products=products, min_sales=100, top_n=5, max_refs=6))
    assert plan.total == 2                      # 每个商品各 1 张相似款主图
    expected = {}
    for product_id, color in (("P-A", "黑色"), ("P-B", "雾蓝")):
        expected[_b64(placeholder_compliant_png(f"{product_id}:{color}", 64, 64))] = product_id
    assert set(catalog.queries) == set(expected)   # 用的是"各商品自己的目标颜色"主图


# ---------------------------------------------------------------- P1-8：搜图输入也校验归属
def test_pailitao_query_asset_requires_ownership(env):
    admin = login(env)
    asset = env.post("/api/image/library", headers=admin,
                     json={"data_b64": _b64(placeholder_compliant_png("admin-lib", 64, 64))}).json()
    worker = login(env, "worker2", "hhw222")
    resp = env.post("/api/image/tasks", headers=worker, json={
        "product_ids": ["P-1001"], "prompt": "白底主图", "image_count": 1,
        "ref_source": "pailitao", "library_asset_ids": [asset["asset_id"]],
    })
    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == "IMG_FORBIDDEN"


def test_pailitao_query_bytes_searches_once_for_all_products():
    """P2-8：勾了素材库图时所有商品用同一张搜图输入 → 只搜一次（过去 N 个商品搜 N 次）。"""
    catalog = _ColorCatalog()
    targets = [
        refs_source.RefTarget(product_id="P-A", color="黑色", color_hex="#111111"),
        refs_source.RefTarget(product_id="P-B", color="雾蓝", color_hex="#5A8FBF"),
    ]
    products = {pid: asyncio.run(catalog.get_product(pid)) for pid in ("P-A", "P-B")}
    plan = asyncio.run(refs_source.plan_pailitao_refs(
        catalog, targets, products=products, query_bytes=b"same-query-image",
        min_sales=100, top_n=5, max_refs=6))
    assert len(catalog.queries) == 1, catalog.queries
    assert plan.total == 2                       # 两个目标各 1 张，复用同一次搜索结果


# ---------------------------------------------------------------- P2-10：素材库按内容去重
def test_library_upload_dedupes_same_image(env):
    headers = login(env)
    data = _b64(placeholder_compliant_png("dup", 640, 640))
    first = env.post("/api/image/library", headers=headers, json={"data_b64": data})
    second = env.post("/api/image/library", headers=headers, json={"data_b64": data})
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["asset_id"] == first.json()["asset_id"]
    assert second.json().get("deduped") is True
    listed = env.get("/api/image/library", headers=headers).json()["items"]
    assert sum(1 for item in listed if item["asset_id"] == first.json()["asset_id"]) == 1


def test_library_upload_sniffs_mime_instead_of_trusting_body(env):
    """P2-6（第八轮）：扩展名/mime 以字节为准——伪造 mime 的假图必须被拒。"""
    import base64

    headers = login(env)
    fake = base64.b64encode(b"this-is-not-an-image").decode("ascii")
    rejected = env.post("/api/image/library", headers=headers,
                        json={"data_b64": fake, "mime": "image/png"})
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error_code"] == "IMG_REFERENCE_INVALID"

    real = env.post("/api/image/library", headers=headers,
                    json={"data_b64": _b64(placeholder_compliant_png("mime", 640, 640)),
                          "mime": "text/plain"})
    assert real.status_code == 201, real.text
    assert real.json()["mime"] == "image/png", "以字节嗅探结果为准，不信任请求里的 mime"


# ---------------------------------------------------------------- P2-12：参考图被清理 → 409
def test_expired_reference_raises_reference_expired(env, monkeypatch):
    headers = login(env)

    class _Stub:
        async def submit(self, task_id: str) -> str:
            return task_id

    monkeypatch.setattr("app.agents_image.api.routes.get_dispatcher", lambda: _Stub())
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1})
    task_id = created.json()["tasks"][0]["task_id"]
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        ref_ids = list(task.ref_asset_ids)
    # 模拟 24h 清理：删掉文件，行还在
    for asset_id in ref_ids:
        with session_scope() as session:
            asset = asset_store.get_asset(session, asset_id)
            key = asset.key
        path = get_image_settings().images_dir / key
        if path.exists():
            path.unlink()
    with pytest.raises(ImageError) as exc:
        with session_scope() as session:
            graph._load_refs(session, ref_ids)
    assert exc.value.code == "IMG_REFERENCE_EXPIRED"
