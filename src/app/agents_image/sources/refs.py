"""把「选中商品 + 颜色」解析成参考图：经 catalog 取图 → 下载/解码 → 落本地 refs 资产。

规则（M3，按颜色粒度）：
- 出图的最小业务单位是 **商品 + 颜色**：米色商品必须用米色的参考图，否则颜色会漂；
- 每个「商品+颜色」至少 1 张（该颜色主图），尽量取到 `IMAGE_REFS_PER_ITEM`（默认 2：主图+细节）；
- 预算不足（颜色数 × 2 > `IMAGE_MAX_REFS`）→ 明确报 `IMG_REFS_BUDGET`，让用户减少颜色/商品，
  而不是悄悄少给参考图（少给参考图 = 身份锁定失效）；
- 取不到字节（网络/坏图）→ `IMG_UPSTREAM_MCP_UNAVAILABLE`(503)；
- 落库 `kind="ref"`、`source="mcp"`、带 `product_id` + `color_key`，同账号重复取同图复用资产行。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

import httpx

from app.agents_image.sources.product_source import PlatformCatalog, ProductData, ProductImage
from app.agents_image.state import RefImage
from app.agents_image.store import assets as asset_store
from app.core.errors import ImageError, raise_image


@dataclass(frozen=True)
class RefTarget:
    """一个出图目标：商品 + 颜色（颜色为空表示该商品没有颜色维度）。"""

    product_id: str
    color: str = ""
    color_hex: str = ""

    @property
    def color_key(self) -> str:
        return f"{self.product_id}#{self.color}" if self.color else self.product_id


@dataclass
class RefResolution:
    """一次批量解析的结果：refs 按 `color_key` 分组，供建任务时逐个目标取用。"""

    refs: dict[str, list[RefImage]] = field(default_factory=dict)
    products: dict[str, ProductData] = field(default_factory=dict)
    truncated: int = 0

    @property
    def total(self) -> int:
        return sum(len(items) for items in self.refs.values())

    def refs_of(self, color_key: str) -> list[RefImage]:
        return list(self.refs.get(color_key) or [])

    def asset_ids_of(self, color_key: str) -> list[str]:
        return [item.asset_id for item in self.refs.get(color_key) or []]


async def _fetch(image, *, timeout: float, catalog=None) -> bytes:
    """取商品图字节：优先用 catalog 自己的取图实现（mcp 实现会带中台令牌）。"""
    fetcher = getattr(catalog, "fetch_image_bytes", None)
    if fetcher is not None:
        return await fetcher(image)
    if image.bytes_b64:
        try:
            return base64.b64decode(image.bytes_b64, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ImageError("IMG_UPSTREAM_INVALID", f"商品图 base64 解码失败：{exc}") from exc
    if not image.url:
        raise ImageError("IMG_UPSTREAM_INVALID", f"商品图缺少地址：{image.image_id or '-'}")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(image.url)
            response.raise_for_status()
            return response.content
    except Exception as exc:  # noqa: BLE001 —— 网络/超时/非 2xx 一律视为中台不可用
        raise ImageError("IMG_UPSTREAM_MCP_UNAVAILABLE",
                         f"下载商品图失败（{image.image_id or image.url}）：{type(exc).__name__}",
                         retryable=True) from exc


def _pick_images(images: list[ProductImage], color: str) -> list[ProductImage]:
    """优先取该颜色的图；没有颜色标注时退回全部图（再按主图优先排序）。"""
    same = [image for image in images if (image.color or "") == (color or "")]
    chosen = same or [image for image in images if not image.color] or list(images)
    return sorted(chosen, key=lambda item: (not item.is_main, item.image_id))


async def resolve_refs(
    catalog: PlatformCatalog,
    targets: list[RefTarget],
    *,
    session,
    username: str,
    department: str | None,
    refs_per_item: int = 2,
    max_refs: int = 6,
    timeout_seconds: float = 5.0,
) -> RefResolution:
    """批量解析：按「商品+颜色」取图并落库（成功时返回每个目标可用的 RefImage 列表）。"""
    if not targets:
        raise_image("IMG_REFERENCE_REQUIRED", "请至少选择一个商品")
    unique_products: dict[str, ProductData] = {}
    for target in targets:
        if target.product_id not in unique_products:
            unique_products[target.product_id] = await catalog.get_product(target.product_id)

    per_item = max(1, int(refs_per_item or 1))
    # 预算校验：颜色数 × 基础张数不能超过上限，否则参考图会被静默截断（身份锁定失效）
    if len(targets) > max_refs:
        raise_image("IMG_REFS_BUDGET",
                    f"参考图预算不足：{len(targets)} 个「商品+颜色」需要至少 {len(targets)} 张，"
                    f"上限 {max_refs} 张；请减少商品/颜色数量或调大 IMAGE_MAX_REFS")
    if len(targets) * per_item > max_refs:
        raise_image("IMG_REFS_BUDGET",
                    f"参考图预算不足：{len(targets)} 个目标 × 每目标 {per_item} 张 = "
                    f"{len(targets) * per_item} 张，上限 {max_refs} 张；"
                    "请减少颜色数量，或把 IMAGE_REFS_PER_ITEM 调到 1")

    result = RefResolution(products=unique_products)
    used = 0
    for target in targets:
        data = unique_products[target.product_id]
        ordered = _pick_images(data.images, target.color)
        picked = 0
        for image in ordered:
            if picked >= per_item or used >= max_refs:
                result.truncated += max(0, len(ordered) - picked)
                break
            if not image.has_payload:
                continue
            raw = await _fetch(image, timeout=timeout_seconds, catalog=catalog)
            asset = asset_store.save_bytes(
                session, raw, kind="ref", mime=image.mime or "image/png",
                username=username, department=department,
                product_id=target.product_id, color_key=target.color_key, source="mcp",
            )
            result.refs.setdefault(target.color_key, []).append(RefImage(
                asset_id=asset.asset_id, sha256=asset.sha256, mime=asset.mime,
                width=asset.width, height=asset.height, bytes=asset.bytes,
                source="mcp", product_id=target.product_id, image_id=image.image_id,
                color=target.color, color_hex=target.color_hex or image.color_hex,
            ))
            picked += 1
            used += 1
        if not result.refs.get(target.color_key):
            raise_image("IMG_REFERENCE_REQUIRED",
                        f"所选商品没有可用参考图：{target.product_id}"
                        f"{('（颜色 ' + target.color + '）') if target.color else ''}")
    return result


__all__ = ["RefTarget", "RefResolution", "resolve_refs"]
