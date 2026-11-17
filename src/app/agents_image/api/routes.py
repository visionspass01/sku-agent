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
from app.agents_image.api.auth_routes import router as auth_router
from app.agents_image.api.common import asset_payload, error_response
from app.agents_image.api.deps import require_user, trace_id_of
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


@router.get("/health")
def image_health() -> dict:
    settings = get_image_settings()
    return {
        "status": "ok" if settings.enabled else "disabled",
        "provider": settings.provider,
        "providers_registered": providers(),
        "product_source": settings.product_source,
        "product_sources_registered": registered_catalogs(),
        "vision_provider": settings.vision_provider,
        "storage": settings.storage,
        "dispatcher": settings.dispatcher,
        "concurrency": settings.concurrency,
        "max_refs": settings.max_refs,
        "max_batch_products": settings.max_batch_products,
        "secret_is_default": settings.secret_is_default,
        "degraded": False,
        "degraded_reason": "",
        **metrics.snapshot(),
    }


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
    """把请求参数对齐到中台：默认值、候选校验、labels 映射（全部来自 catalog）。"""
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
    try:
        count = int(body.get("n") or policy.default_count or 1)
    except (TypeError, ValueError) as exc:
        raise ImageError("IMG_INPUT_INVALID", "张数必须是整数") from exc
    count = max(1, min(count, min(int(policy.max_count or 9), settings.max_total_images)))

    return {
        "platform": platform,
        "category": category,
        "policy": policy,
        "rules": rules,
        "aspect": aspect,
        "count": count,
        "compositions": chosen_compositions,
        "scene_styles": chosen_styles,
        "labels": {
            "compositions": {item.preset_id: item.label for item in compositions},
            "scene_styles": {item.preset_id: item.label for item in scene_styles},
        },
    }


@router.post("/tasks")
async def create_tasks(request: Request) -> JSONResponse:
    """批量建任务：`{request_id?, product_ids[1..5], prompt, compositions?, scene_styles?, n?, aspect?, platform?}`."""
    trace = trace_id_of(request)
    settings = get_image_settings()
    submitted: list[str] = []
    try:
        user = require_user(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise ImageError("IMG_INPUT_INVALID", "请求体必须是 JSON 对象")
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
        params = await _resolve_request_parameters(catalog, body, settings)

        planned_images = sum(1 for item in plan_slots.build_slots(
            ImageTaskState(prompt=prompt, aspect=params["aspect"], count=params["count"],
                           compositions=params["compositions"],
                           scene_styles=params["scene_styles"]),
            policy=params["policy"], labels=params["labels"])
            if item["type"] == "generate") * len(product_ids)
        with session_scope() as session:
            from app.agents_image.store import quota as quota_store

            quota = quota_store.check(session, user.username, planned_images,
                                     is_admin=user.is_admin)
        if not quota["allowed"]:
            raise ImageError("IMG_QUOTA_EXCEEDED",
                             f"配额不足：本期（{quota['period_type']}）剩余 {quota['remaining']} 张，"
                             f"本次需要 {planned_images} 张", retryable=False)

        with session_scope() as session:
            resolution = await refs_source.resolve_refs(
                catalog, product_ids, session=session,
                username=user.username, department=user.department,
                max_refs=settings.max_refs,
                timeout_seconds=settings.mcp_timeout_seconds,
            )
            batch_id = new_batch_id()
            created: list[dict] = []
            first_slots: list[dict] = []
            for product_id in product_ids:
                state = ImageTaskState(
                    prompt=prompt, aspect=params["aspect"], count=params["count"],
                    compositions=params["compositions"], scene_styles=params["scene_styles"],
                    refs=resolution.refs_of(product_id),
                )
                state.slots = [SlotPlan(**item) for item in plan_slots.build_slots(
                    state, policy=params["policy"], labels=params["labels"])]
                validate_node.validate(state, settings=settings)
                task = task_store.create_task(
                    session, task_id=new_task_id(), username=user.username,
                    department=user.department, batch_id=batch_id, product_id=product_id,
                    prompt=prompt, platform=params["platform"], aspect=params["aspect"],
                    n=params["count"], ref_asset_ids=resolution.asset_ids_of(product_id),
                    compositions=params["compositions"], scene_styles=params["scene_styles"],
                    slots=[item.model_dump() for item in state.slots],
                    prompt_pack=[], request_id=str(body.get("request_id") or ""),
                    trace_id=trace,
                    attrs=resolution.products[product_id].attrs,
                )
                asset_store.bind_refs_to_task(session, task.ref_asset_ids, task.task_id)
                created.append({"task_id": task.task_id, "product_id": product_id,
                                "status": task.status,
                                "refs": len(task.ref_asset_ids)})
                first_slots = first_slots or [item.model_dump() for item in state.slots]
        for item in created:
            await get_dispatcher().submit(item["task_id"])
            submitted.append(item["task_id"])
        metrics.record_task_created(len(created))
        payload = {
            "batch_id": batch_id, "tasks": created, "slots": first_slots,
            "limits": {"max_refs": settings.max_refs,
                       "max_batch_products": settings.max_batch_products,
                       "refs_truncated": resolution.truncated},
            "trace_id": trace,
        }
    except ImageError as exc:
        return error_response(exc, trace)
    except Exception as exc:  # noqa: BLE001
        logger.exception("创建生图任务失败")
        return error_response(exc, trace)
    return JSONResponse(status_code=201, content=payload)


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
            refs = asset_store.list_refs(session, task_id)
            payload = {
                "task_id": task.task_id, "batch_id": task.batch_id,
                "product_id": task.product_id, "ref_source": task.ref_source,
                "status": task.status, "username": task.username, "prompt": task.prompt,
                "prompt_final": task.prompt_final, "aspect": task.aspect, "n": task.n,
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
