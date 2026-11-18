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
    products: dict[str, ProductData] | None = None,
) -> RefResolution:
    """批量解析：按「商品+颜色」取图并落库（成功时返回每个目标可用的 RefImage 列表）。

    `products`：调用方已经批量取回的商品上下文（W2 合并请求）——给了就直接用，
    不再逐个商品回中台，避免"一次批量请求打 N 次 MCP"。
    """
    if not targets:
        raise_image("IMG_REFERENCE_REQUIRED", "请至少选择一个商品")
    unique_products: dict[str, ProductData] = {}
    for target in targets:
        if target.product_id in unique_products:
            continue
        if products and target.product_id in products:
            unique_products[target.product_id] = products[target.product_id]
            continue
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


# ---------------------------------------------------------------- W3：素材库 / 拍立淘
def _load_products(targets: list[RefTarget], products: dict[str, ProductData] | None) -> dict:
    out: dict[str, ProductData] = {}
    for target in targets:
        if target.product_id in out:
            continue
        if products and target.product_id in products:
            out[target.product_id] = products[target.product_id]
    return out


def _ref_of(asset, *, source: str, product_id: str, color: str = "",
            color_hex: str = "", image_id: str = "", image=None) -> RefImage:
    return RefImage(asset_id=asset.asset_id, sha256=asset.sha256, mime=asset.mime,
                    width=asset.width, height=asset.height, bytes=asset.bytes,
                    source=source, product_id=product_id, image_id=image_id,
                    color=color, color_hex=color_hex or getattr(image, "color_hex", ""))


async def resolve_library_refs(
    targets: list[RefTarget],
    *,
    session,
    username: str,
    asset_ids: list[str],
    max_refs: int = 6,
    is_admin: bool = False,
    products: dict[str, ProductData] | None = None,
) -> RefResolution:
    """素材库来源（W3）：用人工上传的素材库图片做参考图。

    - 素材库是**全局池 + 账号隔离**：普通账号只能用自己上传的图（管理员可用全部）；
    - 必须**显式勾选**（不给就报 `IMG_REFERENCE_REQUIRED`，不猜"用最新上传的"）；
    - 素材图**不参与颜色锁定**：颜色仍由中台 attrs 决定（避免用错色图当颜色依据）。
    """
    if not targets:
        raise_image("IMG_REFERENCE_REQUIRED", "请至少选择一个商品")
    ids = [str(item).strip() for item in asset_ids or [] if str(item).strip()]
    if not ids:
        raise_image("IMG_REFERENCE_REQUIRED",
                    "参考图来源为素材库，但没有勾选任何素材图；"
                    "请先在素材库上传/勾选（或把 ref_source 改回 mcp）")
    rows = asset_store.get_assets(session, ids)
    usable = [row for row in rows if row.kind == "library"]
    others = [row for row in usable if not is_admin and row.username != username]
    if others:
        raise_image("IMG_FORBIDDEN", "素材库图片属于其他账号，不能使用")
    if not usable:
        raise_image("IMG_REFERENCE_REQUIRED", "勾选的素材图不存在或已删除，请重新选择")
    usable = usable[: max(1, int(max_refs))]
    result = RefResolution(products=_load_products(targets, products))
    refs = [_ref_of(row, source="library", product_id=row.product_id or "")
            for row in usable]
    for target in targets:
        result.refs[target.color_key] = list(refs)
    return result


async def resolve_pailitao_refs(
    catalog: PlatformCatalog,
    targets: list[RefTarget],
    *,
    session,
    username: str,
    department: str | None,
    products: dict[str, ProductData] | None = None,
    query_asset_ids: list[str] | None = None,
    min_sales: int = 100,
    top_n: int = 5,
    max_refs: int = 6,
    timeout_seconds: float = 5.0,
) -> RefResolution:
    """拍立淘来源（W3）：拿图去中台搜相似款，只取销量 > `min_sales` 的前 `top_n` 个商品主图。

    - 搜图的输入图：优先用勾选的素材库图（`query_asset_ids`），否则用所选商品的第一个颜色主图；
    - 结果按销量降序，一个商品只取一张主图（因此最多 `top_n` 张、同时也是商品数上限）；
    - 没有符合条件的商品 → `IMG_SIMILAR_NOT_FOUND`「未搜到销量超100的近似商品」（直接终止，不降级）；
    - 拍立淘图**不参与颜色锁定**（身份锁定仍取中台 attrs）。
    """
    if not targets:
        raise_image("IMG_REFERENCE_REQUIRED", "请至少选择一个商品")
    unique = _load_products(targets, products)
    for target in targets:
        if target.product_id not in unique:
            unique[target.product_id] = await catalog.get_product(target.product_id)

    finder = getattr(catalog, "find_similar", None)
    if not callable(finder):
        raise_image("IMG_UPSTREAM_MCP_UNAVAILABLE",
                    "当前商品数据源不支持拍立淘搜图（需要中台 MCP 图像搜索能力）",
                    retryable=False)

    query_refs = asset_store.get_assets(session, list(query_asset_ids or []))
    query_bytes = asset_store.read_asset_bytes(query_refs[0]) if query_refs else b""

    result = RefResolution(products=unique)
    for product_id in unique:
        data = unique[product_id]
        raw = query_bytes
        if not raw:
            ordered = _pick_images(data.images, targets[0].color if targets else "")
            usable = [item for item in ordered if item.has_payload]
            if not usable:
                raise_image("IMG_REFERENCE_REQUIRED",
                            f"商品 {product_id} 没有可用于搜图的图片（也勾选了素材库图）")
            raw = await _fetch(usable[0], timeout=timeout_seconds, catalog=catalog)
        similar = await finder(raw, min_sales=int(min_sales), top_n=int(top_n))
        similar = [item for item in similar if int(item.sales) > int(min_sales)]
        similar.sort(key=lambda item: -int(item.sales))
        picked: list[RefImage] = []
        for item in similar:
            if len(picked) >= max(1, int(max_refs)):
                break
            if item.image is None or not item.image.has_payload:
                continue
            payload = await _fetch(item.image, timeout=timeout_seconds, catalog=catalog)
            asset = asset_store.save_bytes(
                session, payload, kind="ref", mime=item.image.mime or "image/png",
                username=username, department=department,
                product_id=product_id, source="pailitao",
            )
            picked.append(_ref_of(asset, source="pailitao", product_id=product_id,
                                  image_id=item.image.image_id, image=item.image))
        if not picked:
            raise_image("IMG_SIMILAR_NOT_FOUND",
                        f"未搜到销量超{int(min_sales)}的近似商品")
        for target in targets:
            if target.product_id == product_id:
                result.refs[target.color_key] = list(picked)
    return result


__all__ = ["RefTarget", "RefResolution", "resolve_refs", "resolve_library_refs",
           "resolve_pailitao_refs"]
