"""生图 Agent 的 HTTP 路由（`/api/image/*`）。

v6 契约（重要）：
- 用户**只选商品**（`product_ids`，来自中台清单）+ 写需求；**没有上传接口**，
  参考图由网关经 `sources.refs.resolve_refs` 从所选商品取（kind=ref）；
- 一次请求 = 1 个 `batch_id` + N 个商品任务；
- 所有参数（构图 / 场景风格 / 张数默认 / 平台规则 / 商品数据）**只从中台取**，Agent 不兜底；
- 中台不可用/超时 → `IMG_UPSTREAM_MCP_UNAVAILABLE`(503)，**任务表零残留**（事务回滚）；
- 除 `/health` 外都需要登录；普通账号只能看自己的任务/图片。
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.agents_image import metrics
from app.agents_image.api.admin_routes import router as admin_router
from app.agents_image.api.auth_routes import router as auth_router
from app.agents_image.api.common import asset_payload, error_response
from app.agents_image.api.deps import require_user, trace_id_of
from app.agents_image.api.stream_routes import router as stream_router
from app.agents_image.config import get_image_settings
from app.agents_image.dispatch.task_dispatcher import get_dispatcher
from app.agents_image.ids import new_batch_id, new_task_id
from app.agents_image.nodes import plan_slots
from app.agents_image.nodes import validate as validate_node
from app.agents_image.providers.image_provider import providers
from app.agents_image.sources import refs as refs_source
from app.agents_image.sources.product_source import get_catalog, registered_catalogs
from app.agents_image.state import ImageTaskState, SlotPlan
from app.agents_image.store import assets as asset_store
from app.agents_image.store import tasks as task_store
from app.core.errors import ImageError
from app.db.engine import session_scope

logger = logging.getLogger("sku-agent.image.api")

router = APIRouter(prefix="/api/image", tags=["image"])
router.include_router(auth_router)
router.include_router(stream_router)
router.include_router(admin_router)


@router.get("/health")
def image_health() -> dict:
    settings = get_image_settings()
    return {
        "status": "ok" if settings.enabled else "disabled",
        "provider": settings.provider,
        "providers_registered": providers(),
        "product_source": settings.product_source,
        "product_sources_registered": registered_catalogs(),
        "storage": settings.storage,
        "dispatcher": settings.dispatcher,
        "concurrency": settings.concurrency,
        "provider_concurrency": settings.provider_concurrency,
        "provider_in_flight": _provider_in_flight(),
        "max_refs": settings.max_refs,
        "max_batch_products": settings.max_batch_products,
        "secret_is_default": settings.secret_is_default,
        "degraded": False,
        "degraded_reason": "",
        **metrics.snapshot(),
    }


def _provider_in_flight() -> int:
    """当前在途的第三方生图调用数（进程内视图；面板同时给 DB 侧 running 任务数）。"""
    try:
        from app.agents_image.generator import handler as generator_handler

        return generator_handler.provider_in_flight()
    except Exception:  # noqa: BLE001
        return 0


@router.get("/products")
async def list_products(request: Request) -> JSONResponse:
    """商品清单（转发 MCP `image_list_products`）：前端只从这里多选，不能上传图片。"""
    trace = trace_id_of(request)
    try:
        require_user(request)
        keyword = (request.query_params.get("keyword") or "").strip()
        try:
            limit = int(request.query_params.get("limit") or 20)
        except ValueError as exc:
            raise ImageError("IMG_INPUT_INVALID", "limit 必须是整数") from exc
        rows = await get_catalog().list_products(keyword=keyword, limit=max(1, min(limit, 100)))
        payload = {"products": [row.model_dump() for row in rows], "trace_id": trace}
    except ImageError as exc:
        return error_response(exc, trace)
    except Exception as exc:  # noqa: BLE001
        logger.exception("取商品清单失败")
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content=payload)


@router.get("/catalog")
async def image_catalog(request: Request) -> JSONResponse:
    """构图 / 场景风格 / 图位策略 / 平台规则（全部来自中台，前端只用这里给的值）。"""
    trace = trace_id_of(request)
    try:
        require_user(request)
        platform = (request.query_params.get("platform") or "淘系").strip()
        category = (request.query_params.get("category") or "").strip()
        catalog = get_catalog()
        compositions = await catalog.list_compositions(category=category, platform=platform)
        scene_styles = await catalog.list_scene_styles(category=category, platform=platform)
        policy = await catalog.get_plan_policy(platform=platform, category=category)
        rules = await catalog.get_platform_rules(platform=platform)
        payload = {
            "platform": platform,
            "category": category,
            "compositions": [item.model_dump() for item in compositions],
            "scene_styles": [item.model_dump() for item in scene_styles],
            "plan_policy": policy.model_dump(),
            "platform_rules": rules.model_dump(),
            "trace_id": trace,
        }
    except ImageError as exc:
        return error_response(exc, trace)
    except Exception as exc:  # noqa: BLE001
        logger.exception("取中台预设失败")
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content=payload)


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


async def _resolve_request_parameters(catalog, body: dict, settings) -> dict:
    """把请求参数对齐到中台：默认值、候选校验、labels 映射（全部来自 catalog）。

    M3 起**区分两个张数**：
    - `slot_count`：图位数（默认取中台 `plan_policy` 的 9 图位模板，可 1..9）；
    - `image_count`：真正出图的张数（只有 `type=generate` 的图位出图，默认=该平台 generate 图位数）。
    旧字段 `n` 兼容映射到 `image_count`。
    """
    platform = str(body.get("platform") or "淘系").strip()
    category = str(body.get("category") or "").strip()
    rules = await catalog.get_platform_rules(platform=platform)
    policy = await catalog.get_plan_policy(platform=platform, category=category)
    compositions = await catalog.list_compositions(category=category, platform=platform)
    scene_styles = await catalog.list_scene_styles(category=category, platform=platform)

    composition_ids = {item.preset_id for item in compositions}
    style_ids = {item.preset_id for item in scene_styles}
    chosen_compositions = _dedupe([str(x) for x in (body.get("compositions") or [])])
    chosen_styles = _dedupe([str(x) for x in (body.get("scene_styles") or [])])
    for item in chosen_compositions:
        if item not in composition_ids:
            raise ImageError("IMG_INPUT_INVALID", f"构图不在中台候选里：{item}")
    for item in chosen_styles:
        if item not in style_ids:
            raise ImageError("IMG_INPUT_INVALID", f"场景风格不在中台候选里：{item}")

    aspect = str(body.get("aspect") or rules.aspect or "1:1")
    max_slots = min(int(policy.max_count or 9), settings.max_total_images, 9)
    try:
        raw_n = body.get("n")
        # 兼容：老调用方只传 n（=图位数=出图张数），新调用方用 slot_count / image_count
        raw_slots = body.get("slot_count")
        slot_count = int(raw_slots if raw_slots is not None else
                         (raw_n if raw_n is not None else max_slots))
        raw_images = body.get("image_count")
        image_count = int(raw_images if raw_images is not None else
                          (raw_n if raw_n is not None else 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ImageError("IMG_INPUT_INVALID", "张数必须是整数") from exc
    slot_count = max(1, min(slot_count, max_slots))
    generate_slots = sum(1 for item in policy.slots
                         if int(item.slot) <= slot_count and (item.type or "generate") == "generate")
    generate_slots = max(1, generate_slots)
    if image_count <= 0:
        image_count = generate_slots              # 默认按该平台 generate 图位数量
    if image_count > generate_slots:
        raise ImageError("IMG_INPUT_INVALID",
                         f"出图张数 {image_count} 超过该平台可出图图位数 {generate_slots}"
                         "（尺码对照/包装清单/售后排版不走出图通道）")
    image_count = max(1, min(image_count, settings.max_total_images))

    return {
        "platform": platform,
        "category": category,
        "policy": policy,
        "rules": rules,
        "aspect": aspect,
        "slot_count": slot_count,
        "count": image_count,           # 兼容字段：v6 的 count 语义 = 出图张数
        "image_count": image_count,
        "compositions": chosen_compositions,
        "scene_styles": chosen_styles,
        "labels": {
            "compositions": {item.preset_id: item.label for item in compositions},
            "scene_styles": {item.preset_id: item.label for item in scene_styles},
        },
    }


@router.post("/tasks")
async def create_tasks(request: Request) -> JSONResponse:
    """批量建任务：`{request_id?, product_ids[1..5], prompt, compositions?, scene_styles?, slot_count?, image_count?, aspect?, platform?}`。

    幂等：`Idempotency-Key` 头或 body `request_id` 相同 → 直接返回首次响应（不重复建任务/扣额度）。
    配额：先在事务内**预留**本期额度，交付成功再结算、失败/取消回滚。
    """
    trace = trace_id_of(request)
    settings = get_image_settings()
    submitted: list[str] = []
    reserved_images = 0
    reserved_user = ""
    try:
        user = require_user(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise ImageError("IMG_INPUT_INVALID", "请求体必须是 JSON 对象")
        idempotency_key = (request.headers.get("Idempotency-Key")
                           or body.get("request_id") or "").strip()
        if idempotency_key:
            with session_scope() as session:
                from app.agents_image.store.models import ImageRequest

                row = session.get(ImageRequest, idempotency_key)
                replay = dict(row.response_json or {}) if row is not None else None
            if replay:
                replay.update({"idempotent": True, "trace_id": trace})
                return JSONResponse(status_code=200, content=replay)

        product_ids = _dedupe([str(x) for x in (body.get("product_ids") or [])])
        if not product_ids:
            raise ImageError("IMG_INPUT_INVALID", "请至少选择一个商品（product_ids 不能为空）")
        if len(product_ids) > settings.max_batch_products:
            raise ImageError("IMG_BATCH_TOO_LARGE",
                             f"一次最多选 {settings.max_batch_products} 个商品，收到 {len(product_ids)} 个")
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise ImageError("IMG_INPUT_INVALID", "需求描述不能为空")

        catalog = get_catalog()
        targets = await _resolve_targets(catalog, body, product_ids)
        platforms = _platform_list(body)
        params_by_platform = {
            platform: await _resolve_request_parameters(
                catalog, {**body, "platform": platform}, settings)
            for platform in platforms
        }
        variants = _variant_matrix(params_by_platform[platforms[0]]["policy"], body)
        matrix = [(target, platform, variant)
                  for target in targets for platform in platforms for variant in variants]
        if len(matrix) > settings.max_tasks_per_request:
            raise ImageError("IMG_BATCH_TOO_LARGE",
                             f"一次最多创建 {settings.max_tasks_per_request} 个任务，"
                             f"当前颜色×平台×变体共 {len(matrix)} 个")

        base_params = params_by_platform[platforms[0]]
        planned_images = base_params["image_count"] * len(matrix)
        with session_scope() as session:
            from app.agents_image.store import quota as quota_store

            quota = quota_store.check(session, user.username, planned_images,
                                     is_admin=user.is_admin)
        if not quota["allowed"]:
            raise ImageError("IMG_QUOTA_EXCEEDED",
                             f"配额不足：本期（{quota['period_type']}）剩余 {quota['remaining']} 张，"
                             f"本次需要 {planned_images} 张", retryable=False)

        # 预算闸（P1-8）：用配置单价做**事前估算**；真实计费以 provider 返回为准（P0-5）
        estimated_cost = round(float(settings.cost_per_image_cny) * planned_images, 4)
        if settings.cost_budget_cny and estimated_cost > float(settings.cost_budget_cny):
            raise ImageError("IMG_COST_BUDGET_EXCEEDED",
                             f"单次请求成本估算 ¥{estimated_cost} 超过预算上限 "
                             f"¥{settings.cost_budget_cny}（{planned_images} 张）；"
                             "请减少张数/颜色，或调大 IMAGE_COST_BUDGET_CNY", retryable=False)

        with session_scope() as session:
            from app.agents_image.store import quota as quota_store

            # 预留：占住本次计划张数，防止并发下超发（交付成功转 used，失败/取消释放）
            if not user.is_admin:
                if not quota_store.reserve(session, user.username, planned_images):
                    raise ImageError("IMG_QUOTA_EXCEEDED",
                                     f"配额不足：本期剩余 {quota['remaining']} 张，"
                                     f"本次需要 {planned_images} 张", retryable=False)
                reserved_images = planned_images
                reserved_user = user.username

            resolution = await refs_source.resolve_refs(
                catalog, targets, session=session,
                username=user.username, department=user.department,
                refs_per_item=settings.refs_per_item, max_refs=settings.max_refs,
                timeout_seconds=settings.mcp_timeout_seconds,
            )
            batch_id = new_batch_id()
            created: list[dict] = []
            first_slots: list[dict] = []
            for target, platform, variant in matrix:
                params = params_by_platform[platform]
                state = ImageTaskState(
                    prompt=prompt, aspect=params["aspect"], count=params["image_count"],
                    compositions=params["compositions"], scene_styles=params["scene_styles"],
                    platform=platform, variant_id=variant.get("id", ""),
                    variant_direction=variant.get("hint", "") or variant.get("label", ""),
                    refs=resolution.refs_of(target.color_key),
                )
                state.slots = [SlotPlan(**item) for item in plan_slots.build_slots(
                    state, policy=params["policy"], labels=params["labels"],
                    slot_count=params["slot_count"])]
                state.attrs = _attrs_for_target(
                    resolution.products[target.product_id].attrs, target)
                validate_node.validate(state, settings=settings)
                planned_for_target = sum(1 for item in state.slots
                                         if plan_slots.is_generatable(item))
                task = task_store.create_task(
                    session, task_id=new_task_id(), username=user.username,
                    department=user.department, batch_id=batch_id,
                    product_id=target.product_id, color_key=target.color_key,
                    variant_id=state.variant_id,
                    prompt=prompt, platform=platform, aspect=params["aspect"],
                    n=params["image_count"],
                    ref_asset_ids=resolution.asset_ids_of(target.color_key),
                    compositions=params["compositions"], scene_styles=params["scene_styles"],
                    slots=[item.model_dump() for item in state.slots],
                    prompt_pack=[], request_id=idempotency_key,
                    trace_id=trace, attrs=state.attrs,
                    image_count=min(planned_for_target, params["image_count"]),
                    slot_count=len(state.slots),
                )
                asset_store.bind_refs_to_task(session, task.ref_asset_ids, task.task_id)
                created.append({"task_id": task.task_id, "product_id": target.product_id,
                                "color": target.color, "color_key": target.color_key,
                                "platform": platform, "variant_id": state.variant_id,
                                "variant_direction": state.variant_direction,
                                "status": task.status,
                                "refs": len(task.ref_asset_ids),
                                "image_count": task.image_count,
                                "slot_count": task.slot_count})
                first_slots = first_slots or [item.model_dump() for item in state.slots]
            payload = {
                "batch_id": batch_id, "tasks": created, "slots": first_slots,
                "platforms": platforms,
                "variants": [item.get("id", "") for item in variants],
                "slot_count": base_params["slot_count"],
                "image_count": base_params["image_count"],
                "limits": {"max_refs": settings.max_refs,
                           "refs_per_item": settings.refs_per_item,
                           "max_batch_products": settings.max_batch_products,
                           "max_tasks_per_request": settings.max_tasks_per_request,
                           "refs_truncated": resolution.truncated},
                "planned_images": planned_images,
                "trace_id": trace, "idempotent": False,
            }
            if idempotency_key:
                from app.agents_image.store.models import ImageRequest

                session.add(ImageRequest(request_id=idempotency_key, batch_id=batch_id,
                                         username=user.username, response_json=payload))
                session.flush()
        for item in created:
            await get_dispatcher().submit(item["task_id"])
            submitted.append(item["task_id"])
        metrics.record_task_created(len(created))
    except ImageError as exc:
        if reserved_images and reserved_user:
            _release_reserve(reserved_user, reserved_images)
        return error_response(exc, trace)
    except Exception as exc:  # noqa: BLE001
        logger.exception("创建生图任务失败")
        if reserved_images and reserved_user:
            _release_reserve(reserved_user, reserved_images)
        return error_response(exc, trace)
    return JSONResponse(status_code=201, content=payload)


def _release_reserve(username: str, images: int) -> None:
    """建任务失败时把预留还回去（失败释放不影响用户额度）。"""
    try:
        from app.agents_image.store import quota as quota_store

        with session_scope() as session:
            quota_store.release(session, username, images)
    except Exception as exc:  # noqa: BLE001
        logger.warning("预留释放失败：user=%s n=%s %s", username, images, exc)


async def _resolve_targets(catalog, body: dict, product_ids: list[str]) -> list[refs_source.RefTarget]:
    """把「选中商品 + 颜色」展开成出图目标（M3：出图最小单位 = 商品 + 颜色）。

    请求侧三种写法：
    - `all_colors: true`：每个商品的全部颜色都出图；
    - `product_colors: [{product_id, colors: ["米色", "黑色"]}]`：指定颜色；
    - 都不传：每个商品只出「第一个颜色」（没有颜色维度时就是商品主图）。
    """
    requested: dict[str, list[str]] = {}
    for item in body.get("product_colors") or []:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("product_id") or "").strip()
        if not product_id:
            continue
        requested[product_id] = _dedupe([str(x) for x in (item.get("colors") or [])])
    all_colors = bool(body.get("all_colors"))

    targets: list[refs_source.RefTarget] = []
    for product_id in product_ids:
        data = await catalog.get_product(product_id)
        available = data.colors()
        names = {item["color"] for item in available if item["color"]}
        if product_id in requested and requested[product_id]:
            unknown = [name for name in requested[product_id] if name not in names]
            if unknown:
                raise ImageError("IMG_INPUT_INVALID",
                                 f"商品 {product_id} 没有这些颜色：{'、'.join(unknown)}"
                                 f"（可选：{'、'.join(sorted(names)) or '无颜色维度'}）")
            chosen = [item for item in available if item["color"] in requested[product_id]]
        elif all_colors:
            chosen = available
        else:
            chosen = available[:1]
        for item in chosen:
            targets.append(refs_source.RefTarget(
                product_id=product_id, color=item.get("color", ""),
                color_hex=item.get("color_hex", "")))
    if not targets:
        raise ImageError("IMG_INPUT_INVALID", "没有可出图的目标（商品没有颜色/参考图）")
    return targets


def _platform_list(body: dict) -> list[str]:
    """平台列表（M5）：单平台、多平台双套（platforms）或 platform_variant 开关。

    平台标识统一用 `淘系` / `抖系`；`通用` 只作为中台兜底规则，不作为出图平台。
    """
    explicit = [str(item).strip() for item in (body.get("platforms") or []) if str(item).strip()]
    single = str(body.get("platform") or "").strip()
    if explicit:
        chosen = _dedupe(explicit)
    elif single:
        chosen = [single]
    elif body.get("platform_variant"):
        chosen = ["淘系", "抖系"]
    else:
        chosen = ["淘系"]
    allowed = {"淘系", "抖系"}
    unknown = [item for item in chosen if item not in allowed]
    if unknown:
        raise ImageError(
            "IMG_INPUT_INVALID",
            "不支持的平台：" + "、".join(unknown) + "（可选：" + "、".join(sorted(allowed)) + "）")
    return chosen


def _variant_matrix(policy, body: dict) -> list[dict]:
    """mode=standard/variant/iterate → 变体列表（variant 输出 A/B 两套差异化方案）。"""
    mode = str(body.get("mode") or "standard").strip().lower()
    if mode not in {"standard", "variant", "iterate"}:
        raise ImageError(
            "IMG_INPUT_INVALID",
            "不支持的 mode：" + mode + "（可选 standard / variant / iterate）")
    if mode == "iterate":
        return [{"id": "", "label": "只改主图", "hint": "只重做主图，其余图位沿用历史成品"}]
    if mode != "variant":
        return [{"id": "", "label": "", "hint": ""}]
    directions = list(getattr(policy, "variant_directions", []) or [])
    if not directions:
        directions = [
            {"id": "A", "label": "版型优先", "hint": "主图突出版型与轮廓"},
            {"id": "B", "label": "面料优先", "hint": "主图突出面料质感与细节"},
        ]
    return [{"id": str(item.get("id") or ("V" + str(index + 1))),
             "label": str(item.get("label") or ""),
             "hint": str(item.get("hint") or "")}
            for index, item in enumerate(directions[:2])]


def _attrs_for_target(base_attrs: dict, target) -> dict:
    """按颜色覆盖身份锁定字段：颜色必须锁到具体 hex，避免多色商品串色。"""
    attrs = dict(base_attrs or {})
    if target.color:
        attrs["color_names"] = [target.color]
        if target.color_hex:
            attrs["color_hex"] = target.color_hex
    return attrs


def _registry():
    from app.agents_image.registry import get_registry

    return get_registry()


@router.get("/tasks")
def list_tasks(request: Request) -> JSONResponse:
    """我的任务（管理员可 `?all=true`）；可按 `batch_id` 过滤。"""
    trace = trace_id_of(request)
    try:
        user = require_user(request)
        include_all = user.is_admin and request.query_params.get("all") in {"1", "true", "yes"}
        batch_id = (request.query_params.get("batch_id") or "").strip()
        with session_scope() as session:
            rows = task_store.list_tasks(
                session, username=None if include_all else user.username,
                limit=50, batch_id=batch_id or None,
            )
            payload = [{"task_id": row.task_id, "batch_id": row.batch_id,
                        "product_id": row.product_id, "status": row.status,
                        "username": row.username, "prompt": row.prompt, "n": row.n,
                        "delivered_images": row.delivered_images,
                        "error_code": row.error_code,
                        "created_at": row.created_at.isoformat() if row.created_at else ""}
                       for row in rows]
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={"tasks": payload, "trace_id": trace})


@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request) -> JSONResponse:
    trace = trace_id_of(request)
    try:
        user = require_user(request)
        with session_scope() as session:
            task = task_store.get_task(session, task_id)
            if task is None:
                raise ImageError("IMG_TASK_NOT_FOUND", f"任务不存在：{task_id}")
            _ensure_owner(task.username, user, "任务")
            outputs = asset_store.list_outputs(session, task_id)
            refs = asset_store.get_assets(session, list(task.ref_asset_ids or []))
            payload = {
                "task_id": task.task_id, "batch_id": task.batch_id,
                "product_id": task.product_id, "ref_source": task.ref_source,
                "status": task.status, "username": task.username, "prompt": task.prompt,
                "prompt_final": task.prompt_final, "aspect": task.aspect, "n": task.n,
                "attempts": task.attempts,
                "attempted_images": task.attempted_images,
                "delivered_images": task.delivered_images,
                "cost_cny": task.cost_cny, "error_code": task.error_code,
                "error_detail": task.error_detail, "trace_id": task.trace_id,
                "backfill_status": task.backfill_status,
                "slots": list(task.slots_json or []),
                "refs": [asset_payload(asset) for asset in refs],
                "assets": [asset_payload(asset) for asset in outputs],
                "created_at": task.created_at.isoformat() if task.created_at else "",
                "finished_at": task.finished_at.isoformat() if task.finished_at else "",
            }
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content=payload)


def _ensure_owner(owner: str | None, user, what: str) -> None:
    """普通账号只能碰自己的数据；管理员不限（演示环境不区分部门）。"""
    if user.is_admin:
        return
    if (owner or "") != user.username:
        raise ImageError("IMG_FORBIDDEN", f"无权访问他人的{what}")


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, request: Request):
    trace = trace_id_of(request)
    try:
        user = require_user(request)
        with session_scope() as session:
            asset = asset_store.require_asset(session, asset_id)
            _ensure_owner(asset.username, user, "图片")
            data = asset_store.read_asset_bytes(asset)
            mime = asset.mime
    except ImageError as exc:
        return error_response(exc, trace)
    return Response(content=data, media_type=mime,
                    headers={"Cache-Control": "private, max-age=60"})


@router.get("/stats")
def stats(request: Request) -> JSONResponse:
    trace = trace_id_of(request)
    try:
        user = require_user(request)
    except ImageError as exc:
        return error_response(exc, trace)
    started = time.perf_counter()
    payload = metrics.snapshot()
    payload["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    with session_scope() as session:
        from sqlalchemy import func, select

        from app.agents_image.store.models import ImageTask

        statement = select(func.count()).select_from(ImageTask)
        if not user.is_admin:
            statement = statement.where(ImageTask.username == user.username)
        payload["tasks_total"] = session.execute(statement).scalar_one()
    return JSONResponse(status_code=200, content={**payload, "trace_id": trace})
