"""第八轮 P1-2：目标颜色没有对应参考图时，必须留痕（不能静默串色）。

`_resolve_targets` 的颜色来自 **SKU**，参考图颜色来自**图片标签**；两者不同步时
`_pick_images` 会退回"未标色图 / 其它颜色的图"，而 Prompt 的 color_hex 仍按目标颜色锁定，
等于"照着黑色参考图画米色"。上一版这条兜底完全静默，本轮补 `IMG_REF_COLOR_MISMATCH`：
告警要挂到计划/落库结果上，最终落到任务 warnings。
"""

from __future__ import annotations

import asyncio
import base64

from app.agents_image.png import placeholder_png
from app.agents_image.sources import refs as refs_source
from app.agents_image.sources.product_source import (
    ProductData,
    ProductImage,
    ProductSku,
)


class _StubCatalog:
    """只实现取商品；图片字节直接给 base64（走 `_fetch` 的 b64 分支）。"""

    name = "stub"

    def __init__(self, product: ProductData) -> None:
        self.product = product

    async def get_product(self, product_id: str) -> ProductData:  # noqa: ARG002
        return self.product


def _image(image_id: str, color: str, *, main: bool = False) -> ProductImage:
    return ProductImage(
        image_id=image_id, mime="image/png", is_main=main, width=64, height=64,
        color=color,
        bytes_b64=base64.b64encode(placeholder_png(image_id, 64, 64)).decode("ascii"),
    )


def _product(images: list[ProductImage], colors: list[str]) -> ProductData:
    return ProductData(
        product_id="P-STUB",
        title="测试商品",
        category="女装",
        attrs={"product_type": "测试品类", "color_hex": "#C8B7A6",
               "must_not_change": ["颜色", "版型"]},
        images=images,
        skus=[ProductSku(sku_id=f"sku-{color}", color=color) for color in colors],
    )


def _plan(product: ProductData, color: str) -> refs_source.RefPlan:
    catalog = _StubCatalog(product)
    return asyncio.run(refs_source.plan_refs(
        catalog,
        [refs_source.RefTarget(product_id="P-STUB", color=color, color_hex="#C8B7A6")],
        products={"P-STUB": product},
        refs_per_item=1, max_refs=6, timeout_seconds=0.1,
    ))


def test_missing_color_reference_emits_warning(_db):
    """SKU 有「米色」但图只标了「黑色」→ 退回黑色图，但必须告警。"""
    product = _product([_image("img-black", "黑色", main=True)], ["米色"])
    plan = _plan(product, "米色")
    assert plan.refs["P-STUB#米色"], plan.refs
    assert plan.warnings, "目标颜色无对应图时必须留痕"
    warning = plan.warnings[0]
    assert warning.startswith("IMG_REF_COLOR_MISMATCH")
    assert "米色" in warning and "黑色" in warning

    # 落库阶段要把告警带进 resolution（create_tasks 再挂到任务 warnings）
    from app.agents_image.store.schema import ensure_image_schema
    from app.db.engine import session_scope

    ensure_image_schema()
    with session_scope() as session:
        resolution = refs_source.persist_refs(session, plan, username="u-color")
    assert any("IMG_REF_COLOR_MISMATCH" in item for item in resolution.warnings)


def test_matched_color_has_no_warning(_db):
    product = _product([_image("img-beige", "米色", main=True)], ["米色"])
    plan = _plan(product, "米色")
    assert plan.warnings == []
    assert plan.refs["P-STUB#米色"][0].image_id == "img-beige"


def test_untagged_images_are_preferred_over_other_colors_but_still_warned(_db):
    """既有未标色图也有别的颜色图：优先用未标色图（更接近），但仍要告警。"""
    product = _product(
        [_image("img-black", "黑色", main=True), _image("img-plain", "")], ["米色"])
    plan = _plan(product, "米色")
    assert plan.warnings, "仍然缺少目标颜色的图"
    assert "未标色" in plan.warnings[0]
    assert plan.refs["P-STUB#米色"][0].image_id == "img-plain"
