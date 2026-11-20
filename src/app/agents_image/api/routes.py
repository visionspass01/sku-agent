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

import asyncio
import logging
import secrets
import time
from types import SimpleNamespace

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.agents_image import metrics
from app.agents_image.api.admin_routes import router as admin_router
from app.agents_image.api.auth_routes import router as auth_router
from app.agents_image.api.batch_routes import retry_one as _retry_one
from app.agents_image.api.batch_routes import router as batch_router
from app.agents_image.api.common import (
    DEPARTMENTS,
    asset_payload,
    error_response,
    is_terminal,
    verify_asset_signature,
)
from app.agents_image.api.deps import require_user, trace_id_of
from app.agents_image.api.library_routes import router as library_router
from app.agents_image.api.ratelimit import check_rate_limit
from app.agents_image.api.stream_routes import router as stream_router
from app.agents_image.config import get_image_settings
from app.agents_image.dispatch.task_dispatcher import get_dispatcher
from app.agents_image.ids import new_batch_id, new_task_id
from app.agents_image.nodes import plan_slots
from app.agents_image.nodes import validate as validate_node
from app.agents_image.providers.image_provider import effective_max_refs, providers
from app.agents_image.sources import batch_context
from app.agents_image.sources import refs as refs_source
from app.agents_image.sources.product_source import get_catalog, registered_catalogs
from app.agents_image.state import ImageTaskState, SlotPlan
from app.agents_image.store import assets as asset_store
from app.agents_image.store import tasks as task_store
from app.core.errors import ImageError
from app.db.engine import session_scope

logger = logging.getLogger("sku-agent.image.api")

# 参考图三源（W3）：任务级互斥，默认走中台商品图
REF_SOURCES = ("mcp", "library", "pailitao")

router = APIRouter(prefix="/api/image", tags=["image"])
router.include_router(auth_router)
router.include_router(stream_router)
router.include_router(admin_router)
router.include_router(library_router)
router.include_router(batch_router)


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
        # P1-7：真正生效的参考图上限（配置上限与 provider 能力取小），创建期按它校验
        "max_refs_effective": effective_max_refs(settings),
        "max_prompt_chars": int(getattr(settings, "max_prompt_chars", 0) or 0),
        # P0-2：预算闸是否真的生效（单价或预算为 0 时它等于不存在）
        "cost_price_cny": float(getattr(settings, "cost_per_image_cny", 0.0) or 0.0),
        "cost_budget_cny": float(getattr(settings, "cost_budget_cny", 0.0) or 0.0),
        "cost_budget_enabled": bool(
            float(getattr(settings, "cost_per_image_cny", 0.0) or 0.0) > 0
            and float(getattr(settings, "cost_budget_cny", 0.0) or 0.0) > 0),
        "max_batch_products": settings.max_batch_products,
        "secret_is_default": settings.secret_is_default,
        "context_batches": _context_batches(),
        # P1-9：召回缓存后端（redis / fakeredis / memory）与登录限流作用域（进程内）
        "cache_backend": _cache_backend(),
        "throttle_scope": "instance",
        **_department_health(),
        **_degraded_state(),
        **metrics.snapshot(),
    }


_DEPT_HEALTH_CACHE: dict[str, float] = {}
_DEPT_HEALTH_TTL = 60.0


def reset_department_health_cache() -> None:
    _DEPT_HEALTH_CACHE.clear()


def _department_health() -> dict:
    """P0-5：有多少普通账号没绑定部门（这些账号会被 403，运维要提前看到）。

    第六轮 P1-1：**只给计数，不给账号名单**。`/health` 是匿名可读的，
    上一版把前 10 个账号名挂在这里等于公开账号枚举面；
    具体是哪些账号请走管理员接口 `GET /admin/users`。

    P2-3（第九轮）：结果缓存 60s。`/health` 被 k8s/负载均衡高频探测，
    每次探测都查账号表会给 DB 制造无意义的压力。
    """
    now = time.monotonic()
    cached = _DEPT_HEALTH_CACHE.get("value_at")
    if cached is not None and now - cached < _DEPT_HEALTH_TTL:
        return dict(_DEPT_HEALTH_CACHE.get("value") or {})
    try:
        from app.agents_image.store import users as user_store

        with session_scope() as session:
            rows = user_store.list_users(session)
    except Exception:  # noqa: BLE001 —— 账号表读失败不影响 health 其它字段
        return {}
    missing = [row for row in rows
               if str(row.get("role") or "") != "admin" and not row.get("department")]
    payload = {"users_without_department": len(missing)}
    _DEPT_HEALTH_CACHE["value"] = payload
    _DEPT_HEALTH_CACHE["value_at"] = now
    return dict(payload)


@router.get("/metrics", include_in_schema=False)
def image_metrics_endpoint(request: Request):
    """Prometheus text format（P1-7 / 简历 4.5）。

    鉴权策略（决策 D5）：默认公开只读数值；配置了 `IMAGE_METRICS_TOKEN` 就要求 Bearer，
    方便部署时用 nginx / Prometheus 的 basic auth 之外再加一层。
    """
    settings = get_image_settings()
    token = str(getattr(settings, "metrics_token", "") or "").strip()
    if token:
        auth = request.headers.get("authorization", "")
        provided = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not secrets.compare_digest(provided, token):
            return Response(status_code=401, content=b"unauthorized\n",
                            media_type="text/plain; charset=utf-8")
    from app.agents_image import observability

    return Response(content=observability.render_metrics(),
                    media_type=observability._CONTENT_TYPE)


def _degraded_state() -> dict:
    """库结构降级（补列/唯一索引失败）要能被 health 与面板看到，而不是只在日志里。"""
    try:
        from app.agents_image.store.schema import schema_status

        status = schema_status()
    except Exception:  # noqa: BLE001
        status = {"degraded": False, "issues": []}
    return {"degraded": bool(status.get("degraded")),
            "degraded_reason": "；".join(status.get("issues") or [])[:300]}


def _cache_backend() -> str:
    """召回缓存后端（P1-9）：redis / fakeredis / memory（缓存层不可用）。"""
    try:
        from app.agents_image.rag import index as rag_index

        return rag_index.cache_backend()
    except Exception:  # noqa: BLE001
        return "unknown"


def _context_batches() -> dict:
    """商品上下文合并统计（W2）：真实发起的合并请求数 / 缓存命中。"""
    try:
        from app.agents_image.sources.product_source import get_catalog

        return batch_context.get_product_batcher(get_catalog()).stats()
    except Exception:  # noqa: BLE001
        return {}


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
        user = require_user(request)
        keyword = (request.query_params.get("keyword") or "").strip()
        try:
            limit = int(request.query_params.get("limit") or 20)
        except ValueError as exc:
            raise ImageError("IMG_INPUT_INVALID", "limit 必须是整数") from exc
        catalog = get_catalog()
        rows = await catalog.list_products(keyword=keyword, limit=max(1, min(limit, 100)))
        # 颜色候选（第三轮 P1-11）：一次批量取商品上下文，顺便消掉"每个商品回一次中台"的 N+1
        products = await batch_context.get_product_batcher(catalog).get_many(
            [row.product_id for row in rows])
        # P0-5：普通账号必须先有部门（缺部门 403），再按部门过滤清单
        visible = []
        department_of_user = "" if user.is_admin else _require_department(user)
        for row in rows:
            data = products.get(row.product_id)
            if data is None:
                continue
            if not user.is_admin:
                department = _department_of(data.category)
                if department != department_of_user:
                    continue
            item = row.model_dump()
            item["colors"] = [{"color": entry.get("color", ""),
                               "color_hex": entry.get("color_hex", "")}
                              for entry in data.colors()]
            visible.append(item)
        payload = {"products": visible, "trace_id": trace}
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


def _request_fingerprint(body: dict) -> str:
    """请求体指纹（P1-3）：幂等键只重放"同样的请求"，参数变了必须 409。"""
    import hashlib
    import json

    payload = {key: value for key, value in (body or {}).items() if key not in {"trace_id"}}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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

    幂等（W1 改为**先抢占再落库**）：`Idempotency-Key` 头或 body `request_id` 相同 →
    已完成的请求重放首次响应；仍在处理中的返回 409 + `Retry-After`（不再并发双建）。
    配额：事务内**预留**实际要出的张数（复用图位不计），交付成功结算、失败/取消回滚。
    """
    trace = trace_id_of(request)
    settings = get_image_settings()
    try:
        user = require_user(request)
        # P1-6（第九轮）：请求级限流（配额按周期，挡不住短时间狂点建任务打爆中台）
        check_rate_limit(user.username, "create_tasks")
        # P0-2：真实 provider + 单价 0 → 预算闸（按张数估算）会永远不触发，等于没有护栏。
        # 这里在**创建期**直接拒绝，而不是等出完图才发现超预算。
        if str(settings.provider or "").strip().lower() not in {"", "fake"} \
                and float(getattr(settings, "cost_per_image_cny", 0.0) or 0.0) <= 0:
            raise ImageError(
                "IMG_COST_CONFIG_MISSING",
                "已启用真实生图 provider，但 IMAGE_COST_PER_IMAGE_CNY 未配置（<= 0）："
                "预算闸会失效；请先配置单张成本（元）再建任务", retryable=False)
        body = await request.json()
        if not isinstance(body, dict):
            raise ImageError("IMG_INPUT_INVALID", "请求体必须是 JSON 对象")
        idempotency_key = (request.headers.get("Idempotency-Key")
                           or body.get("request_id") or "").strip()
        # P1-3（第九轮）：幂等键必须绑定请求体。上一版只看 key：同一个 request_id
        # 换参数重发会静默重放**旧响应**（用户以为新参数生效，实际什么都没发生）。
        payload_hash = _request_fingerprint(body)
        if idempotency_key:
            with session_scope() as session:
                from app.agents_image.store.models import ImageRequest

                row = session.get(ImageRequest, idempotency_key)
                replay = dict(row.response_json or {}) if row is not None else None
                stored_hash = str(getattr(row, "payload_hash", "") or "") if row is not None else ""
            if row is not None and stored_hash and stored_hash != payload_hash:
                raise ImageError(
                    "IMG_TASK_CONFLICT",
                    "同一个 request_id / Idempotency-Key 携带了不同参数；"
                    "请换一个新的 request_id（幂等键只用于重放同样的请求）",
                    retryable=False)
            if replay:
                replay.update({"idempotent": True, "trace_id": trace})
                return JSONResponse(status_code=200, content=replay)
            if row is not None:
                # 同 key 的首次请求还在处理（已抢占、尚未提交响应）
                return _idempotent_conflict(trace)

        product_ids = _dedupe([str(x) for x in (body.get("product_ids") or [])])
        if not product_ids:
            raise ImageError("IMG_INPUT_INVALID", "请至少选择一个商品（product_ids 不能为空）")
        if len(product_ids) > settings.max_batch_products:
            raise ImageError("IMG_BATCH_TOO_LARGE",
                             f"一次最多选 {settings.max_batch_products} 个商品，收到 {len(product_ids)} 个")
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise ImageError("IMG_INPUT_INVALID", "需求描述不能为空")
        # P2-6：需求描述长度上限（超长截断并告警，不静默丢）
        max_prompt_chars = int(getattr(settings, "max_prompt_chars", 0) or 0)
        prompt_warnings: list[str] = []
        if max_prompt_chars and len(prompt) > max_prompt_chars:
            prompt_warnings.append(
                f"IMG_PROMPT_TRUNCATED: 需求描述 {len(prompt)} 字超过上限 {max_prompt_chars} 字，"
                f"已截断到 {max_prompt_chars} 字")
            prompt = prompt[:max_prompt_chars]
        # W3：参考图三源（互斥）——mcp（中台商品图，默认）/ library（人工素材库）/ pailitao（拍立淘）
        ref_source = str(body.get("ref_source") or "mcp").strip().lower() or "mcp"
        if ref_source not in REF_SOURCES:
            raise ImageError("IMG_INPUT_INVALID",
                             f"不支持的参考图来源：{ref_source}"
                             f"（可选：{'、'.join(REF_SOURCES)}）")
        library_asset_ids = _dedupe([str(item) for item in (body.get("library_asset_ids") or [])])

        catalog = get_catalog()
        # P1-3：iterate 必须先读基座任务 —— 平台/变体/颜色都要跟基座对齐，
        # 否则 A/B 变体基座或抖系基座永远命中不了复用（key 是 (颜色, 平台, 变体) 三元组）
        base_task = None
        mode_hint = str(body.get("mode") or "standard").strip().lower()
        base_task_id_hint = str(body.get("base_task_id") or "").strip()
        if mode_hint == "iterate" and base_task_id_hint:
            with session_scope() as session:
                row = task_store.get_task(session, base_task_id_hint)
                if row is None:
                    raise ImageError("IMG_TASK_NOT_FOUND", f"基础任务不存在：{base_task_id_hint}")
                if not user.is_admin and row.username != user.username:
                    raise ImageError("IMG_FORBIDDEN", "无权复用他人的任务")
                # 会话关闭后 ORM 属性会失效（DetachedInstanceError）：先取成纯数据
                base_task = _base_snapshot(row)
            body = _inherit_from_base(body, base_task)
        # W2：商品上下文一次批量取回（窗口内合并 MCP 请求：3 秒内的多次取数合成一次调用）
        products = await batch_context.get_product_batcher(catalog).get_many(product_ids)
        targets = await _resolve_targets(catalog, body, product_ids, products=products)
        platforms = _platform_list(body)
        # P2-5：双平台参数解析并发（过去串行 2×4 次 MCP 调用，首屏明显变慢）
        resolved = await asyncio.gather(*(
            _resolve_request_parameters(catalog, {**body, "platform": platform}, settings)
            for platform in platforms))
        params_by_platform = dict(zip(platforms, resolved, strict=True))
        variants = _variant_matrix(
            params_by_platform[platforms[0]]["policy"], body,
            base_variant=str(getattr(base_task, "variant_id", "") or "") if base_task else "")
        matrix = [(target, platform, variant)
                  for target in targets for platform in platforms for variant in variants]
        if len(matrix) > settings.max_tasks_per_request:
            raise ImageError("IMG_BATCH_TOO_LARGE",
                             f"一次最多创建 {settings.max_tasks_per_request} 个任务，"
                             f"当前颜色×平台×变体共 {len(matrix)} 个")

        base_params = params_by_platform[platforms[0]]

        # 复用计划（只读、在建任务之前）：决定"实际要出多少张"，配额/成本按它算
        with session_scope() as session:
            reuse_plan = await _reuse_plan(session, user, body, targets)

        def _reuse_targets(target, platform: str, variant: dict) -> bool:
            """该目标是否与基础任务同 (颜色, 平台, 变体)：决定是否记录 base_task_id 血统。"""
            if not reuse_plan:
                return False
            key = (target.color_key, platform, str(variant.get("id", "") or ""))
            return key == reuse_plan["key"]

        def _reuse_of(target, platform: str, variant: dict) -> dict[str, str]:
            if not reuse_plan:
                return {}
            key = (target.color_key, platform, str(variant.get("id", "") or ""))
            return dict(reuse_plan["reuse_map"]) if key == reuse_plan["key"] else {}
        # 预演（W4）：每个任务实际要出的张数 = 目标图位数 - 可复用的图位数（iterate 差额补齐），
        # 配额/成本/结算都用同一个数，避免"预留 1 张、实际出 6 张"的账实不符。
        def _plan_of(target, platform: str, variant: dict) -> ImageTaskState:
            params = params_by_platform[platform]
            state = ImageTaskState(
                prompt=prompt, aspect=params["aspect"], count=params["image_count"],
                compositions=params["compositions"], scene_styles=params["scene_styles"],
                platform=platform, variant_id=variant.get("id", ""),
                variant_direction=variant.get("hint", "") or variant.get("label", ""),
                refs=[],
            )
            state.slots = [SlotPlan(**item) for item in plan_slots.build_slots(
                state, policy=params["policy"], labels=params["labels"],
                slot_count=params["slot_count"])]
            state.attrs = _attrs_for_target(products[target.product_id].attrs, target)
            # 参考图相关的校验要等解析完参考图再做（事务内），这里只预演图位与身份锁定
            return state

        planned_states = [
            (target, platform, variant, _plan_of(target, platform, variant))
            for target, platform, variant in matrix
        ]

        def _reuse_window(state: ImageTaskState, reuse_map: dict[str, str]) -> dict[str, str]:
            """复用只保留**本次图位窗口**内的 slot（第三轮 P1-2）。

            基础任务可能有 9 个图位，本次只请求 2 个；过去会把 3..9 的成品也挂过来，
            导致"slot_count=2 的任务里出现 slot 7 的图"。
            """
            if not reuse_map:
                return {}
            valid = {int(slot.slot) for slot in (state.slots or [])
                     if plan_slots.is_generatable(slot)}
            return {str(slot): asset_id for slot, asset_id in reuse_map.items()
                    if int(slot) in valid}

        def _gap_of(item) -> int:
            target, platform, variant, state = item
            params = params_by_platform[platform]
            reuse_map = _reuse_window(state, _reuse_of(target, platform, variant))
            generatable = [slot for slot in state.slots if plan_slots.is_generatable(slot)]
            if not reuse_map:
                return min(len(generatable), int(params["image_count"]))
            reused_here = [slot for slot in generatable
                           if int(slot.slot) in {int(key) for key in reuse_map}]
            gap = min(int(params["image_count"]),
                      max(0, len(generatable) - len(reused_here)))
            if gap <= 0:
                raise ImageError(
                    "IMG_INPUT_INVALID",
                    f"{target.color_key}（{platform}）的图位全部可复用，没有需要重做的图位；"
                    "请改用 standard 模式或增加图位数")
            return gap

        plan_by_task = [_gap_of(item) for item in planned_states]
        planned_images = sum(plan_by_task)
        with session_scope() as session:
            from app.agents_image.store import quota as quota_store

            quota = quota_store.check(session, user.username, planned_images,
                                     is_admin=user.is_admin)
        if not quota["allowed"]:
            raise ImageError("IMG_QUOTA_EXCEEDED",
                             f"配额不足：本期（{quota['period_type']}）剩余 {quota['remaining']} 张，"
                             f"本次需要 {planned_images} 张", retryable=False)

        # 部门闸（W2 前置）：必须在下载参考图之前判定，越权用户不能先把别的部门的图拉下来
        for product_id in product_ids:
            _ensure_department(products[product_id], user)

        # 预算闸（P1-8）：用配置单价做**事前估算**；真实计费以 provider 返回为准（P0-5）
        estimated_cost = round(float(settings.cost_per_image_cny) * planned_images, 4)
        if settings.cost_budget_cny and estimated_cost > float(settings.cost_budget_cny):
            raise ImageError("IMG_COST_BUDGET_EXCEEDED",
                             f"单次请求成本估算 ¥{estimated_cost} 超过预算上限 "
                             f"¥{settings.cost_budget_cny}（{planned_images} 张）；"
                             "请减少张数/颜色，或调大 IMAGE_COST_BUDGET_CNY", retryable=False)

        # 阶段 B：取参考图（第三轮 P1-5 —— **不在写事务里做网络 IO**）
        # 读库/读文件（素材库归属校验、拍立淘 query 图）在只读会话里完成，随后关闭；
        # 下载/搜图完全是网络阶段，事务只覆盖"预留 + 落库 + 建任务"。
        # P1-7：三个来源统一走「配置上限 ∩ provider 能力」的生效上限，超额在创建期报 IMG_REFS_BUDGET
        max_refs = effective_max_refs(settings)
        if ref_source == "library":
            with session_scope() as read_session:
                ref_plan = await refs_source.plan_library_refs(
                    targets, session=read_session, username=user.username,
                    asset_ids=library_asset_ids, max_refs=max_refs,
                    is_admin=user.is_admin, products=products)
        elif ref_source == "pailitao":
            with session_scope() as read_session:
                query_bytes = refs_source.load_pailitao_query(
                    read_session, username=user.username, is_admin=user.is_admin,
                    asset_ids=library_asset_ids)
            ref_plan = await refs_source.plan_pailitao_refs(
                catalog, targets, products=products, query_bytes=query_bytes,
                min_sales=settings.pailitao_min_sales, top_n=settings.pailitao_top_n,
                max_refs=max_refs,
                timeout_seconds=settings.mcp_timeout_seconds)
        else:
            ref_plan = await refs_source.plan_refs(
                catalog, targets, products=products,
                refs_per_item=settings.refs_per_item, max_refs=max_refs,
                timeout_seconds=settings.mcp_timeout_seconds,
            )

        # 阶段 C：短写事务（幂等抢占 + 预留 + 参考图落库 + 建任务）
        with session_scope() as session:
            from sqlalchemy.exc import IntegrityError

            from app.agents_image.store import quota as quota_store

            # 幂等抢占：先插 pending 行；并发同 key 的第二个请求会撞主键 → 409
            if idempotency_key:
                from app.agents_image.store.models import ImageRequest

                session.add(ImageRequest(request_id=idempotency_key, status="pending",
                                         batch_id="", username=user.username,
                                         payload_hash=payload_hash,
                                         response_json={}))
                try:
                    session.flush()
                except IntegrityError:
                    session.rollback()
                    return _idempotent_conflict(trace)

            # 预留：占住本次计划张数，防止并发下超发（交付成功转 used，失败/取消由事务回滚/结算释放）
            if not user.is_admin:
                if not quota_store.reserve(session, user.username, planned_images):
                    raise ImageError("IMG_QUOTA_EXCEEDED",
                                     f"配额不足：本期剩余 {quota['remaining']} 张，"
                                     f"本次需要 {planned_images} 张", retryable=False)

            resolution = refs_source.persist_refs(session, ref_plan, username=user.username,
                                                  department=user.department)
            batch_id = new_batch_id()
            created: list[dict] = []
            first_slots: list[dict] = []
            for index, (target, platform, variant, state) in enumerate(planned_states):
                params = params_by_platform[platform]
                # 参考图在事务内落库后再绑定到 state（复用预演出来的图位与身份锁定）
                state.refs = resolution.refs_of(target.color_key)
                validate_node.validate(state, settings=settings)
                # 复用只保留本次图位窗口内的 slot（第三轮 P1-2）
                reuse_map = _reuse_window(state, _reuse_of(target, platform, variant))
                # P1-3/P1-4：iterate 但一张图位都复用不到时必须解释清楚（基座 + 目标三元组）
                task_warnings = _reuse_miss_warnings(
                    base_task_id_hint, base_task, target, platform,
                    str(state.variant_id or ""), reuse_map) + list(prompt_warnings)
                # P1-2（第八轮）：目标颜色没有对应参考图时，参考图解析会退回别颜色的图，
                # 这条告警必须落到任务上（否则运营只看到"颜色不对"却不知道原因）
                task_warnings.extend(resolution.warnings)
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
                    ref_source=ref_source,
                    trace_id=trace, attrs=state.attrs,
                    image_count=plan_by_task[index],
                    slot_count=len(state.slots),
                    # 血统只记"同一 (颜色, 平台, 变体) 的 iterate 目标"（即便没有可复用图位）
                    base_task_id=(reuse_plan or {}).get("base_task_id")
                    if (reuse_plan and _reuse_targets(target, platform, variant))
                    else None,
                    reuse_asset_ids=reuse_map,
                    reused_images=len(reuse_map),
                    warnings=task_warnings,
                )
                asset_store.bind_refs_to_task(session, task.ref_asset_ids, task.task_id)
                created.append({"task_id": task.task_id, "product_id": target.product_id,
                                "ref_source": task.ref_source,
                                "color": target.color, "color_key": target.color_key,
                                "platform": platform, "variant_id": state.variant_id,
                                "variant_direction": state.variant_direction,
                                "status": task.status,
                                "refs": len(task.ref_asset_ids),
                                "image_count": task.image_count,
                                "slot_count": task.slot_count,
                                "base_task_id": task.base_task_id,
                                "reused_images": task.reused_images})
                first_slots = first_slots or [item.model_dump() for item in state.slots]
            payload = {
                "batch_id": batch_id, "tasks": created, "slots": first_slots,
                "platforms": platforms,
                "variants": [item.get("id", "") for item in variants],
                "slot_count": base_params["slot_count"],
                "image_count": base_params["image_count"],
                "limits": {"max_refs": max_refs,
                           "max_refs_configured": settings.max_refs,
                           "refs_per_item": settings.refs_per_item,
                           "max_batch_products": settings.max_batch_products,
                           "max_tasks_per_request": settings.max_tasks_per_request,
                           "max_prompt_chars": max_prompt_chars,
                           "refs_truncated": resolution.truncated},
                "planned_images": planned_images,
                "ref_source": ref_source,
                # 复用口径（P2-8）：`reused_images` 是**图片张数**，`reused_tasks` 是**任务数**
                "reused_images": sum(len(_reuse_window(state, _reuse_of(target, platform, variant)))
                                     for (target, platform, variant, state) in planned_states),
                "reused_tasks": sum(1 for (target, platform, variant, state) in planned_states
                                    if _reuse_window(state, _reuse_of(target, platform, variant))),
                "trace_id": trace, "idempotent": False,
            }
            if idempotency_key:
                from app.agents_image.store.models import ImageRequest

                row = session.get(ImageRequest, idempotency_key)
                if row is None:
                    session.add(ImageRequest(request_id=idempotency_key, status="done",
                                             batch_id=batch_id, username=user.username,
                                             payload_hash=payload_hash,
                                             response_json=payload))
                else:
                    row.batch_id = batch_id
                    row.response_json = payload
                    row.payload_hash = payload_hash
                    row.status = "done"
                session.flush()
        # 事务已提交：任务与预留都落库。派发失败不算建任务失败（任务保持 queued，看门狗会补投）
        for item in created:
            try:
                await get_dispatcher().submit(item["task_id"])
            except Exception:  # noqa: BLE001
                logger.warning("任务派发失败（保留 queued，由看门狗补投）：%s", item["task_id"])
        metrics.record_task_created(len(created))
    except ImageError as exc:
        # 事务回滚已撤销预留 → 这里**不再显式释放**（否则会把别人的预留吃掉）
        return error_response(exc, trace)
    except Exception as exc:  # noqa: BLE001
        logger.exception("创建生图任务失败")
        return error_response(exc, trace)
    return JSONResponse(status_code=201, content=payload)


def _idempotent_conflict(trace: str) -> JSONResponse:
    """同 key 的首个请求还在处理中：让客户端 2 秒后重试（拿到首次响应）。"""
    return JSONResponse(
        status_code=409,
        content={"detail": "相同 request_id 的请求正在处理中，请稍后重试",
                 "error_code": "IMG_TASK_CONFLICT", "retryable": True, "trace_id": trace},
        headers={"Retry-After": "2", "X-Error-Code": "IMG_TASK_CONFLICT",
                 "X-Trace-Id": trace},
    )


async def _resolve_targets(catalog, body: dict, product_ids: list[str],
                           products: dict | None = None) -> list[refs_source.RefTarget]:
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
        data = (products or {}).get(product_id)
        if data is None:
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
    if body.get("platform_variant"):
        # W1：`platform_variant` 语义固定为"淘系+抖系双套"，不再被单平台字段降级成一套
        chosen = ["淘系", "抖系"]
    elif explicit:
        chosen = _dedupe(explicit)
    elif single:
        chosen = [single]
    else:
        chosen = ["淘系"]
    allowed = {"淘系", "抖系"}
    unknown = [item for item in chosen if item not in allowed]
    if unknown:
        raise ImageError(
            "IMG_INPUT_INVALID",
            "不支持的平台：" + "、".join(unknown) + "（可选：" + "、".join(sorted(allowed)) + "）")
    return chosen


def _variant_matrix(policy, body: dict, base_variant: str = "") -> list[dict]:
    """mode=standard/variant/iterate → 变体列表（variant 输出 A/B 两套差异化方案）。

    P1-3：iterate 必须**继承基座变体**——过去固定返回 `id=""`，而 A/B 变体基座的 key 是
    `(颜色, 平台, "A"/"B")`，两者永远对不上，于是"永不命中复用"且 `base_task_id` 血统丢失。
    """
    mode = str(body.get("mode") or "standard").strip().lower()
    if mode not in {"standard", "variant", "iterate"}:
        raise ImageError(
            "IMG_INPUT_INVALID",
            "不支持的 mode：" + mode + "（可选 standard / variant / iterate）")
    if mode == "iterate":
        return [{"id": str(base_variant or "").strip(), "label": "只改主图",
                 "hint": "重做主图；其余图位能复用就复用，缺失的图位按差额补齐"}]
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




def _department_of(category: str | None) -> str | None:
    """商品类目 → 部门（与中台 DEPARTMENT_CATEGORIES 同口径：童装归女装）。"""
    mapping = {"男装": "男装", "女装": "女装", "童装": "女装"}
    return mapping.get((category or "").strip())


def _require_department(user) -> str:
    """普通账号必须有部门（第五轮 P0-5）：缺部门 = 不能出图，而不是"全部门都能看"。

    过去 `if user.department and ...` 只在**有部门且不匹配**时拦截，于是"建号时没填部门"
    的账号反而能看到/出所有部门的商品——权限模型被"少填一个字段"绕过。
    """
    department = str(getattr(user, "department", "") or "").strip()
    if user.is_admin:
        return department
    if not department:
        raise ImageError(
            "IMG_FORBIDDEN",
            "账号未绑定部门，不能出图；请联系管理员在「账号管理」里设置部门（男装 / 女装）")
    if department not in DEPARTMENTS:
        raise ImageError("IMG_FORBIDDEN",
                         f"账号部门「{department}」不在允许范围：{'、'.join(DEPARTMENTS)}")
    return department


def _ensure_department(product, user) -> None:
    """部门隔离：普通账号只能出自己部门的商品；类目未维护 → 一律拒绝。"""
    if user.is_admin:
        return
    department_of_user = _require_department(user)
    category = str(getattr(product, "category", "") or "").strip()
    department = _department_of(category)
    if not department:
        raise ImageError("IMG_FORBIDDEN",
                         f"商品类目「{category or '空'}」无法映射到部门，普通账号不能出图；"
                         "请在中台把该类目归到男装/女装（含童装）")
    if department != department_of_user:
        raise ImageError("IMG_FORBIDDEN",
                         f"无权操作 {department} 部门的商品（当前账号：{department_of_user}）")


def _base_snapshot(task) -> SimpleNamespace:
    """把基座任务取成纯数据（会话关闭后 ORM 属性会失效，不能跨 session 读）。"""
    return SimpleNamespace(
        task_id=str(getattr(task, "task_id", "") or ""),
        username=str(getattr(task, "username", "") or ""),
        department=getattr(task, "department", None),
        product_id=str(getattr(task, "product_id", "") or ""),
        platform=str(getattr(task, "platform", "") or ""),
        variant_id=str(getattr(task, "variant_id", "") or ""),
        color_key=str(getattr(task, "color_key", "") or ""),
        status=str(getattr(task, "status", "") or ""),
    )


def _inherit_from_base(body: dict, base) -> dict:
    """iterate 时把基座任务的平台/颜色继承下来（P1-3）。

    只补「请求里根本没给」的维度：显式给了就以请求为准（运维仍可手动改平台/颜色）。
    过去缺平台时默认落到淘系、缺颜色时落到第一个颜色，于是抖系基座或非首色基座
    永远对不上复用 key，复用链等于失效。
    """
    merged = dict(body)
    if not (merged.get("platform") or merged.get("platforms") or merged.get("platform_variant")):
        merged["platform"] = str(getattr(base, "platform", "") or "淘系")
    if not (merged.get("product_colors") or merged.get("all_colors")):
        color_key = str(getattr(base, "color_key", "") or "")
        color = color_key.split("#", 1)[1] if "#" in color_key else ""
        product_id = str(getattr(base, "product_id", "") or "")
        if product_id:
            merged["product_colors"] = [
                {"product_id": product_id, "colors": [color] if color else []}]
    return merged


def _reuse_miss_warnings(base, base_task_id: str, target, platform: str,
                         variant_id: str, reuse_map: dict) -> list[str]:
    """iterate 一张图位都复用不到时落 `IMG_REUSE_SKIPPED`（P1-3 / 第五轮 P1-5）。

    第五轮修正：过去只在「目标与基座三元组完全一致」时才告警——于是**显式改了平台/变体/颜色**
    的 iterate 静默全量重出，用户以为在续做基座。现在只要"给了 base_task_id 却没复用上"就告警，
    并明确列出与基座不同的维度。
    """
    if not base_task_id or reuse_map:
        return []
    target_key = f"{target.color_key or '无颜色'}/{platform}/{variant_id or '-'}"
    if base is None:
        return [f"IMG_REUSE_SKIPPED: 基座 {base_task_id} 与目标 {target_key} 没有可复用的图位，"
                "已按差额全量重出"]
    diffs: list[str] = []
    if str(getattr(base, "color_key", "") or "") != str(target.color_key or ""):
        diffs.append("颜色")
    if str(getattr(base, "platform", "") or "") != str(platform or ""):
        diffs.append("平台")
    if str(getattr(base, "variant_id", "") or "") != str(variant_id or ""):
        diffs.append("变体")
    base_key = (f"{str(getattr(base, 'color_key', '') or '无颜色')}"
                f"/{str(getattr(base, 'platform', '') or '-')}"
                f"/{str(getattr(base, 'variant_id', '') or '-')}")
    hint = f"（与基座不同的维度：{'、'.join(diffs)}）" if diffs else ""
    return [
        f"IMG_REUSE_SKIPPED: 基座 {base_task_id}（{base_key}）与目标 {target_key} "
        f"没有可复用的图位{hint}（基座成品可能被驳回/已过保留期/本任务窗口更小），"
        "已按差额全量重出"
    ]


async def _reuse_plan(session, user, body: dict, targets) -> dict | None:
    """C2/W4：`mode=iterate` + `base_task_id` → 主图重做，其余 generate 图位能复用就复用。

    基础任务缺图位（比如只出了主图）时**不再报错**：可复用集合为空，剩下的图位按
    `image_count` 差额补齐（W4），并只按实际要出的张数占额度。
    """
    mode = str(body.get("mode") or "standard").strip().lower()
    base_task_id = str(body.get("base_task_id") or "").strip()
    if mode != "iterate" or not base_task_id:
        return None
    base = task_store.get_task(session, base_task_id)
    if base is None:
        raise ImageError("IMG_TASK_NOT_FOUND", f"基础任务不存在：{base_task_id}")
    if not user.is_admin and base.username != user.username:
        raise ImageError("IMG_FORBIDDEN", "无权复用他人的任务")
    if base.status not in {"done"}:
        raise ImageError("IMG_TASK_CONFLICT",
                         f"基础任务未完成（{base.status}），不能复用它的成品")
    # 复用目标必须与基础任务一致（同一商品/颜色），否则会串商品
    expected = {target.color_key for target in targets}
    if (base.color_key or "") not in expected:
        raise ImageError("IMG_INPUT_INVALID",
                         f"基础任务的颜色（{base.color_key or '无'}）不在本次出图目标里")
    reuse_map: dict[str, str] = {}
    outputs = asset_store.list_outputs(session, base_task_id)
    for asset in outputs:
        if int(asset.slot) == 1:
            continue                      # 主图要重做
        # P1-4：被人工驳回的成品不能再当复用源（否则驳回的图会顺着复用链复活）
        if str(getattr(asset, "adopt_status", "") or "") == "rejected":
            continue
        if getattr(asset, "purged_at", None) is not None:
            continue                      # 已被清理
        reuse_map[str(asset.slot)] = asset.asset_id
    # 复用只对"同一 (颜色, 平台, 变体)"的目标生效：否则多色/双平台 iterate 会串色串平台
    key = (base.color_key or "", base.platform or "",
           str(getattr(base, "variant_id", "") or ""))
    return {"base_task_id": base_task_id, "key": key, "reuse_map": reuse_map,
            "reused_images": len(reuse_map)}


def _attrs_for_target(base_attrs: dict, target) -> dict:
    """按颜色覆盖身份锁定字段：颜色必须锁到具体 hex，避免多色商品串色。

    W2：目标颜色**没有色值**时不能静默沿用商品首个颜色的 hex（会把米色画成黑色），
    必须报 `IMG_INPUT_INVALID` 让中台补齐；只有"商品本身只有这一个颜色、中台 attrs
    的色值就是它"时才允许沿用。
    """
    attrs = dict(base_attrs or {})
    if not target.color:
        return attrs
    known = [str(item) for item in (attrs.get("color_names") or [])]
    base_hex = str(attrs.get("color_hex") or "")
    attrs["color_names"] = [target.color]
    if target.color_hex:
        attrs["color_hex"] = target.color_hex
        return attrs
    if base_hex and known == [target.color]:
        return attrs          # 单色商品：中台 attrs 的色值就是这个颜色，可以沿用
    raise ImageError(
        "IMG_INPUT_INVALID",
        f"颜色「{target.color}」缺少色值（color_hex），无法锁定身份；请在中台补齐该颜色色值")


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
        try:
            limit = max(1, min(int(request.query_params.get("limit") or 50), 200))
            offset = max(0, int(request.query_params.get("offset") or 0))
        except ValueError as exc:
            raise ImageError("IMG_INPUT_INVALID", "limit/offset 必须是整数") from exc
        with session_scope() as session:
            rows = task_store.list_tasks(
                session, username=None if include_all else user.username,
                limit=limit, offset=offset, batch_id=batch_id or None,
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


@router.post("/tasks/{task_id}/assets/{asset_id}/adopt")
async def adopt_asset(task_id: str, asset_id: str, request: Request) -> JSONResponse:
    """采纳（正样本）：只做人工判断，成本早已按 provider 成功计入，不因驳回返还。"""
    trace = trace_id_of(request)
    try:
        user = require_user(request)
        return _verdict_response(request, task_id, asset_id, "adopted", trace, user)
    except ImageError as exc:
        return error_response(exc, trace)


@router.post("/tasks/{task_id}/assets/{asset_id}/reject")
async def reject_asset(task_id: str, asset_id: str, request: Request) -> JSONResponse:
    """驳回（负样本）：必须给原因，原因会进 Reflexion 的候选补丁。"""
    trace = trace_id_of(request)
    try:
        user = require_user(request)
        body = {}
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 —— 允许空 body
            body = {}
        reason = str((body or {}).get("reason") or "").strip()
        if not reason:
            raise ImageError("IMG_INPUT_INVALID", "驳回必须填写原因（reason）")
        return _verdict_response(request, task_id, asset_id, "rejected", trace, user,
                                 reason=reason)
    except ImageError as exc:
        return error_response(exc, trace)


def _verdict_response(request: Request, task_id: str, asset_id: str, verdict: str,
                      trace: str, user, reason: str = "") -> JSONResponse:
    from app.agents_image.store import feedback as feedback_store

    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        if task is None:
            raise ImageError("IMG_TASK_NOT_FOUND", f"任务不存在：{task_id}")
        _ensure_owner(task.username, user, "任务")
        asset = asset_store.get_asset(session, asset_id)
        if asset is None or asset.task_id != task_id:
            raise ImageError("IMG_TASK_NOT_FOUND", f"图片不属于该任务：{asset_id}")
        payload = feedback_store.record(
            session, task_id=task_id, asset_id=asset_id, verdict=verdict,
            reason=reason, username=user.username, slot=asset.slot)
        payload["stats"] = feedback_store.summary(session, task_id=task_id)
    return JSONResponse(status_code=200, content={**payload, "trace_id": trace})


@router.post("/tasks/{task_id}/assets/{asset_id}/unblock")
def unblock_asset(task_id: str, asset_id: str, request: Request) -> JSONResponse:
    """人工放行被像素门禁拦下的图（管理员或本人；门禁假阳性时用）。

    放行必须把账补齐（P2-11）：图位从 `blocked` 移出、`delivered_images + 1`、留一条
    `IMG_BLOCKED_RELEASED` 告警，否则“人工放行了但交付张数没变”，指标继续少算。
    """
    trace = trace_id_of(request)
    try:
        user = require_user(request)
        with session_scope() as session:
            task = task_store.get_task(session, task_id)
            if task is None:
                raise ImageError("IMG_TASK_NOT_FOUND", f"任务不存在：{task_id}")
            _ensure_owner(task.username, user, "任务")
            asset = asset_store.get_asset(session, asset_id)
            if asset is None or asset.task_id != task_id:
                raise ImageError("IMG_TASK_NOT_FOUND", f"图片不属于该任务：{asset_id}")
            was_blocked = asset.kind == "blocked"
            asset.kind = "output"
            asset.blocked_reason = ""
            asset.adopt_status = "pending"
            if was_blocked:
                blocked = [item for item in (getattr(task, "blocked_json", None) or [])
                           if int(item.get("slot", 0) or 0) != int(asset.slot or 0)]
                warnings = list(getattr(task, "warnings_json", None) or [])
                warnings.append(f"IMG_BLOCKED_RELEASED: 图位 {asset.slot} 已由人工放行")
                task_store.update_task(
                    session, task_id, blocked_json=blocked, warnings_json=warnings,
                    delivered_images=int(getattr(task, "delivered_images", 0) or 0) + 1)
                # P1-4（第八轮）：放行也算"交付"，使用量流水里的 agent_passed 必须同步 +1，
                # 否则卡片的「交付张数」（usage.agent_passed）与任务台账（delivered_images）
                # 从此永久对不上，上架率分子也少一张。
                try:
                    from app.agents_image.store import usage as usage_store

                    usage_store.shift(session, username=task.username,
                                      department=task.department, agent_passed=1)
                except Exception as exc:  # noqa: BLE001 —— 记账失败不影响放行
                    metrics.record_swallowed("unblock_usage")
                    logger.warning("人工放行的使用量记账失败：task=%s %s", task_id, exc)
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={"ok": True, "asset_id": asset_id,
                                                  "trace_id": trace})


@router.post("/tasks/{task_id}/listing")
async def update_listing(task_id: str, request: Request) -> JSONResponse:
    """标记上架（B2，轻量）：`{status: not_listed|pending|listed|failed, listing_url?}`。

    注意：上架动作发生在商家后台，这里只记录"运营填报的状态与链接"，不作为上架凭证。
    """
    trace = trace_id_of(request)
    try:
        user = require_user(request)
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        status = str(body.get("status") or "").strip().lower()
        url = str(body.get("listing_url") or "").strip()
        with session_scope() as session:
            task = task_store.get_task(session, task_id)
            if task is None:
                raise ImageError("IMG_TASK_NOT_FOUND", f"任务不存在：{task_id}")
            _ensure_owner(task.username, user, "任务")
            if status == "listed" and not url:
                raise ImageError("IMG_INPUT_INVALID", "标记已上架时必须填 listing_url（商品链接）")
            task_store.update_listing(session, task_id, status=status, url=url)
            payload = {"task_id": task_id, "status": status, "listing_url": url}
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={**payload, "trace_id": trace})


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str, request: Request) -> JSONResponse:
    """人工重试失败/已取消的任务（功能6）：重新排队再跑一次。

    - 只允许 `failed` / `canceled`；每个任务最多 `IMAGE_MAX_TASK_RETRIES` 次（默认 3）；
    - 重新**预留**额度（失败时已释放），交付成功再结算——和新建任务同一套账；
    - 重置 `recoveries/settled_at/warnings/blocked/listing`（第三轮 P1-10 / P2-11），
      否则重试后的第一次看门狗扫描会按"重投超限"把任务判死。
    """
    trace = trace_id_of(request)
    try:
        user = require_user(request)
        result = await _retry_one(user, task_id)
        payload = {"task_id": result["task_id"], "status": result["status"],
                   "retries": result["retries"]}
    except ImageError as exc:
        return error_response(exc, trace)
    except Exception as exc:  # noqa: BLE001
        logger.exception("任务重试失败")
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={**payload, "trace_id": trace})


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
                "platform": task.platform, "variant_id": task.variant_id,
                "color_key": getattr(task, "color_key", "") or "",
                "slot_count": task.slot_count, "image_count": task.image_count,
                "base_task_id": task.base_task_id,
                "reused_images": task.reused_images,
                **_partial_fields(task),
                "retries": int(getattr(task, "retries", 0) or 0),
                "provider_calls": int(getattr(task, "provider_calls", 0) or 0),
                "status": task.status, "username": task.username, "prompt": task.prompt,
                "prompt_final": task.prompt_final, "aspect": task.aspect, "n": task.n,
                "attempts": task.attempts,
                "attempted_images": task.attempted_images,
                "delivered_images": task.delivered_images,
                "cost_cny": task.cost_cny, "error_code": task.error_code,
                # P1-1：LLM/视觉解析成本单列（详情页与卡片同口径）
                "llm_cost_cny": round(float(getattr(task, "llm_cost_cny", 0.0) or 0.0), 6),
                "cost_total_cny": round(float(task.cost_cny or 0.0)
                                        + float(getattr(task, "llm_cost_cny", 0.0) or 0.0), 6),
                "error_detail": task.error_detail, "trace_id": task.trace_id,
                "backfill_status": task.backfill_status,
                "listing_status": task.listing_status,
                "listing_url": task.listing_url,
                "listed_at": task.listed_at.isoformat() if task.listed_at else "",
                "slots": list(task.slots_json or []),
                "refs": [asset_payload(asset) for asset in refs],
                "assets": [asset_payload(asset) for asset in outputs],
                "copy": dict(task.copy_json or {}),
                "warnings": list(getattr(task, "warnings_json", None) or []),
                "blocked": list(getattr(task, "blocked_json", None) or []),
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


def _partial_fields(task) -> dict:
    """交付口径（第五轮 P0-3）：预期/交付/复用/总数 + 是否部分 + 给用户看的一句话。

    三条规则，别混：
    - **非终态**不算结论（`in_progress=True`）：排队/运行中 delivered 必然小于预期；
    - **failed / canceled 不是"部分成功"**：没有可用图就是失败/取消，交给 `error_code` 解释，
      过去这里会拼出"部分成功，预期 6 张，输出 0 张"，等于诬告运营；
    - **done 且少图**才是部分成功（iterate 的"预期"是差额，复用张数单列说明）。
    """
    expected = int(getattr(task, "image_count", 0) or 0)
    delivered = int(getattr(task, "delivered_images", 0) or 0)
    reused = int(getattr(task, "reused_images", 0) or 0)
    status = str(getattr(task, "status", "") or "")
    payload = {
        "expected_images": expected,
        "delivered_images": delivered,
        "reused_images": reused,
        "total_images": delivered + reused,
        "failure_code": str(getattr(task, "error_code", "") or ""),
        "in_progress": not is_terminal(status),
        "partial": False,
        "message": "",
    }
    if status != "done":
        return payload
    if expected and delivered < expected:
        reused_note = f"（另有 {reused} 张复用）" if reused else ""
        payload["partial"] = True
        payload["message"] = f"部分成功，预期 {expected} 张{reused_note}，输出 {delivered} 张"
    return payload


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, request: Request):
    """取图：`?exp=&sig=` 短签名（给 <img src>）或 Bearer 令牌（给接口调用）。"""
    trace = trace_id_of(request)
    try:
        exp = request.query_params.get("exp") or ""
        sig = request.query_params.get("sig") or ""
        if exp or sig:
            if not verify_asset_signature(asset_id, exp, sig):
                raise ImageError("IMG_FORBIDDEN", "图片签名无效或已过期")
            user = None
        else:
            user = require_user(request)
        with session_scope() as session:
            asset = asset_store.require_asset(session, asset_id)
            if user is not None:
                _ensure_owner(asset.username, user, "图片")
            if getattr(asset, "purged_at", None) is not None:
                raise ImageError("IMG_TASK_NOT_FOUND", "图片已过保留期（本地 24h 已清理）")
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
