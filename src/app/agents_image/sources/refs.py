"""把「选中商品」解析成参考图：经 catalog 取商品图 → 下载/解码 → 落本地 refs 资产。

规则（v6）：
- 参考图**只能来自中台商品图**，用户不可上传；
- 每商品至少 1 张（主图优先），整任务合计 ≤ `IMAGE_MAX_REFS`；
- 取不到字节（网络/坏图）→ `IMG_UPSTREAM_MCP_UNAVAILABLE`(503)；
- 解析后某商品仍 0 张 → `IMG_REFERENCE_REQUIRED`(422)；
- 落库 `kind="ref"`、`source="mcp"`、带 `product_id`，同账号重复取同图复用资产行。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

import httpx

from app.agents_image.sources.product_source import PlatformCatalog, ProductData
from app.agents_image.state import RefImage
from app.agents_image.store import assets as asset_store
from app.core.errors import ImageError, raise_image


@dataclass
class RefResolution:
    """一次批量解析的结果：refs 按商品分组，供建任务时逐个商品取用。"""

    refs: dict[str, list[RefImage]] = field(default_factory=dict)
    products: dict[str, ProductData] = field(default_factory=dict)
    truncated: int = 0

    @property
    def total(self) -> int:
        return sum(len(items) for items in self.refs.values())

    def refs_of(self, product_id: str) -> list[RefImage]:
        return list(self.refs.get(product_id) or [])

    def asset_ids_of(self, product_id: str) -> list[str]:
        return [item.asset_id for item in self.refs.get(product_id) or []]


def _budget_per_product(total: int, count: int, max_refs: int) -> int:
    """平均分额度，至少 1 张/商品（`max_refs >= count` 由调用方校验）。"""
    if count <= 0:
        return max_refs
    return max(1, max_refs // count)


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


async def resolve_refs(
    catalog: PlatformCatalog,
    product_ids: list[str],
    *,
    session,
    username: str,
    department: str | None,
    max_refs: int,
    timeout_seconds: float = 5.0,
) -> RefResolution:
    """批量解析：按商品取图并落库（成功时返回每个商品可用的 RefImage 列表）。"""
    products: dict[str, ProductData] = {}
    for product_id in product_ids:
        products[product_id] = await catalog.get_product(product_id)

    budget = _budget_per_product(len(product_ids), len(products), max_refs)
    result = RefResolution(products=products)
    used = 0
    for product_id, data in products.items():
        ordered = sorted(data.images, key=lambda item: (not item.is_main, item.image_id))
        picked = 0
        for image in ordered:
            if picked >= budget or used >= max_refs:
                result.truncated += max(0, len(ordered) - picked)
                break
            if not image.has_payload:
                continue
            raw = await _fetch(image, timeout=timeout_seconds, catalog=catalog)
            asset = asset_store.save_bytes(
                session, raw, kind="ref", mime=image.mime or "image/png",
                username=username, department=department,
                product_id=product_id, source="mcp",
            )
            result.refs.setdefault(product_id, []).append(RefImage(
                asset_id=asset.asset_id, sha256=asset.sha256, mime=asset.mime,
                width=asset.width, height=asset.height, bytes=asset.bytes,
                source="mcp", product_id=product_id, image_id=image.image_id,
            ))
            picked += 1
            used += 1
        if not result.refs.get(product_id):
            raise_image("IMG_REFERENCE_REQUIRED",
                        f"所选商品没有可用参考图：{product_id}（{data.title or '-'}）")
    return result


__all__ = ["RefResolution", "resolve_refs"]
