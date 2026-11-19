"""数据中台 image 模块 REST（`/api/v1/image/*`）：生图 Agent 的唯一参数与数据来源。

分工（与 §4.2 一致）：
- 读：商品清单 / 商品上下文（含**参考图 URL**）/ 构图 / 场景风格 / 图位策略 / 平台规则；
- 写：`POST /results` 回写结果元数据（**不含图片二进制**），`(task_id, slot)` 唯一 → 幂等；
- 图：`GET /assets/{image_id}.png` 返回商品图字节（mock 期由占位图生成，生产回源 OSS/COS）。

治理（认证/配额/审计）在中台中间件里统一做，本文件只声明 scope。
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import select

from data_platform import image_mock
from data_platform.api import image_schemas as isc
from data_platform.api.authz import require_scope, resolve_principal
from data_platform.config import department_of
from data_platform.db import ImageCatalog, ImageRequest, ImageResult, session_scope

router = APIRouter(prefix="/api/v1/image", tags=["image"])


def _catalog_rows(kind: str) -> list[dict]:
    """从 `image_catalog` 读启用的预设；表为空时退回文件内置（首次启动前也能用）。"""
    with session_scope() as session:
        rows = session.execute(
            select(ImageCatalog).where(ImageCatalog.kind == kind, ImageCatalog.enabled.is_(True))
        ).scalars().all()
        # 只留纯数据：session 关闭后 ORM 属性会过期（DetachedInstanceError）
        data = [dict(row.attrs_json or {}) | {"label": row.label, "desc": row.desc}
                for row in rows]
    if data:
        return data
    return [dict(item) for item in image_mock.PRESETS if item["kind"] == kind]


def _preset_out(item: dict) -> isc.PresetOut:
    return isc.PresetOut(
        preset_id=item.get("preset_id", ""), kind=item.get("kind", ""),
        code=item.get("code", ""), label=item.get("label", ""),
        desc=item.get("desc", ""), prompt_hint=item.get("prompt_hint", ""),
    )


@router.get("/products", response_model=isc.ProductsOut,
            dependencies=[Depends(require_scope("read"))])
def list_products(request: Request,
                  keyword: str = Query(default=""),
                  limit: int = Query(default=20, ge=1, le=100)):
    """商品清单（Agent 前端据此多选；用户只能从这里选，不能上传图片）。

    P1-8：这里过去**没有部门过滤**（context / similar 都有），部门账号能直接把别的部门的
    商品拉进清单。现在与另两个读接口同口径：越权行过滤掉，并在 `filtered` 与审计里说明。
    """
    rows = image_mock.list_products(keyword=keyword, limit=limit)
    principal = resolve_principal(request)
    out: list[isc.ProductSummaryOut] = []
    filtered = 0
    for row in rows:
        if principal is not None and not principal.department_allowed(
                department_of(row.get("category"))):
            filtered += 1
            continue
        out.append(isc.ProductSummaryOut(**row))
    request.state.audit_detail = (
        f"image.products n={len(out)} filtered={filtered} "
        f"dept={getattr(principal, 'departments', None)}")
    return isc.ProductsOut(products=out, count=len(out), filtered=filtered)


@router.get("/products/{product_id}/context", response_model=isc.ProductContextOut,
            dependencies=[Depends(require_scope("read"))])
def product_context(product_id: str, request: Request):
    data = image_mock.get_product(product_id)
    if data is None:
        request.state.audit_detail = f"image.context missing product={product_id}"
        raise HTTPException(status_code=404, detail=f"商品不存在：{product_id}")
    department = department_of(data.get("category"))
    if department:
        request.state.audit_detail = f"image.context dept={department}"
    else:
        request.state.audit_detail = "image.context dept=unknown"
    return isc.ProductContextOut(
        product_id=data["product_id"], title=data["title"], category=data["category"],
        attrs=data["attrs"],
        images=[isc.ProductImageOut(**item) for item in data["images"]],
        skus=[isc.ProductSkuOut(**item) for item in data.get("skus") or []],
        sku_ids=data["sku_ids"],
    )


@router.get("/products/context", response_model=isc.ProductContextListOut,
            dependencies=[Depends(require_scope("read"))])
def products_context(request: Request, ids: str = Query(default="")):
    """批量商品上下文（W2）：Agent 一次请求多个商品，避免 N 次往返。

    `ids` 用逗号分隔（也可重复传参）；不存在的 id 放进 `missing`，由 Agent 侧翻成 404。
    """
    wanted = [item.strip() for item in (ids or "").replace(" ", ",").split(",") if item.strip()]
    if not wanted:
        for value in request.query_params.getlist("ids"):
            wanted.extend([item.strip() for item in value.split(",") if item.strip()])
    seen: list[str] = []
    for item in wanted:
        if item not in seen:
            seen.append(item)
    principal = resolve_principal(request)
    products: list[isc.ProductContextOut] = []
    missing: list[str] = []
    forbidden: list[str] = []
    for product_id in seen:
        data = image_mock.get_product(product_id)
        if data is None:
            missing.append(product_id)
            continue
        if principal is not None and not principal.department_allowed(
                department_of(data.get("category"))):
            # 治理下沉（第三轮 P1-13）：部门作用域在中台兜底，不能只靠 Agent 自己挡
            forbidden.append(product_id)
            continue
        products.append(isc.ProductContextOut(
            product_id=data["product_id"], title=data["title"], category=data["category"],
            attrs=data["attrs"],
            images=[isc.ProductImageOut(**item) for item in data["images"]],
            skus=[isc.ProductSkuOut(**item) for item in data.get("skus") or []],
            sku_ids=data["sku_ids"],
        ))
    request.state.audit_detail = (
        f"image.products_context n={len(products)} missing={len(missing)} "
        f"forbidden={len(forbidden)} dept={getattr(principal, 'departments', None)}")
    return isc.ProductContextListOut(products=products, missing=missing,
                                     forbidden=forbidden, count=len(products))


@router.post("/similar-by-image", response_model=isc.SimilarOut,
             dependencies=[Depends(require_scope("read"))])
def similar_by_image(payload: isc.SimilarIn, request: Request):
    """拍立淘（W3）：以图搜相似款。只返回销量 > `min_sales` 的商品，按销量降序取前 N 个。"""
    items = image_mock.find_similar(payload.image_b64, min_sales=payload.min_sales,
                                    top_n=payload.top_n, exclude=payload.exclude)
    principal = resolve_principal(request)
    allowed: list[dict] = []
    filtered = 0
    for item in items:
        data = image_mock.get_product(str(item.get("product_id") or "")) or {}
        if principal is not None and not principal.department_allowed(
                department_of(data.get("category"))):
            filtered += 1
            continue
        allowed.append(item)
    out = [isc.SimilarItemOut(**item) for item in allowed]
    request.state.audit_detail = (
        f"image.similar n={len(out)} filtered={filtered} "
        f"min_sales={payload.min_sales} dept={getattr(principal, 'departments', None)}")
    return isc.SimilarOut(items=out, count=len(out), min_sales=payload.min_sales,
                          filtered=filtered)


async def binary_asset_endpoint(request: Request):
    """商品图二进制（Starlette 原生路由：不走响应模型，避免把二进制塞进 JSON 契约）。

    治理仍在中台中间件里做（认证/配额/审计），这里只补 scope 校验。
    `path_params["image_id"]` 已经含 `.png`（路由模板是 `{image_id}.png`）。
    """
    from data_platform.api.authz import resolve_principal

    principal = resolve_principal(request)
    if principal is None:
        return Response(status_code=401, content=b"")
    if not principal.has("read"):
        return Response(status_code=403, content=b"")
    image_id = request.path_params.get("image_id", "")
    raw = image_mock.image_bytes(image_id)
    if raw is None:
        return Response(status_code=404, content=b"")
    request.state.audit_detail = f"image.binary bytes={len(raw)}"
    return Response(content=raw, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/presets/compositions", response_model=isc.PresetsOut,
            dependencies=[Depends(require_scope("read"))])
def list_compositions(platform: str = Query(default="淘系"), category: str = Query(default="")):
    items = [_preset_out(item) for item in _catalog_rows("composition")]
    return isc.PresetsOut(kind="composition", platform=platform, category=category,
                          items=items, count=len(items))


@router.get("/presets/scene-styles", response_model=isc.PresetsOut,
            dependencies=[Depends(require_scope("read"))])
def list_scene_styles(platform: str = Query(default="淘系"), category: str = Query(default="")):
    items = [_preset_out(item) for item in _catalog_rows("scene_style")]
    return isc.PresetsOut(kind="scene_style", platform=platform, category=category,
                          items=items, count=len(items))


@router.get("/plan-policy", response_model=isc.PlanPolicyOut,
            dependencies=[Depends(require_scope("read"))])
def plan_policy(platform: str = Query(default="淘系"), category: str = Query(default="")):
    """9 图位框架 + 默认张数 + 默认构图/风格（可按平台/品类扩展，这里给同一份默认）。"""
    base = image_mock.PLAN_POLICY["default"]
    return isc.PlanPolicyOut(
        platform=platform, category=category,
        default_count=int(base["default_count"]), max_count=int(base["max_count"]),
        default_composition_id=base["default_composition_id"],
        default_scene_style_id=base["default_scene_style_id"],
        slots=[isc.SlotOut(**item) for item in image_mock.SLOTS],
    )


@router.get("/platform-rules/{platform}", response_model=isc.PlatformRulesOut,
            dependencies=[Depends(require_scope("read"))])
def platform_rules(platform: str):
    rules = image_mock.PLATFORM_RULES.get(platform) or image_mock.PLATFORM_RULES["通用"]
    return isc.PlatformRulesOut(**rules)


def _apply_result(row, payload: isc.ResultsIn, item, now: dt.datetime) -> None:
    """把一条回写结果写到 row 上（新建与覆盖共用，避免两套字段名漂移）。"""
    row.request_id = payload.request_id
    row.batch_id = payload.batch_id
    row.product_id = payload.product_id
    row.task_id = payload.task_id
    row.slot = item.slot
    row.role = item.role
    row.prompt = item.prompt
    row.negative_prompt = item.negative_prompt
    row.model = item.model
    row.params_json = payload.params or {}
    row.prompt_pack_json = list(payload.prompt_pack or [])
    row.sha256 = item.sha256
    row.mime = item.mime
    row.width = item.width
    row.height = item.height
    row.bytes = item.bytes
    row.storage = item.storage
    row.storage_key = item.storage_key
    row.critic_score = item.critic_score
    row.critic_issues = list(item.critic_issues or [])
    row.cost_cny = item.cost_cny
    row.elapsed_ms = item.elapsed_ms
    row.status = item.status
    row.copy_json = dict(item.copy_json or payload.copy_json or {})
    row.reused = 1 if item.reused else 0
    row.blocked_json = list(payload.blocked or [])
    row.warnings_json = list(payload.warnings or [])
    row.partial = 1 if payload.partial else 0
    row.expected_images = int(payload.expected_images or 0)
    row.variant_id = str(getattr(payload, "variant_id", "") or "")
    row.variant_direction = str(getattr(payload, "variant_direction", "") or "")
    row.ref_source = str(getattr(payload, "ref_source", "") or "mcp")
    row.trace_id = payload.trace_id
    row.created_at = now


@router.post("/results", response_model=isc.ResultsOut,
             dependencies=[Depends(require_scope("write"))])
def save_results(payload: isc.ResultsIn, request: Request):
    """回写结果元数据：`(task_id, slot)` **upsert**。

    P2-14：过去同 `(task_id, slot)` 一律跳过（算 deduped），于是"Agent 重试后重发的结果"
    会被静默丢掉，中台永远留着上一轮的旧图。现在分三种情况：
    - 不存在 → 新建（`saved`）；
    - 已存在且 `sha256` 相同 → `deduped`（真的重复）；
    - 已存在但内容变了 → 覆盖并 `updated`（重试后的新结果必须能落到中台）。
    """
    now = dt.datetime.now()
    saved = 0
    deduped = 0
    updated = 0
    with session_scope() as session:
        existing_request = session.get(ImageRequest, payload.request_id) \
            if payload.request_id else None
        if payload.request_id and existing_request is None:
            session.add(ImageRequest(
                request_id=payload.request_id, batch_id=payload.batch_id,
                product_ids=[payload.product_id] if payload.product_id else [],
                compositions=list(payload.compositions or []),
                scene_styles=list(payload.scene_styles or []),
                count=int(payload.count or 1), aspect=payload.aspect,
                platform=payload.platform, requester=payload.requester, created_at=now,
            ))
            session.flush()
        for item in payload.results:
            existing = session.execute(
                select(ImageResult).where(ImageResult.task_id == payload.task_id,
                                          ImageResult.slot == item.slot)
            ).scalars().first()
            if existing is not None:
                if (existing.sha256 or "") == (item.sha256 or ""):
                    deduped += 1
                    continue
                _apply_result(existing, payload, item, now)
                updated += 1
                continue
            row = ImageResult()
            _apply_result(row, payload, item, now)
            session.add(row)
            saved += 1
        session.flush()
    request.state.audit_detail = (f"image.results task={payload.task_id} "
                                  f"saved={saved} updated={updated} deduped={deduped}")
    return isc.ResultsOut(saved=saved, deduped=deduped, updated=updated,
                          request_id=payload.request_id, task_id=payload.task_id)


@router.get("/results", response_model=isc.ResultListOut,
            dependencies=[Depends(require_scope("read"))])
def list_results(product_id: str = Query(default=""),
                 limit: int = Query(default=20, ge=1, le=200)):
    statement = select(ImageResult).order_by(ImageResult.created_at.desc()).limit(limit)
    if product_id:
        statement = statement.where(ImageResult.product_id == product_id)
    with session_scope() as session:
        rows = session.execute(statement).scalars().all()
        # 在 session 内取值（会话结束后 ORM 属性过期）
        items = [isc.ResultRowOut(
            request_id=row.request_id, product_id=row.product_id, task_id=row.task_id,
            slot=row.slot, role=row.role, prompt=row.prompt, sha256=row.sha256,
            mime=row.mime, width=row.width, height=row.height, bytes=row.bytes,
            storage=row.storage, storage_key=row.storage_key, critic_score=row.critic_score,
            cost_cny=row.cost_cny, status=row.status,
            reused=bool(row.reused), copy_json=dict(row.copy_json or {}),
            variant_id=row.variant_id, ref_source=row.ref_source,
            partial=bool(row.partial), expected_images=int(row.expected_images or 0),
            blocked=list(row.blocked_json or []),
            warnings=list(row.warnings_json or []),
            created_at=row.created_at.isoformat() if row.created_at else "",
        ) for row in rows]
    return isc.ResultListOut(product_id=product_id, items=items, count=len(items))


__all__ = ["router"]
