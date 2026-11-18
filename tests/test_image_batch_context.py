"""W2：商品上下文批量取数（3 秒合并 MCP 请求）+ 部门闸前置 + 颜色色值兜底。"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import get_image_settings, reset_image_settings
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.sources import refs as refs_source
from app.agents_image.sources.fake_catalog import FakeCatalog
from app.agents_image.sources.product_source import ProductData, ProductSku
from app.agents_image.store import quota as quota_store
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


class _CountingCatalog:
    """轻量替身：只数"批量取数被调用几次"（不生成 PNG，避免测出的是图片编码耗时）。"""

    name = "counting"

    def __init__(self) -> None:
        self.batch_calls = 0
        self.single_calls = 0
        self.fail = False

    def _product(self, product_id: str) -> ProductData:
        return ProductData(product_id=product_id, title=f"商品{product_id}", category="女装",
                           attrs={"color_names": ["米色"], "color_hex": "#C8B7A6"},
                           skus=[ProductSku(sku_id=f"{product_id}-米色", color="米色",
                                            color_hex="#C8B7A6")])

    async def get_products_context(self, product_ids: list[str]):
        self.batch_calls += 1
        await asyncio.sleep(0.01)          # 模拟一次真实往返
        if self.fail:
            raise RuntimeError("中台挂了")
        return {pid: self._product(pid) for pid in product_ids}

    async def get_product(self, product_id: str):
        self.single_calls += 1
        return self._product(product_id)


# ---------------------------------------------------------------- 合并窗口
def test_window_merges_concurrent_requests_into_one_batch(monkeypatch):
    monkeypatch.setenv("IMAGE_CONTEXT_BATCH_WINDOW_SECONDS", "0.2")
    reset_image_settings()
    catalog = _CountingCatalog()
    batcher = batch_context.ProductContextBatcher(catalog)

    async def scenario():
        return await asyncio.gather(
            batcher.get_many(["P-1001"]),
            batcher.get_many(["P-1002"]),
            batcher.get_many(["P-1001", "P-3001"]),
        )

    results = asyncio.run(scenario())
    assert set(results[0]) == {"P-1001"}
    assert set(results[1]) == {"P-1002"}
    assert set(results[2]) == {"P-1001", "P-3001"}
    # 三个并发请求 → 一次合并后的 MCP 调用（不是三次）
    assert catalog.batch_calls == 1
    assert batcher.batches == 1
    reset_image_settings()


def test_cache_serves_repeat_within_ttl(monkeypatch):
    monkeypatch.setenv("IMAGE_CONTEXT_BATCH_WINDOW_SECONDS", "0")
    monkeypatch.setenv("IMAGE_CONTEXT_CACHE_TTL_SECONDS", "60")
    reset_image_settings()
    catalog = _CountingCatalog()
    batcher = batch_context.ProductContextBatcher(catalog)

    asyncio.run(batcher.get_many(["P-1001"]))
    asyncio.run(batcher.get_many(["P-1001"]))
    assert catalog.batch_calls == 1
    assert batcher.cache_hits >= 1
    reset_image_settings()


def test_batch_is_chunked_by_max_ids(monkeypatch):
    monkeypatch.setenv("IMAGE_CONTEXT_BATCH_WINDOW_SECONDS", "0")
    monkeypatch.setenv("IMAGE_CONTEXT_BATCH_MAX_IDS", "2")
    reset_image_settings()
    catalog = _CountingCatalog()
    batcher = batch_context.ProductContextBatcher(catalog)
    got = asyncio.run(batcher.get_many(["P-1001", "P-1002", "P-2001"]))
    assert set(got) == {"P-1001", "P-1002", "P-2001"}
    assert catalog.batch_calls == 2         # 3 个 id / 每次最多 2 个
    reset_image_settings()


def test_catalog_without_batch_method_falls_back_to_single_calls():
    class _SingleOnly(FakeCatalog):
        get_products_context = None       # type: ignore[assignment]

    catalog = _SingleOnly()
    got = asyncio.run(batch_context.fetch_products_context(catalog, ["P-1001", "P-1002"]))
    assert set(got) == {"P-1001", "P-1002"}


# ---------------------------------------------------------------- 建任务链路
def test_department_gate_blocks_before_downloading_refs(env, monkeypatch):
    """越权商品：403，且**没有**为它下载/落库任何参考图。"""
    headers = login(env, "worker2", "hhw222")     # worker2 = 女装
    with session_scope() as session:
        quota_store.grant(session, "worker2", period_type="day", period_limit=50)
    before = _ref_assets("P-1002")                # 男装商品
    resp = env.post("/api/image/tasks", headers=headers,
                    json={"product_ids": ["P-1002"], "prompt": "白底主图", "image_count": 1})
    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == "IMG_FORBIDDEN"
    assert _ref_assets("P-1002") == before


def _ref_assets(product_id: str) -> int:
    with session_scope() as session:
        from sqlalchemy import func, select

        from app.agents_image.store.models import ImageAsset

        return int(session.execute(
            select(func.count()).select_from(ImageAsset)
            .where(ImageAsset.product_id == product_id, ImageAsset.kind == "ref")
        ).scalar_one())


def test_unknown_category_is_forbidden(env):
    """类目没维护（映射不到部门）→ 普通账号 403，不能"类目空就放过"。"""

    class _NoCategoryCatalog(FakeCatalog):
        async def get_product(self, product_id: str):
            data = await super().get_product(product_id)
            return ProductData(product_id=data.product_id, title=data.title, category="",
                               attrs=data.attrs, images=data.images, skus=data.skus,
                               sku_ids=data.sku_ids)

    ps.register_catalog("fake", _NoCategoryCatalog())
    batch_context.reset_product_batcher()
    headers = login(env, "worker1", "hhw111")
    with session_scope() as session:
        quota_store.grant(session, "worker1", period_type="day", period_limit=50)
    resp = env.post("/api/image/tasks", headers=headers,
                    json={"product_ids": ["P-1002"], "prompt": "白底主图", "image_count": 1})
    assert resp.status_code == 403, resp.text
    assert "无法映射到部门" in resp.json()["detail"]


def test_missing_color_hex_is_rejected_instead_of_borrowing_other_color(env):
    """目标颜色没有色值 → 422（不能静默沿用商品首个颜色的 hex，否则直接串色）。"""

    class _NoHexCatalog(FakeCatalog):
        async def get_product(self, product_id: str):
            data = await super().get_product(product_id)
            skus = [ProductSku(sku_id=sku.sku_id, color=sku.color,
                               color_hex="" if sku.color == "黑色" else sku.color_hex,
                               size=sku.size) for sku in data.skus]
            return ProductData(product_id=data.product_id, title=data.title,
                               category=data.category, attrs=data.attrs,
                               images=data.images, skus=skus, sku_ids=data.sku_ids)

    ps.register_catalog("fake", _NoHexCatalog())
    batch_context.reset_product_batcher()
    headers = login(env, "admin", "hhw123")
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1001"],
        "product_colors": [{"product_id": "P-1001", "colors": ["黑色"]}],
        "prompt": "白底主图", "image_count": 1,
    })
    assert resp.status_code == 422, resp.text
    assert resp.json()["error_code"] == "IMG_INPUT_INVALID"
    assert "黑色" in resp.json()["detail"] and "色值" in resp.json()["detail"]


def test_single_color_product_may_reuse_attrs_hex():
    """单色商品：颜色与 attrs 完全对上时允许沿用中台 attrs 的 color_hex。"""
    from app.agents_image.api.routes import _attrs_for_target

    attrs = _attrs_for_target({"color_names": ["米色"], "color_hex": "#C8B7A6"},
                              refs_source.RefTarget(product_id="P-1001", color="米色"))
    assert attrs["color_hex"] == "#C8B7A6"


def test_resolve_refs_uses_prefetched_products(env):
    """给了预取的商品上下文就不再逐个回中台取商品（批量请求才是唯一往返）。"""
    calls = {"get_product": 0}

    class _NoSingleCatalog(FakeCatalog):
        async def get_product(self, product_id: str):
            calls["get_product"] += 1
            return await super().get_product(product_id)

    catalog = _NoSingleCatalog()
    products = asyncio.run(catalog.get_products_context(["P-1001"]))
    calls["get_product"] = 0
    targets = [refs_source.RefTarget(product_id="P-1001", color="米色", color_hex="#C8B7A6")]
    with session_scope() as session:
        resolution = asyncio.run(refs_source.resolve_refs(
            catalog, targets, session=session, username="u-prefetch", department=None,
            refs_per_item=1, max_refs=2, products=products))
    assert resolution.total == 1
    assert calls["get_product"] == 0


def test_health_exposes_batch_stats(env):
    headers = login(env)
    env.post("/api/image/tasks", headers=headers,
             json={"product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1})
    body = env.get("/api/image/health").json()
    assert "context_batches" in body
    assert body["context_batches"]["batches"] >= 1


# ---------------------------------------------------------------- 中台批量接口
@pytest.fixture(scope="module")
def platform_env(tmp_path_factory):
    from tests.image_platform_support import init_platform_db, reset_platform_engine

    tmp = tmp_path_factory.mktemp("batch_context_platform")
    init_platform_db(tmp)
    yield tmp
    reset_platform_engine()


@pytest.fixture()
def platform_api(platform_env):
    from tests.image_platform_support import platform_client

    with platform_client() as client:
        yield client


def test_platform_products_context_endpoint(platform_api):
    from tests.image_platform_support import TOKEN

    resp = platform_api.get("/api/v1/image/products/context",
                            params={"ids": "P-1001,P-9999"}, headers=TOKEN)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [item["product_id"] for item in body["products"]] == ["P-1001"]
    assert body["missing"] == ["P-9999"]
    assert body["count"] == 1


def test_platform_products_context_requires_token(platform_api):
    assert platform_api.get("/api/v1/image/products/context",
                            params={"ids": "P-1001"}).status_code == 401


def test_platform_products_context_empty_ids(platform_api):
    from tests.image_platform_support import TOKEN

    resp = platform_api.get("/api/v1/image/products/context", params={"ids": ""},
                            headers=TOKEN)
    assert resp.status_code == 200
    assert resp.json()["products"] == []


# ---------------------------------------------------------------- 计时护栏
def test_batcher_does_not_stall_with_zero_window(monkeypatch):
    monkeypatch.setenv("IMAGE_CONTEXT_BATCH_WINDOW_SECONDS", "0")
    reset_image_settings()
    catalog = _CountingCatalog()
    batcher = batch_context.ProductContextBatcher(catalog)
    started = time.monotonic()
    asyncio.run(batcher.get_many(["P-1001"]))
    assert time.monotonic() - started < 2.0
    assert get_image_settings().context_batch_window_seconds == 0.0
    reset_image_settings()
