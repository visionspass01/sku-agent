"""取数接缝（`sources`）：中台替身契约、参考图解析、失败语义（不降级）。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.agents_image.config import reset_image_settings
from app.agents_image.sources import product_source as ps
from app.agents_image.sources import refs as refs_source
from app.agents_image.sources.fake_catalog import FakeCatalog
from app.agents_image.sources.product_source import ProductData, ProductImage
from app.agents_image.store import assets as asset_store
from app.agents_image.store.schema import ensure_image_schema
from app.core.errors import ImageError
from app.db.engine import session_scope


@pytest.fixture()
def cat(tmp_path, monkeypatch, _db):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.delenv("IMAGE_PRODUCT_SOURCE", raising=False)
    reset_image_settings()
    ps.reset_catalogs()
    ps._ensure_builtin()
    ensure_image_schema()
    yield ps.get_catalog()
    reset_image_settings()
    ps.reset_catalogs()
    ps._ensure_builtin()


def _resolve(catalog, product_ids, *, username="worker1", max_refs=6, timeout=5.0):
    async def run():
        with session_scope() as session:
            return await refs_source.resolve_refs(
                catalog, product_ids, session=session, username=username,
                department=None, max_refs=max_refs, timeout_seconds=timeout,
            )

    return asyncio.run(run())


# ---------------------------------------------------------------- 替身契约
def test_fake_catalog_lists_and_filters(cat):
    rows = asyncio.run(cat.list_products())
    assert {row.product_id for row in rows} >= {"P-1001", "P-1002", "P-2001"}
    filtered = asyncio.run(cat.list_products(keyword="工装"))
    assert [row.product_id for row in filtered] == ["P-1002"]
    assert len(asyncio.run(cat.list_products(limit=2))) == 2


def test_fake_catalog_product_has_locked_attrs_and_images(cat):
    data = asyncio.run(cat.get_product("P-1001"))
    assert data.category == "女装"
    assert data.attrs["must_not_change"]
    assert data.attrs["source"] == "platform"
    assert len(data.images) == 3
    assert data.images[0].is_main is True
    assert all(image.bytes_b64 for image in data.images)


def test_fake_catalog_presets_and_policy(cat):
    assert len(asyncio.run(cat.list_compositions())) >= 4
    assert len(asyncio.run(cat.list_scene_styles())) >= 4
    policy = asyncio.run(cat.get_plan_policy())
    assert policy.default_count == 6 and len(policy.slots) == 9
    assert policy.default_composition_id and policy.default_scene_style_id
    rules = asyncio.run(cat.get_platform_rules(platform="淘系"))
    assert rules.aspect == "1:1" and rules.forbidden_words
    assert asyncio.run(cat.save_result({"task_id": "t1"}))["saved"] is True


def test_registry_switching_and_unknown_source(cat):
    assert "fake" in ps.registered_catalogs()
    assert ps.get_catalog().name == "fake"
    with pytest.raises(ImageError) as exc:
        ps.get_catalog("nope")
    assert exc.value.code == "IMG_INTERNAL"


# ---------------------------------------------------------------- 参考图解析
def test_resolve_refs_writes_ref_assets(cat):
    resolution = _resolve(cat, ["P-1001"])
    assert resolution.total == 3
    refs = resolution.refs_of("P-1001")
    assert refs and all(item.source == "mcp" for item in refs)
    assert all(item.product_id == "P-1001" for item in refs)
    assert refs[0].image_id == "P-1001-img-1"
    with session_scope() as session:
        asset = asset_store.get_asset(session, refs[0].asset_id)
        assert asset.kind == "ref" and asset.product_id == "P-1001"
        assert len(asset_store.read_asset_bytes(asset)) == refs[0].bytes


def test_resolve_refs_respects_total_budget(cat):
    resolution = _resolve(cat, ["P-1001", "P-2001"], max_refs=2)
    assert resolution.total == 2                     # 每商品至少 1 张，合计不超上限
    assert len(resolution.refs_of("P-1001")) == 1
    assert len(resolution.refs_of("P-2001")) == 1
    assert resolution.truncated > 0


def test_resolve_refs_product_without_images(cat):
    with pytest.raises(ImageError) as exc:
        _resolve(cat, ["P-4001"])
    assert exc.value.code == "IMG_REFERENCE_REQUIRED"


def test_resolve_refs_unknown_product(cat):
    with pytest.raises(ImageError) as exc:
        _resolve(cat, ["P-9999"])
    assert exc.value.code == "IMG_PRODUCT_NOT_FOUND"


def test_resolve_refs_platform_unavailable_fails_hard(cat):
    cat.fail_next = "unavailable"
    with pytest.raises(ImageError) as exc:
        _resolve(cat, ["P-1001"])
    assert exc.value.code == "IMG_UPSTREAM_MCP_UNAVAILABLE"
    assert exc.value.retryable is True


def test_resolve_refs_invalid_payload(cat):
    cat.fail_next = "invalid"
    with pytest.raises(ImageError) as exc:
        _resolve(cat, ["P-1001"])
    assert exc.value.code == "IMG_UPSTREAM_INVALID"


def test_resolve_refs_is_idempotent_per_account(cat):
    first = _resolve(cat, ["P-1002"], username="worker1")
    again = _resolve(cat, ["P-1002"], username="worker1")
    assert first.asset_ids_of("P-1002") == again.asset_ids_of("P-1002")

    other = _resolve(cat, ["P-1002"], username="worker2")
    assert set(other.asset_ids_of("P-1002")).isdisjoint(first.asset_ids_of("P-1002"))


def test_resolve_refs_download_failure_is_503(cat, monkeypatch):
    async def boom(self, url, *args, **kwargs):
        raise httpx.ConnectError("download failed")

    monkeypatch.setattr(httpx.AsyncClient, "get", boom)
    remote = ProductData(product_id="P-X", category="女装", attrs={},
                         images=[ProductImage(image_id="i1", url="http://platform/x.png",
                                              is_main=True)])

    class Catalog(FakeCatalog):
        async def get_product(self, product_id: str):
            return remote

    with pytest.raises(ImageError) as exc:
        _resolve(Catalog(), ["P-X"])
    assert exc.value.code == "IMG_UPSTREAM_MCP_UNAVAILABLE"
