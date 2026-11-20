"""把「选中商品 + 颜色」解析成参考图（第三轮 W4：改为**取字节 / 落库两阶段**）。

规则（M3，按颜色粒度）：
- 出图的最小业务单位是 **商品 + 颜色**：米色商品必须用米色的参考图，否则颜色会漂；
- 每个「商品+颜色」至少 1 张（该颜色主图），尽量取到 `IMAGE_REFS_PER_ITEM`（默认 2）；
- 预算不足（颜色数 × 2 > `IMAGE_MAX_REFS`）→ 明确报 `IMG_REFS_BUDGET`，不静默截断；
- 取不到字节 → `IMG_UPSTREAM_MCP_UNAVAILABLE`(503)。

三种来源（任务级 `ref_source`，互斥）：
- `mcp`：中台商品图（默认，颜色参与身份锁定）；
- `library`：人工上传的素材库（账号隔离 + 必须显式勾选，不参与颜色锁定）；
- `pailitao`：拍立淘相似款（销量 > 门槛的前 5 个商品主图，不参与颜色锁定）。

为什么两阶段（P1-5）：过去"下载参考图 + 拍立淘搜图"都发生在**建任务的数据库事务里**，
网络一慢就长时间占着写锁（真实冒烟出现过 `database is locked`）。现在拆成
`plan_*()`（只取字节，不写库）与 `persist_refs()`（只写库，不做网络），
事务只覆盖"预留 + 落库 + 建任务"。
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field

import httpx

from app.agents_image.config import get_image_settings
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
class PlannedRef:
    """一张待落库的参考图（已取到字节，尚未写库）。

    `asset_id` 非空表示"直接引用已有资产"（素材库来源不再复制一份，保持账号池语义）。
    """

    data: bytes
    mime: str = "image/png"
    source: str = "mcp"
    product_id: str = ""
    color_key: str = ""
    color: str = ""
    color_hex: str = ""
    image_id: str = ""
    asset_id: str = ""


@dataclass
class RefPlan:
    """一次批量解析的"计划"：每个目标要用的图（已下载），还没落库。"""

    refs: dict[str, list[PlannedRef]] = field(default_factory=dict)
    products: dict[str, ProductData] = field(default_factory=dict)
    truncated: int = 0
    warnings: list[str] = field(default_factory=list)

    def refs_of(self, color_key: str) -> list[PlannedRef]:
        return list(self.refs.get(color_key) or [])

    @property
    def total(self) -> int:
        return sum(len(items) for items in self.refs.values())


@dataclass
class RefResolution:
    """落库后的结果：refs 按 `color_key` 分组，供建任务时逐个目标取用。"""

    refs: dict[str, list[RefImage]] = field(default_factory=dict)
    products: dict[str, ProductData] = field(default_factory=dict)
    truncated: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(len(items) for items in self.refs.values())

    def refs_of(self, color_key: str) -> list[RefImage]:
        return list(self.refs.get(color_key) or [])

    def asset_ids_of(self, color_key: str) -> list[str]:
        return [item.asset_id for item in self.refs.get(color_key) or []]


async def _fetch(image, *, timeout: float, catalog=None, client=None) -> bytes:
    """取商品图字节：优先用 catalog 自己的取图实现（mcp 实现会带中台令牌）。

    `client`（P1-8，第十二轮）：批量下载时复用同一个 `httpx.AsyncClient`，
    避免每张图都新建连接池（握手/关闭开销 + 无连接复用）。
    """
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
        if client is not None:
            response = await client.get(image.url)
            response.raise_for_status()
            return response.content
        async with httpx.AsyncClient(timeout=timeout) as own_client:
            response = await own_client.get(image.url)
            response.raise_for_status()
            return response.content
    except Exception as exc:  # noqa: BLE001 —— 网络/超时/非 2xx 一律视为中台不可用
        raise ImageError("IMG_UPSTREAM_MCP_UNAVAILABLE",
                         f"下载商品图失败（{image.image_id or image.url}）：{type(exc).__name__}",
                         retryable=True) from exc


def _needs_http(image, catalog) -> bool:  # noqa: ANN001 —— ProductImage
    """这张图是否需要走 HTTP（P1-8）：catalog 自带取图实现 / 内嵌 base64 都不需要。

    `httpx.AsyncClient` 的构造本身就有成本（Windows 下实测 ~0.5s：SSL 上下文 + 代理探测），
    对"中台返回 base64"或"catalog 自己带令牌取图"的主流路径，建 client 纯属白等。
    """
    if callable(getattr(catalog, "fetch_image_bytes", None)):
        return False
    if getattr(image, "bytes_b64", ""):
        return False
    return bool(getattr(image, "url", ""))


async def _fetch_many(items: list[tuple[object, object]], *, timeout: float, catalog=None) -> list[bytes]:
    """并发下载一批商品图（P1-8）：限并发 + 复用 client + 整体预算，失败**快速失败**。

    上一版是逐张 `await`（最多 6 张 × 5s 超时 = 30s 串行，无整体预算），
    中台一抖动建任务就顶到调用方超时；同一批图之间其实完全独立。
    """
    if not items:
        return []
    settings = get_image_settings()
    concurrency = max(1, int(getattr(settings, "refs_download_concurrency", 4) or 4))
    budget = max(1.0, float(getattr(settings, "refs_download_budget_seconds", 20.0) or 20.0))
    gate = asyncio.Semaphore(concurrency)
    needs_http = any(_needs_http(image, catalog) for _target, image in items)

    async def _one(image, client):  # noqa: ANN001 —— ProductImage
        async with gate:
            return await _fetch(image, timeout=timeout, catalog=catalog, client=client)

    async def _run(client) -> list[bytes]:  # noqa: ANN001
        return await asyncio.wait_for(
            asyncio.gather(*[_one(image, client) for _target, image in items]), timeout=budget)

    try:
        if not needs_http:                       # 无需 HTTP：不建 client（省掉构造开销）
            return await _run(None)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await _run(client)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise ImageError("IMG_UPSTREAM_MCP_UNAVAILABLE",
                         f"参考图下载超时（{budget:g}s 预算内未完成 {len(items)} 张）",
                         retryable=True) from exc


def _pick_images(images: list[ProductImage], color: str) -> list[ProductImage]:
    """优先取该颜色的图；没有颜色标注时退回全部图（再按主图优先排序）。"""
    same = [image for image in images if (image.color or "") == (color or "")]
    chosen = same or [image for image in images if not image.color] or list(images)
    return sorted(chosen, key=lambda item: (not item.is_main, item.image_id))


def _color_mismatch_warning(product_id: str, target: RefTarget,
                            images: list[ProductImage],
                            chosen: list[ProductImage]) -> str:
    """目标颜色没有对应参考图时的告警文本（P1-2）。

    为什么必须报出来：`_resolve_targets` 的颜色来自 **SKU**，参考图颜色来自**图片标签**；
    两者不同步时 `_pick_images` 会退回"未标色的图/其它颜色的图"，而 Prompt 里的
    `color_hex` 仍按目标颜色锁定 —— 等于让模型"照着黑色参考图画米色"，颜色必漂。
    过去这条兜底完全静默，运营只会看到"图不对"却不知道原因。
    """
    untagged = [image for image in chosen if not (image.color or "").strip()]
    if untagged:
        used = "未标色的图"
    else:
        others = sorted({(image.color or "").strip() for image in chosen
                         if (image.color or "").strip()})
        used = f"其它颜色的图（{'、'.join(others[:5])}）"
    return (f"IMG_REF_COLOR_MISMATCH: 商品 {product_id} 的目标颜色「{target.color}」"
            f"没有对应参考图，已退回 {used}；颜色锁定可能失效（考虑在中台补齐该颜色图）")


def _load_products(targets: list[RefTarget], products: dict[str, ProductData] | None) -> dict:
    out: dict[str, ProductData] = {}
    for target in targets:
        if target.product_id in out:
            continue
        if products and target.product_id in products:
            out[target.product_id] = products[target.product_id]
    return out


# ---------------------------------------------------------------- 阶段一：取字节（无事务）
async def plan_refs(
    catalog: PlatformCatalog,
    targets: list[RefTarget],
    *,
    products: dict[str, ProductData] | None = None,
    refs_per_item: int = 2,
    max_refs: int = 6,
    timeout_seconds: float = 5.0,
) -> RefPlan:
    """`ref_source=mcp`：按「商品+颜色」取中台商品图（只下载，不落库）。"""
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

    plan = RefPlan(products=unique_products)
    # 1) 先只做"选哪几张图"（纯内存）：保持原有的预算/截断口径不变
    selected: list[tuple[RefTarget, list]] = []
    used = 0
    for target in targets:
        data = unique_products[target.product_id]
        ordered = _pick_images(data.images, target.color)
        if target.color and not any((image.color or "").strip() == target.color.strip()
                                    for image in data.images):
            # P1-2：颜色锁不住要可见，不能静默串色
            plan.warnings.append(_color_mismatch_warning(
                target.product_id, target, data.images, ordered))
        picked: list = []
        for image in ordered:
            if len(picked) >= per_item or used >= max_refs:
                plan.truncated += max(0, len(ordered) - len(picked))
                break
            if not image.has_payload:
                continue
            picked.append(image)
            used += 1
        if not picked:
            raise_image("IMG_REFERENCE_REQUIRED",
                        f"所选商品没有可用参考图：{target.product_id}"
                        f"{('（颜色 ' + target.color + '）') if target.color else ''}")
        selected.append((target, picked))
    # 2) 并发下载（P1-8）：限并发 + 复用 client + 整体预算
    pairs = [(target, image) for target, images_ in selected for image in images_]
    blobs = await _fetch_many(pairs, timeout=timeout_seconds, catalog=catalog)
    # 3) 按选中顺序组装（与串行版的结果顺序一致）
    cursor = 0
    for target, images_ in selected:
        for image in images_:
            raw = blobs[cursor]
            cursor += 1
            plan.refs.setdefault(target.color_key, []).append(PlannedRef(
                data=raw, mime=image.mime or "image/png", source="mcp",
                product_id=target.product_id, color_key=target.color_key,
                color=target.color, color_hex=target.color_hex or image.color_hex,
                image_id=image.image_id,
            ))
    return plan


async def plan_library_refs(
    targets: list[RefTarget],
    *,
    session,
    username: str,
    asset_ids: list[str],
    max_refs: int = 6,
    is_admin: bool = False,
    products: dict[str, ProductData] | None = None,
) -> RefPlan:
    """`ref_source=library`：用人工上传的素材库图片（只读库 + 读文件，不写库）。

    - 素材库是**全局池 + 账号隔离**：普通账号只能用自己上传的图（管理员可用全部）；
    - 必须**显式勾选**（不给就报 `IMG_REFERENCE_REQUIRED`，不猜"用最新上传的"）；
    - 素材图**不参与颜色锁定**：颜色仍由中台 attrs 决定。
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
    plan = RefPlan(products=_load_products(targets, products))
    refs: list[PlannedRef] = []
    for row in usable:
        data = asset_store.read_asset_bytes(row)
        refs.append(PlannedRef(data=data, mime=row.mime or "image/png", source="library",
                               product_id=row.product_id or "", image_id=row.asset_id,
                               asset_id=row.asset_id))
    for target in targets:
        plan.refs[target.color_key] = list(refs)
    return plan


def load_pailitao_query(session, *, username: str, is_admin: bool = False,
                        asset_ids: list[str] | None = None) -> bytes:
    """读取"拿去搜图"的素材图字节（同时做归属校验，P1-8）。

    过去这段完全没有归属校验：任何人只要拿到别人的素材 id 就能把它当作搜图输入。
    这里只读库/读文件，不做网络，保证调用方可以在事务外完成。
    """
    query_ids = [str(item).strip() for item in (asset_ids or []) if str(item).strip()]
    if not query_ids:
        return b""
    rows = [row for row in asset_store.get_assets(session, query_ids)
            if row.kind in {"library", "ref", "output"}]
    others = [row for row in rows if not is_admin and row.username != username]
    if others:
        raise_image("IMG_FORBIDDEN", "搜图用的素材图属于其他账号，不能使用")
    if not rows:
        raise_image("IMG_REFERENCE_REQUIRED", "搜图用的素材图不存在或已删除，请重新选择")
    return asset_store.read_asset_bytes(rows[0])


async def plan_pailitao_refs(
    catalog: PlatformCatalog,
    targets: list[RefTarget],
    *,
    products: dict[str, ProductData] | None = None,
    query_bytes: bytes = b"",
    min_sales: int = 100,
    top_n: int = 5,
    max_refs: int = 6,
    timeout_seconds: float = 5.0,
) -> RefPlan:
    """`ref_source=pailitao`：拿图去中台搜相似款，只取销量 > 门槛的前 N 个商品主图（只下载）。

    - 搜图输入：`query_bytes`（素材库图，已由 `load_pailitao_query` 校验归属），否则用
      **该商品自己**的颜色主图；
    - 每个商品单独选颜色（P1-7：过去对所有商品都用第一个目标的颜色，多商品必选错）；
    - 结果按销量降序，一个商品只取一张主图（最多 `top_n` 张）；
    - 没有符合条件的商品 → `IMG_SIMILAR_NOT_FOUND`「未搜到销量超100的近似商品」（直接终止）。
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

    def _rank(items) -> list:
        picked = [item for item in items if int(item.sales) > int(min_sales)]
        picked.sort(key=lambda item: -int(item.sales))
        return picked

    # P2-8：勾了素材库图（query_bytes 非空）时，所有商品用的是**同一张**搜图输入 ——
    # 搜一次就够；过去每个商品各搜一次（N 次搜索 + N 行重复资产）。
    shared_similar = None
    if query_bytes:
        shared_similar = _rank(await finder(query_bytes, min_sales=int(min_sales),
                                            top_n=int(top_n)))
        if not shared_similar:
            raise_image("IMG_SIMILAR_NOT_FOUND", f"未搜到销量超{int(min_sales)}的近似商品")

    plan = RefPlan(products=unique)
    payload_cache: dict[str, bytes] = {}     # 同一张相似款主图只下载一次
    # P1-8（第十二轮）：整个拍立淘阶段复用同一个 client（与 mcp 来源同款处理）
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        for product_id in unique:
            data = unique[product_id]
            if shared_similar is not None:
                similar = shared_similar
            else:
                # 用**该商品自己**的目标颜色选图（P1-7）
                color = next((target.color for target in targets
                              if target.product_id == product_id), "")
                ordered = _pick_images(data.images, color)
                usable = [item for item in ordered if item.has_payload]
                if not usable:
                    raise_image("IMG_REFERENCE_REQUIRED",
                                f"商品 {product_id} 没有可用于搜图的图片（也没勾选素材库图）")
                raw = await _fetch(usable[0], timeout=timeout_seconds, catalog=catalog,
                                   client=client)
                similar = _rank(await finder(raw, min_sales=int(min_sales), top_n=int(top_n)))
            picked: list[PlannedRef] = []
            for item in similar:
                if len(picked) >= max(1, int(max_refs)):
                    break
                if item.image is None or not item.image.has_payload:
                    continue
                image_key = str(getattr(item.image, "image_id", "")
                                or getattr(item.image, "sha256", "") or "")
                payload = payload_cache.get(image_key)
                if payload is None:
                    payload = await _fetch(item.image, timeout=timeout_seconds,
                                           catalog=catalog, client=client)
                    if image_key:
                        payload_cache[image_key] = payload
                picked.append(PlannedRef(data=payload, mime=item.image.mime or "image/png",
                                         source="pailitao", product_id=product_id,
                                         color_hex=item.image.color_hex,
                                         image_id=item.image.image_id))
            if not picked:
                raise_image("IMG_SIMILAR_NOT_FOUND",
                            f"未搜到销量超{int(min_sales)}的近似商品")
            for target in targets:
                if target.product_id == product_id:
                    plan.refs[target.color_key] = list(picked)
    return plan


# ---------------------------------------------------------------- 阶段二：落库（只写库）
def persist_refs(session, plan: RefPlan, *, username: str,
                 department: str | None = None) -> RefResolution:
    """把计划里的图写成 `kind=ref` 资产（同账号同图复用一行），组装成 `RefResolution`。

    素材库来源（`asset_id` 已存在）直接引用那一行，不复制——素材库是账号池，
    复制会让"删素材图"和"任务引用"两套记录打架。
    """
    resolution = RefResolution(products=dict(plan.products or {}), truncated=plan.truncated,
                               warnings=list(plan.warnings or []))
    for color_key, items in (plan.refs or {}).items():
        out: list[RefImage] = []
        for planned in items:
            if planned.asset_id:
                row = asset_store.get_asset(session, planned.asset_id)
                if row is not None:
                    out.append(RefImage(
                        asset_id=row.asset_id, sha256=row.sha256, mime=row.mime,
                        width=row.width, height=row.height, bytes=row.bytes,
                        source=planned.source, product_id=row.product_id or "",
                        image_id=planned.image_id, color_hex=planned.color_hex,
                    ))
                    continue
            asset = asset_store.save_bytes(
                session, planned.data, kind="ref", mime=planned.mime or "image/png",
                username=username, department=department,
                product_id=planned.product_id or None, color_key=color_key,
                source=planned.source,
            )
            out.append(RefImage(
                asset_id=asset.asset_id, sha256=asset.sha256, mime=asset.mime,
                width=asset.width, height=asset.height, bytes=asset.bytes,
                source=planned.source, product_id=planned.product_id,
                image_id=planned.image_id, color=planned.color,
                color_hex=planned.color_hex,
            ))
        resolution.refs[color_key] = out
    return resolution


# ---------------------------------------------------------------- 兼容封装（旧签名）
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
    """`ref_source=mcp` 的组合封装（取字节 + 落库），保留旧签名给已有调用/测试。"""
    plan = await plan_refs(catalog, targets, products=products, refs_per_item=refs_per_item,
                           max_refs=max_refs, timeout_seconds=timeout_seconds)
    return persist_refs(session, plan, username=username, department=department)


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
    """`ref_source=library` 的组合封装。"""
    plan = await plan_library_refs(targets, session=session, username=username,
                                   asset_ids=asset_ids, max_refs=max_refs,
                                   is_admin=is_admin, products=products)
    return persist_refs(session, plan, username=username)


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
    is_admin: bool = False,
) -> RefResolution:
    """`ref_source=pailitao` 的组合封装（读查询图 + 搜图下载 + 落库）。"""
    query_bytes = load_pailitao_query(session, username=username, is_admin=is_admin,
                                      asset_ids=query_asset_ids)
    plan = await plan_pailitao_refs(
        catalog, targets, products=products, query_bytes=query_bytes,
        min_sales=min_sales, top_n=top_n, max_refs=max_refs,
        timeout_seconds=timeout_seconds)
    return persist_refs(session, plan, username=username, department=department)


__all__ = ["RefTarget", "PlannedRef", "RefPlan", "RefResolution", "plan_refs",
           "plan_library_refs", "plan_pailitao_refs", "load_pailitao_query",
           "persist_refs", "resolve_refs", "resolve_library_refs",
           "resolve_pailitao_refs"]
