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
    """进程内计数 + 在途图片 + RAG 缓存命中（面板的 5 张卡靠它）。"""
    from app.agents_image import metrics as image_metrics
    from app.agents_image.dispatch.task_dispatcher import get_dispatcher
    from app.agents_image.rag import index as rag_index

    snapshot = image_metrics.snapshot()
    try:
        in_flight = int(getattr(get_dispatcher(), "in_flight", 0) or 0)
    except Exception:  # noqa: BLE001
        in_flight = snapshot.get("in_flight_images", 0)
    return {"counters": snapshot, "in_flight": in_flight,
            "rag_cache": rag_index.cache_stats()}


def _cards(session, *, runtime: dict) -> dict:
    from sqlalchemy import select

    from app.agents_image.store.models import ImageTask

    totals = usage_store.totals(session)
    rows = session.execute(select(ImageTask)).scalars().all()
    done = [row for row in rows if row.status == "done"]
    first_pass = [row for row in done if int(row.attempts or 1) <= 1]
    cache = runtime.get("rag_cache") or {}
    return {
        "cache_hit_rate": cache.get("hit_rate", 0.0),
        "llm_calls": 0,                      # 生图链路目前不调 LLM（D16 接 rubric 后再统计）
        "llm_tokens": int(totals.get("tokens") or 0),
        "in_flight_images": int(runtime.get("in_flight") or 0),
        "images_delivered": int(totals.get("images") or 0),
        "images_attempted": int(totals.get("images_attempted") or 0),
        "first_pass_rate": round(len(first_pass) / len(done), 4) if done else 0.0,
        "tasks_done": len(done),
        "tasks_failed": sum(1 for row in rows if row.status == "failed"),
        "cost_cny": totals.get("cost_cny", 0.0),
    }


@router.get("/admin/metrics")
async def admin_metrics(request: Request) -> JSONResponse:
    """近 N 个周期的折线数据 + 五张卡（全员或单账号 × 月/周/日/小时）。"""
    trace = trace_id_of(request)
    try:
        require_admin(request)
        granularity = (request.query_params.get("granularity") or "day").strip().lower()
        username = (request.query_params.get("username") or "").strip() or None
        count = int(request.query_params.get("count") or 30)
        runtime = await _runtime_snapshot()
        with session_scope() as session:
            rows = usage_store.buckets(session, granularity=granularity, count=count,
                                       username=username)
            cards = _cards(session, runtime=runtime)
            outbox = outbox_store.stats(session)
    except ImageError as exc:
        return error_response(exc, trace)
    except ValueError as exc:
        return error_response(ImageError("IMG_INPUT_INVALID", f"count 必须是整数：{exc}"), trace)
    return JSONResponse(status_code=200, content={
        "granularity": granularity, "username": username or "all",
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
        limit = int(request.query_params.get("limit") or 50)
        with session_scope() as session:
            rows = task_store.list_tasks(session, username=username, status=status, limit=limit)
            payload = [{"task_id": row.task_id, "batch_id": row.batch_id,
                        "username": row.username, "department": row.department,
                        "product_id": row.product_id, "status": row.status,
                        "attempts": row.attempts, "n": row.n,
                        "attempted_images": row.attempted_images,
                        "delivered_images": row.delivered_images,
                        "backfill_status": row.backfill_status,
                        "error_code": row.error_code,
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
            payload = {
                "task_id": task.task_id, "batch_id": task.batch_id,
                "username": task.username, "department": task.department,
                "product_id": task.product_id, "status": task.status,
                "attempts": task.attempts, "prompt": task.prompt,
                "prompt_final": task.prompt_final, "aspect": task.aspect, "n": task.n,
                "attempted_images": task.attempted_images,
                "delivered_images": task.delivered_images, "cost_cny": task.cost_cny,
                "error_code": task.error_code, "error_detail": task.error_detail,
                "backfill_status": task.backfill_status,
                "slots": list(task.slots_json or []), "prompt_pack": list(task.prompt_pack_json or []),
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


@router.post("/admin/users")
async def admin_create_user(request: Request) -> JSONResponse:
    trace = trace_id_of(request)
    try:
        admin = require_admin(request)
        body = await request.json()
        username = str((body or {}).get("username") or "").strip()
        password = str((body or {}).get("password") or "")
        if not username or len(password) < 6:
            raise ImageError("IMG_INPUT_INVALID", "用户名不能为空，密码至少 6 位")
        with session_scope() as session:
            user_store.create_user(session, username=username, password=password,
                                   role=str((body or {}).get("role") or "user"),
                                   department=(body or {}).get("department") or None)
    except ImageError as exc:
        return error_response(exc, trace)
    except Exception as exc:  # noqa: BLE001
        return error_response(exc, trace)
    return JSONResponse(status_code=201, content={"ok": True, "username": username,
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
            if body.get("password"):
                if len(str(body["password"])) < 6:
                    raise ImageError("IMG_INPUT_INVALID", "密码至少 6 位")
                user_store.set_password(session, username, str(body["password"]))
            if "enabled" in body:
                user_store.set_enabled(session, username, bool(body["enabled"]))
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


@router.post("/admin/replay-outbox")
async def admin_replay_outbox(request: Request) -> JSONResponse:
    trace = trace_id_of(request)
    try:
        require_admin(request)
        with session_scope() as session:
            result = await outbox_store.replay(session, limit=50)
            stats = outbox_store.stats(session)
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
