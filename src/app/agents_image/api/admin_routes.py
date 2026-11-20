"""管理员接口（`/api/image/admin/*`）：指标、任务、账号、配额、热加载与 outbox 重放。

原则：
- 一律 `require_admin`（普通账号 403 `IMG_FORBIDDEN`）；
- 指标从「使用量小时流水 + 内存计数 + 任务表」聚合，口径写在返回里，前端不自己算；
- 写操作（改配额、重放 outbox、热加载）都返回执行结果，不静默成功。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.agents_image.api.common import asset_payload, error_response
from app.agents_image.api.deps import require_admin, trace_id_of
from app.agents_image.store import assets as asset_store
from app.agents_image.store import outbox as outbox_store
from app.agents_image.store import quota as quota_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store import usage as usage_store
from app.agents_image.store import users as user_store
from app.core.errors import ImageError
from app.db.engine import session_scope

logger = logging.getLogger("sku-agent.image.admin")

router = APIRouter(tags=["image-admin"])   # 由 `api/routes.py` 以 `/api/image` 前缀挂载，路径以 `/admin/` 开头


async def _runtime_snapshot() -> dict:
    """**本实例**的运行时视图（多副本部署时只能代表当前进程，面板会明确标注）。"""
    from app.agents_image import metrics as image_metrics
    from app.agents_image.dispatch.task_dispatcher import get_dispatcher
    from app.agents_image.generator import handler as generator_handler
    from app.agents_image.rag import index as rag_index

    snapshot = image_metrics.snapshot()
    try:
        in_flight = int(getattr(get_dispatcher(), "in_flight", 0) or 0)
    except Exception:  # noqa: BLE001
        in_flight = snapshot.get("in_flight_images", 0)
    return {"scope": "instance", "counters": snapshot, "in_flight": in_flight,
            "provider_in_flight": generator_handler.provider_in_flight(),
            "rag_cache": rag_index.cache_stats()}


def _cards(session, *, runtime: dict, username: str | None = None,
           department: str | None = None) -> dict:
    """五张卡：**全部从 DB 聚合**（多副本也一致）。

    第三轮 P2-3：卡片要跟 `?username=` / `?department=` 一致（过去只过滤了折线桶，
    卡片还是全局数，单账号视图自相矛盾）；P2-7：在途用 DB 的 running 为准，避免
    "本实例在途 + DB running" 双计数。
    """
    from sqlalchemy import func, select

    from app.agents_image.store.models import ImageTask

    totals = usage_store.totals(session, username=username, department=department)

    def _task_filter(statement):
        if username:
            statement = statement.where(ImageTask.username == username)
        if department:
            statement = statement.where(ImageTask.department == department)
        return statement

    def _count(status: str) -> int:
        return int(session.execute(
            _task_filter(select(func.count()).select_from(ImageTask))
            .where(ImageTask.status == status)).scalar_one() or 0)

    def _sum(column):
        return float(session.execute(
            _task_filter(select(func.coalesce(func.sum(column), 0)))).scalar_one() or 0)

    done_total = _count("done")
    failed_total = _count("failed")
    running_db = _count("running")
    first_pass = int(session.execute(
        _task_filter(select(func.count()).select_from(ImageTask))
        .where(ImageTask.status == "done", ImageTask.attempts <= 1)).scalar_one() or 0)
    delivered_sum = _sum(ImageTask.delivered_images)
    attempted_sum = _sum(ImageTask.attempted_images)
    cost_sum = _sum(ImageTask.external_cost_cny)
    cache = runtime.get("rag_cache") or {}
    # 功能8 口径（唯一真相：都在 usage 流水里）
    usage_images = int(totals.get("images") or 0)          # 使用量：第三方成功生成张数
    provider_calls = int(totals.get("provider_calls") or 0)  # 发起量：第三方调用发起次数
    agent_passed = int(totals.get("agent_passed") or 0)     # 交付张数：Agent 审核通过
    adopted = int(totals.get("adopted") or 0)               # 人工通过（采纳，仅新生成图）
    # 复用图采纳单列（P1-1）：复用图不在使用量分母里，混进分子会让上架率 >100%
    reused_adopted = int(totals.get("reused_adopted") or 0)
    quota_used, quota_limit = _quota_totals(session, department)
    from app.agents_image.config import get_image_settings

    settings = get_image_settings()
    cost_budget_enabled = bool(
        float(getattr(settings, "cost_per_image_cny", 0.0) or 0.0) > 0
        and float(getattr(settings, "cost_budget_cny", 0.0) or 0.0) > 0)
    return {
        "usage_images": usage_images,
        "provider_calls": provider_calls,
        "api_availability": (round(usage_images / provider_calls, 4)
                             if provider_calls else 0.0),
        "agent_passed": agent_passed,
        "agent_pass_rate": (round(agent_passed / usage_images, 4)
                            if usage_images else 0.0),
        # 上架率 = 新生成图采纳 / 使用量；分子取 min(adopted, 使用量) 兜住历史脏账（
        # 旧版把复用图也算进 adopted，那一版留下的行会让比率虚高，这里不让它 >100%）
        "listing_rate": (round(min(adopted, usage_images) / usage_images, 4)
                         if usage_images else 0.0),
        "listing_reported_rate": _listing_rate(session),
        "quota_used": quota_used,
        "quota_limit": quota_limit,
        "quota_used_rate": (round(quota_used / quota_limit, 4) if quota_limit else 0.0),
        # 在途以 DB 为准（跨副本一致）；本实例的在途单独给，避免双计数
        "in_flight": running_db,
        "in_flight_instance": int(runtime.get("in_flight") or 0),
        "cost_per_image": (round(cost_sum / usage_images, 4) if usage_images else 0.0),
        "cache_hit_rate": cache.get("hit_rate", 0.0),
        # 4.4/P1-7：LLM 用量改真实值（过去常量 0），来自进程内 counters（/api/image/metrics 同源）
        "llm_calls": int((runtime.get("counters") or {}).get("llm_calls") or 0),
        "llm_tokens": int((runtime.get("counters") or {}).get("llm_tokens")
                          or totals.get("tokens") or 0),
        "in_flight_images": int(runtime.get("in_flight") or 0),
        "in_flight_scope": "db",
        "tasks_running_db": running_db,      # DB 侧真实在跑（跨副本）
        "provider_in_flight": int(runtime.get("provider_in_flight") or 0),
        "images_delivered": int(delivered_sum),
        "images_attempted": int(attempted_sum),
        "adopted": adopted,
        "reused_adopted": reused_adopted,
        "rejected": int(totals.get("rejected") or 0),
        # P0-2：预算闸是否生效（单价或预算为 0 时它等于不存在，面板要能看出来）
        "cost_budget_enabled": cost_budget_enabled,
        "first_pass_rate": round(first_pass / done_total, 4) if done_total else 0.0,
        "tasks_done": done_total,
        "tasks_failed": failed_total,
        "cost_cny": round(cost_sum, 4),
    }


def _quota_totals(session, department: str | None = None) -> tuple[int, int]:
    """配额总量（已用/上限）：只统计有限额度的账号（管理员 unlimited 不计入分母）。

    P2-2：支持按部门聚合。配额行只有 username，没有部门列，所以先用账号表把
    username 映射成部门再过滤 —— 否则部门视图的"使用量"按部门、"配额"按全局，
    卡片自己跟自己矛盾。
    """
    from sqlalchemy import func, select

    from app.agents_image.store.models import ImageQuota

    usable_users: list[str] | None = None
    if department:
        usable_users = [str(row.get("username") or "") for row in user_store.list_users(session)
                        if str(row.get("department") or "") == department]
        if not usable_users:
            return 0, 0

    used_statement = select(func.coalesce(func.sum(ImageQuota.used), 0))
    limit_statement = select(func.coalesce(func.sum(ImageQuota.period_limit), 0))
    if usable_users:
        used_statement = used_statement.where(ImageQuota.username.in_(usable_users))
        limit_statement = limit_statement.where(ImageQuota.username.in_(usable_users))
    used = int(session.execute(used_statement).scalar_one() or 0)
    limit = int(session.execute(limit_statement).scalar_one() or 0)
    return used, limit


def _listing_rate(session) -> float:
    """上架率（B2）：采纳过的任务里，有多少被运营标记为已上架。"""
    from sqlalchemy import select

    from app.agents_image.store.models import ImageFeedback, ImageTask

    adopted_tasks = {row[0] for row in session.execute(
        select(ImageFeedback.task_id).where(ImageFeedback.verdict == "adopted")).all()}
    if not adopted_tasks:
        return 0.0
    listed = session.execute(
        select(ImageTask.task_id).where(
            ImageTask.task_id.in_(list(adopted_tasks)),
            ImageTask.listing_status == "listed")
    ).scalars().all()
    return round(len(listed) / len(adopted_tasks), 4)


@router.get("/admin/metrics")
async def admin_metrics(request: Request) -> JSONResponse:
    """近 N 个周期的折线数据 + 五张卡（全员或单账号 × 月/周/日/小时）。"""
    trace = trace_id_of(request)
    try:
        require_admin(request)
        granularity = (request.query_params.get("granularity") or "day").strip().lower()
        username = (request.query_params.get("username") or "").strip() or None
        department = (request.query_params.get("department") or "").strip() or None
        count = int(request.query_params.get("count") or 30)
        runtime = await _runtime_snapshot()
        with session_scope() as session:
            rows = usage_store.buckets(session, granularity=granularity, count=count,
                                       username=username, department=department)
            cards = _cards(session, runtime=runtime, username=username,
                           department=department)
            outbox = outbox_store.stats()
    except ImageError as exc:
        return error_response(exc, trace)
    except ValueError as exc:
        return error_response(ImageError("IMG_INPUT_INVALID", f"count 必须是整数：{exc}"), trace)
    return JSONResponse(status_code=200, content={
        "granularity": granularity, "username": username or "all",
        "department": department or "all",
        "buckets": rows, "cards": cards, "outbox": outbox,
        "runtime": runtime, "trace_id": trace,
    })


@router.get("/admin/tasks")
def admin_tasks(request: Request) -> JSONResponse:
    trace = trace_id_of(request)
    try:
        require_admin(request)
        username = (request.query_params.get("username") or "").strip() or None
        status = (request.query_params.get("status") or "").strip() or None
        department = (request.query_params.get("department") or "").strip() or None
        try:
            limit = int(request.query_params.get("limit") or 50)
        except ValueError as exc:      # P1-6：非法 limit 应该是 422，不是 500
            raise ImageError("IMG_INPUT_INVALID", "limit 必须是整数") from exc
        # P2-3：limit 要夹紧，避免一次拉全表
        limit = max(1, min(limit, 200))
        with session_scope() as session:
            rows = task_store.list_tasks(session, username=username, status=status,
                                         department=department, limit=limit)
            payload = [{"task_id": row.task_id, "batch_id": row.batch_id,
                        "username": row.username, "department": row.department,
                        "product_id": row.product_id, "status": row.status,
                        "attempts": row.attempts, "n": row.n,
                        "attempted_images": row.attempted_images,
                        "delivered_images": row.delivered_images,
                        "backfill_status": row.backfill_status,
                "listing_status": row.listing_status,
                        "error_code": row.error_code,
                        # P2-3：列表也要能看出图位数/参考图来源/复用与重试情况
                        "image_count": int(getattr(row, "image_count", 0) or 0),
                        "slot_count": int(getattr(row, "slot_count", 0) or 0),
                        "ref_source": getattr(row, "ref_source", "") or "",
                        "reused_images": int(getattr(row, "reused_images", 0) or 0),
                        "retries": int(getattr(row, "retries", 0) or 0),
                        "provider_calls": int(getattr(row, "provider_calls", 0) or 0),
                        "created_at": row.created_at.isoformat() if row.created_at else ""}
                       for row in rows]
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={"tasks": payload, "trace_id": trace})


@router.get("/admin/tasks/{task_id}")
def admin_task_detail(task_id: str, request: Request) -> JSONResponse:
    trace = trace_id_of(request)
    try:
        require_admin(request)
        with session_scope() as session:
            task = task_store.get_task(session, task_id)
            if task is None:
                raise ImageError("IMG_TASK_NOT_FOUND", f"任务不存在：{task_id}")
            outputs = [asset_payload(asset) for asset in asset_store.list_outputs(session, task_id)]
            refs = [asset_payload(asset) for asset in asset_store.get_assets(session, list(task.ref_asset_ids or []))]
            expected = int(getattr(task, "image_count", 0) or 0)
            delivered = int(getattr(task, "delivered_images", 0) or 0)
            reused = int(getattr(task, "reused_images", 0) or 0)
            status = str(task.status or "")
            terminal = status in {"done", "failed", "canceled"}
            payload = {
                "task_id": task.task_id, "batch_id": task.batch_id,
                "username": task.username, "department": task.department,
                "product_id": task.product_id, "status": task.status,
                "platform": task.platform, "variant_id": task.variant_id,
                "color_key": getattr(task, "color_key", "") or "",
                "slot_count": task.slot_count, "image_count": task.image_count,
                "attempts": task.attempts, "prompt": task.prompt,
                "prompt_final": task.prompt_final, "aspect": task.aspect, "n": task.n,
                "attempted_images": task.attempted_images,
                "cost_cny": task.cost_cny,
                "error_code": task.error_code, "error_detail": task.error_detail,
                "backfill_status": task.backfill_status,
                "listing_status": task.listing_status,
                "listing_url": task.listing_url,
                "slots": list(task.slots_json or []), "prompt_pack": list(task.prompt_pack_json or []),
                # P2-1：详情要能解释"为什么少图/为什么没复用/这次打了几次第三方"
                "warnings": list(getattr(task, "warnings_json", None) or []),
                "blocked": list(getattr(task, "blocked_json", None) or []),
                "copy": dict(getattr(task, "copy_json", None) or {}),
                "expected_images": expected,
                "delivered_images": delivered,
                "total_images": delivered + reused,
                # P0-3：failed/canceled 不算"部分成功"（由 error_code/error_detail 解释）
                "partial": bool(status == "done" and expected and delivered < expected),
                "in_progress": not terminal,
                "ref_source": getattr(task, "ref_source", "") or "",
                "retries": int(getattr(task, "retries", 0) or 0),
                "provider_calls": int(getattr(task, "provider_calls", 0) or 0),
                "base_task_id": getattr(task, "base_task_id", None),
                "reused_images": reused,
                "refs": refs, "assets": outputs,
                "created_at": task.created_at.isoformat() if task.created_at else "",
                "finished_at": task.finished_at.isoformat() if task.finished_at else "",
            }
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={**payload, "trace_id": trace})


@router.get("/admin/users")
def admin_users(request: Request) -> JSONResponse:
    trace = trace_id_of(request)
    try:
        require_admin(request)
        with session_scope() as session:
            rows = user_store.list_users(session)
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={"users": rows, "trace_id": trace})


def _validate_department(value: str | None) -> str | None:
    """部门校验（第五轮 P0-5）：只允许白名单内的部门，空值表示"不绑定"。"""
    from app.agents_image.api.common import DEPARTMENTS

    department = str(value or "").strip()
    if not department:
        return None
    if department not in DEPARTMENTS:
        raise ImageError("IMG_INPUT_INVALID",
                         f"不支持的部门：{department}（可选：{'、'.join(DEPARTMENTS)}）")
    return department


@router.post("/admin/users")
async def admin_create_user(request: Request) -> JSONResponse:
    trace = trace_id_of(request)
    try:
        admin = require_admin(request)
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        role = str(body.get("role") or "user")
        if not username or len(password) < 6:
            raise ImageError("IMG_INPUT_INVALID", "用户名不能为空，密码至少 6 位")
        department = _validate_department(body.get("department"))
        if role != "admin" and not department:
            # P0-5：普通账号没有部门 = 无可见范围；建号时就必须指定，避免"少填一个字段就全权限"
            raise ImageError(
                "IMG_INPUT_INVALID",
                "普通账号必须绑定部门（男装 / 女装）；只有管理员可以不绑定")
        with session_scope() as session:
            user_store.create_user(session, username=username, password=password,
                                   role=role, department=department)
    except ImageError as exc:
        return error_response(exc, trace)
    except Exception as exc:  # noqa: BLE001
        return error_response(exc, trace)
    return JSONResponse(status_code=201, content={"ok": True, "username": username,
                                                  "role": role, "department": department,
                                                  "granted_by": admin.username,
                                                  "trace_id": trace})


@router.put("/admin/users/{username}")
async def admin_update_user(username: str, request: Request) -> JSONResponse:
    trace = trace_id_of(request)
    try:
        require_admin(request)
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        with session_scope() as session:
            row = user_store.get_user(session, username)
            if row is None:
                # 更新一个不存在的账号不该"返回 ok 其实什么也没做"
                raise ImageError("IMG_USER_NOT_FOUND", f"账号不存在：{username}")
            role = str(getattr(row, "role", "") or "user")
            if body.get("password"):
                if len(str(body["password"])) < 6:
                    raise ImageError("IMG_INPUT_INVALID", "密码至少 6 位")
                user_store.set_password(session, username, str(body["password"]))
            if "enabled" in body:
                user_store.set_enabled(session, username, bool(body["enabled"]))
            if "department" in body:
                # P0-5：允许把历史"无部门"账号修回白名单部门；改完仍是空 + 非管理员 → 拒绝
                department = _validate_department(body.get("department"))
                if role != "admin" and not department:
                    raise ImageError("IMG_INPUT_INVALID",
                                     "普通账号必须绑定部门（男装 / 女装）")
                user_store.set_department(session, username, department)
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={"ok": True, "username": username,
                                                  "trace_id": trace})


@router.get("/admin/quota")
def admin_quota(request: Request) -> JSONResponse:
    trace = trace_id_of(request)
    try:
        require_admin(request)
        with session_scope() as session:
            rows = quota_store.list_quotas(session)
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={"quotas": rows, "trace_id": trace})


@router.put("/admin/quota/{username}")
async def admin_grant_quota(username: str, request: Request) -> JSONResponse:
    trace = trace_id_of(request)
    try:
        admin = require_admin(request)
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        period_type = str(body.get("period_type") or "day")
        limit = int(body.get("period_limit") or 0)
        if period_type not in {"day", "week", "month", "unlimited"}:
            raise ImageError("IMG_INPUT_INVALID", f"不支持的周期：{period_type}")
        with session_scope() as session:
            row = quota_store.grant(session, username, period_type=period_type,
                                    period_limit=limit, granted_by=admin.username)
            payload = {"username": row.username, "period_type": row.period_type,
                       "period_limit": int(row.period_limit or 0),
                       "remaining": quota_store.remaining(row)}
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={**payload, "trace_id": trace})


@router.get("/admin/samples")
def admin_samples(request: Request) -> JSONResponse:
    """正/负样本清单（M6）：采纳=正样本、驳回=负样本。"""
    trace = trace_id_of(request)
    try:
        require_admin(request)
        from app.agents_image.store import feedback as feedback_store

        kind = (request.query_params.get("kind") or "").strip() or None
        with session_scope() as session:
            rows = feedback_store.samples(session, kind=kind)
            summary = feedback_store.summary(session)
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={"samples": rows, "summary": summary,
                                                  "trace_id": trace})


@router.get("/admin/feedback")
def admin_feedback(request: Request) -> JSONResponse:
    trace = trace_id_of(request)
    try:
        require_admin(request)
        from app.agents_image.store import feedback as feedback_store

        with session_scope() as session:
            rows = feedback_store.list_feedback(session)
            summary = feedback_store.summary(session)
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={"feedback": rows, "summary": summary,
                                                  "trace_id": trace})


@router.post("/admin/replay-outbox")
async def admin_replay_outbox(request: Request) -> JSONResponse:
    """手动重放回写队列（P1-5：网络 IO 在会话之外，只有读统计时才开会话）。"""
    trace = trace_id_of(request)
    try:
        require_admin(request)
        result = await outbox_store.replay(limit=50)
        stats = outbox_store.stats()
        with session_scope() as session:
            pending = outbox_store.list_outbox(session, status="pending")
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={**result, "stats": stats,
                                                  "pending": pending, "trace_id": trace})


@router.post("/admin/reload")
def admin_reload(request: Request) -> JSONResponse:
    """热加载 manifest / prompt（注册表靠指纹判断，改文件即生效）。"""
    trace = trace_id_of(request)
    try:
        require_admin(request)
        from app.agents_image.registry import get_registry

        changed = get_registry().maybe_reload()
        health = get_registry().health()
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={"reloaded": bool(changed),
                                                  "health": health, "trace_id": trace})


@router.post("/admin/reindex")
def admin_reindex(request: Request) -> JSONResponse:
    """重建本地知识库索引（改 data/kb_image 后不用重启）。"""
    trace = trace_id_of(request)
    try:
        require_admin(request)
        from app.agents_image.rag import index as rag_index

        rag_index.reset_index()
        status = rag_index.index_status()
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={"index": status, "trace_id": trace})
