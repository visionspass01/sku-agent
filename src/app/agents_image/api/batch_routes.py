"""批次级操作（功能12）：一次对整批任务取消 / 重试 / 看汇总。

为什么要有批次接口：用户是按「批量选中商品」下的单，出问题时也是整批出问题
（改错了 prompt、参考图选错、配额评估错），逐个任务点取消/重试既不现实也容易漏。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.agents_image.api.common import error_response
from app.agents_image.api.deps import require_user, trace_id_of
from app.agents_image.config import get_image_settings
from app.agents_image.dispatch.task_dispatcher import get_dispatcher
from app.agents_image.store import quota as quota_store
from app.agents_image.store import tasks as task_store
from app.core.errors import ImageError
from app.db.engine import session_scope

logger = logging.getLogger("sku-agent.image.api.batch")

router = APIRouter(tags=["image-batch"])

_TERMINAL = {"done", "failed", "canceled"}


def cancel_one(user, task_id: str) -> dict:
    """取消单个任务的**判定部分**（两分支语义）。

    返回 `{"canceled": ..., "provider_in_flight": ...}` 表示"已经有结论"（已在途/已结束），
    返回 `{"needs_dispatch": True}` 表示还没调模型、需要调度器真取消。
    """
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        if task is None:
            raise ImageError("IMG_TASK_NOT_FOUND", f"任务不存在：{task_id}")
        if not user.is_admin and task.username != user.username:
            raise ImageError("IMG_FORBIDDEN", "无权取消他人的任务")
        if task.status in _TERMINAL:
            raise ImageError("IMG_TASK_CONFLICT", f"任务已结束（{task.status}），不能取消")
        provider_started = task.provider_started_at is not None
        if provider_started:
            task_store.update_task(session, task_id, cancel_requested=True)
    if provider_started:
        return {"task_id": task_id, "canceled": False, "provider_in_flight": True,
                "message": "已提交生图模型，无法撤回；本次结果将被丢弃（成本已发生）"}
    return {"task_id": task_id, "needs_dispatch": True}


async def _cancel_dispatch(task_id: str) -> bool:
    return await get_dispatcher().cancel(task_id)


async def retry_one(user, task_id: str) -> dict:
    """重试单个任务：重新排队 + 重新预留额度（与被取消/失败的额度释放配对）。"""
    settings = get_image_settings()
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        if task is None:
            raise ImageError("IMG_TASK_NOT_FOUND", f"任务不存在：{task_id}")
        if not user.is_admin and task.username != user.username:
            raise ImageError("IMG_FORBIDDEN", "无权重试他人的任务")
        if task.status not in {"failed", "canceled"}:
            raise ImageError("IMG_TASK_CONFLICT",
                             f"只有 failed / canceled 的任务可以重试（当前 {task.status}）")
        retries = int(getattr(task, "retries", 0) or 0)
        if retries >= int(settings.max_task_retries):
            raise ImageError("IMG_TASK_CONFLICT",
                             f"该任务已重试 {retries} 次，达到上限 {settings.max_task_retries}")
        planned = int(task.image_count or 0)
        username = task.username
    with session_scope() as session:
        if not user.is_admin and planned and not quota_store.reserve(session, username, planned):
            quota = quota_store.check(session, username, planned)
            raise ImageError("IMG_QUOTA_EXCEEDED",
                             f"配额不足：本期剩余 {quota['remaining']} 张，重试需要 {planned} 张",
                             retryable=False)
        task_store.update_task(
            session, task_id, status="queued", retries=retries + 1,
            error_code="", error_detail="", finished_at=None, attempts=0,
            attempted_images=0, delivered_images=0, provider_started_at=None,
            cancel_requested=False, lease_until=None)
    try:
        await get_dispatcher().submit(task_id)
    except Exception:  # noqa: BLE001 —— 已入库，看门狗会补投
        logger.warning("批次重试派发失败（保留 queued）：%s", task_id)
    return {"task_id": task_id, "status": "queued", "retries": retries + 1}


def _batch_tasks(session, batch_id: str) -> list:
    return task_store.list_tasks(session, batch_id=batch_id, limit=500)


def _body_batch_id(body: dict) -> str:
    batch_id = str((body or {}).get("batch_id") or "").strip()
    if not batch_id:
        raise ImageError("IMG_INPUT_INVALID", "缺少 batch_id")
    return batch_id


@router.post("/tasks/cancel-batch")
async def cancel_batch(request: Request) -> JSONResponse:
    """整批取消：逐任务按两分支语义处理，返回每个任务的结果与统计。"""
    trace = trace_id_of(request)
    try:
        user = require_user(request)
        batch_id = _body_batch_id(await request.json())
        with session_scope() as session:
            rows = _batch_tasks(session, batch_id)
            if not rows:
                raise ImageError("IMG_TASK_NOT_FOUND", f"批次不存在或没有任务：{batch_id}")
            for row in rows:
                if not user.is_admin and row.username != user.username:
                    raise ImageError("IMG_FORBIDDEN", "批次里有他人任务，不能整批取消")
            ids = [row.task_id for row in rows]
        results = []
        for task_id in ids:
            try:
                outcome = cancel_one(user, task_id)
                if not outcome.get("needs_dispatch"):
                    results.append(outcome)
                    continue
                ok = await _cancel_dispatch(task_id)
                results.append({"task_id": task_id, "canceled": ok,
                                "provider_in_flight": False,
                                "message": ("未调用生图模型的任务请求已取消。" if ok
                                            else "任务已结束，跳过")})
            except ImageError as exc:
                results.append({"task_id": task_id, "canceled": False,
                                "error_code": exc.code, "message": exc.detail})
        payload = {
            "batch_id": batch_id,
            "canceled": sum(1 for item in results if item.get("canceled")),
            "discarded": sum(1 for item in results
                             if item.get("provider_in_flight")
                             and "无法撤回" in item.get("message", "")),
            "skipped": sum(1 for item in results if not item.get("canceled")
                           and not item.get("provider_in_flight")),
            "results": results,
        }
    except ImageError as exc:
        return error_response(exc, trace)
    except Exception:  # noqa: BLE001
        logger.exception("整批取消失败")
        return error_response(ImageError("IMG_INTERNAL", "整批取消失败"), trace)
    return JSONResponse(status_code=200, content={**payload, "trace_id": trace})


@router.post("/tasks/retry-batch")
async def retry_batch(request: Request) -> JSONResponse:
    """整批重试：只挑 `failed` / `canceled` 的任务重新排队（其余跳过并说明原因）。"""
    trace = trace_id_of(request)
    try:
        user = require_user(request)
        batch_id = _body_batch_id(await request.json())
        with session_scope() as session:
            rows = _batch_tasks(session, batch_id)
            if not rows:
                raise ImageError("IMG_TASK_NOT_FOUND", f"批次不存在或没有任务：{batch_id}")
            if not user.is_admin and any(row.username != user.username for row in rows):
                raise ImageError("IMG_FORBIDDEN", "批次里有他人任务，不能整批重试")
            candidates = [(row.task_id, row.status) for row in rows]
        results = []
        for task_id, status in candidates:
            if status not in {"failed", "canceled"}:
                results.append({"task_id": task_id, "retried": False,
                                "message": f"状态 {status} 不需要重试"})
                continue
            try:
                results.append({"retried": True, **await retry_one(user, task_id)})
            except ImageError as exc:
                results.append({"task_id": task_id, "retried": False,
                                "error_code": exc.code, "message": exc.detail})
        payload = {"batch_id": batch_id,
                   "retried": sum(1 for item in results if item.get("retried")),
                   "skipped": sum(1 for item in results if not item.get("retried")),
                   "results": results}
    except ImageError as exc:
        return error_response(exc, trace)
    except Exception:  # noqa: BLE001
        logger.exception("整批重试失败")
        return error_response(ImageError("IMG_INTERNAL", "整批重试失败"), trace)
    return JSONResponse(status_code=200, content={**payload, "trace_id": trace})


@router.get("/batches/{batch_id}")
def batch_detail(batch_id: str, request: Request) -> JSONResponse:
    """批次汇总：状态分布 + 交付/尝试/成本 + 参考图来源 + 上架情况 + 任务brief。"""
    trace = trace_id_of(request)
    try:
        user = require_user(request)
        with session_scope() as session:
            rows = _batch_tasks(session, batch_id)
            if not rows:
                raise ImageError("IMG_TASK_NOT_FOUND", f"批次不存在或没有任务：{batch_id}")
            if not user.is_admin and any(row.username != user.username for row in rows):
                raise ImageError("IMG_FORBIDDEN", "无权查看他人的批次")
            statuses: dict[str, int] = {}
            delivered = attempted = reused = 0
            cost = 0.0
            listed = 0
            tasks = []
            for row in rows:
                statuses[row.status] = statuses.get(row.status, 0) + 1
                delivered += int(row.delivered_images or 0)
                attempted += int(row.attempted_images or 0)
                reused += int(getattr(row, "reused_images", 0) or 0)
                cost += float(row.external_cost_cny or 0)
                if (row.listing_status or "") == "listed":
                    listed += 1
                tasks.append({
                    "task_id": row.task_id, "product_id": row.product_id,
                    "color_key": getattr(row, "color_key", ""),
                    "platform": row.platform, "variant_id": row.variant_id,
                    "status": row.status, "delivered_images": row.delivered_images,
                    "attempted_images": row.attempted_images,
                    "reused_images": getattr(row, "reused_images", 0),
                    "error_code": row.error_code,
                    "listing_status": row.listing_status,
                    "ref_source": row.ref_source,
                })
            payload = {
                "batch_id": batch_id,
                "count": len(rows),
                "statuses": statuses,
                "delivered_images": delivered,
                "attempted_images": attempted,
                "reused_images": reused,
                "cost_cny": round(cost, 4),
                "listed_tasks": listed,
                "ref_source": rows[0].ref_source,
                "products": sorted({row.product_id for row in rows if row.product_id}),
                "platforms": sorted({row.platform for row in rows if row.platform}),
                "tasks": tasks,
            }
    except ImageError as exc:
        return error_response(exc, trace)
    except Exception:  # noqa: BLE001
        logger.exception("批次汇总失败")
        return error_response(ImageError("IMG_INTERNAL", "批次汇总失败"), trace)
    return JSONResponse(status_code=200, content={**payload, "trace_id": trace})


__all__ = ["router", "cancel_one", "retry_one"]
