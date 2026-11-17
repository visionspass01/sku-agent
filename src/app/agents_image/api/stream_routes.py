"""SSE 进度流 + 取消 + 我的配额（用户侧）。

流的事件序列（与 SKU 侧同风格，前端按事件名 switch）：
`snapshot`（当前快照）→ `stage`（状态变化时）→ `asset`（新图落盘）→ `backfill`（回写结果）→ `done`。
服务端只**轮询数据库**（任务状态本身就在 DB，天然无状态），不依赖进程内队列。
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.agents_image.api.common import asset_payload, error_response
from app.agents_image.api.deps import require_user, trace_id_of
from app.agents_image.store import assets as asset_store
from app.agents_image.store import quota as quota_store
from app.agents_image.store import tasks as task_store
from app.core import sse
from app.core.errors import ImageError
from app.db.engine import session_scope

router = APIRouter(tags=["image-stream"])
_TERMINAL = {"done", "failed", "canceled"}
_POLL_SECONDS = 0.3
_KEEPALIVE_SECONDS = 10
_MAX_STREAM_SECONDS = 120


def _snapshot_payload(task) -> dict:
    return {
        "task_id": task.task_id, "batch_id": task.batch_id, "product_id": task.product_id,
        "status": task.status, "attempts": task.attempts,
        "attempted_images": task.attempted_images,
        "delivered_images": task.delivered_images,
        "backfill_status": task.backfill_status,
        "error_code": task.error_code, "slots": list(task.slots_json or []),
    }


@router.get("/tasks/{task_id}/stream")
async def stream_task(task_id: str, request: Request):
    """SSE：先给快照，再推状态/新图/回写，最后 `done`（终态或超时都会收尾）。"""
    trace = trace_id_of(request)
    try:
        user = require_user(request)
        with session_scope() as session:
            task = task_store.get_task(session, task_id)
            if task is None:
                raise ImageError("IMG_TASK_NOT_FOUND", f"任务不存在：{task_id}")
            if not user.is_admin and task.username != user.username:
                raise ImageError("IMG_FORBIDDEN", "无权访问他人的任务")
            snapshot = _snapshot_payload(task)
    except ImageError as exc:
        return error_response(exc, trace)

    async def event_stream():
        seen_assets: set[str] = set()
        last_status = ""
        last_keepalive = time.time()
        started = time.time()
        yield sse.sse_event("snapshot", {**snapshot, "trace_id": trace})
        while True:
            with session_scope() as session:
                task = task_store.get_task(session, task_id)
                if task is None:
                    yield sse.sse_event("fatal", {"message": f"任务不存在：{task_id}",
                                                  "error_code": "IMG_TASK_NOT_FOUND",
                                                  "trace_id": trace, "fatal": True})
                    return
                status = task.status
                state = _snapshot_payload(task)
                fresh = [asset_payload(asset) for asset in asset_store.list_outputs(session, task_id)
                         if asset.asset_id not in seen_assets]
            for item in fresh:
                seen_assets.add(item["asset_id"])
                yield sse.sse_event("asset", {**item, "task_id": task_id, "trace_id": trace})
            if status != last_status:
                last_status = status
                yield sse.sse_event("stage", {"status": status, "task_id": task_id,
                                              "attempts": state["attempts"],
                                              "trace_id": trace})
            if status in _TERMINAL:
                if state["backfill_status"] not in {"", "pending"}:
                    yield sse.sse_event("backfill", {"status": state["backfill_status"],
                                                     "task_id": task_id, "trace_id": trace})
                yield sse.sse_event("done", {**state, "elapsed_ms": int((time.time() - started) * 1000),
                                             "trace_id": trace})
                return
            if time.time() - started > _MAX_STREAM_SECONDS:
                yield sse.sse_event("done", {**state, "timed_out": True,
                                             "trace_id": trace})
                return
            if time.time() - last_keepalive > _KEEPALIVE_SECONDS:
                last_keepalive = time.time()
                yield sse.sse_comment("keepalive")
            await asyncio.sleep(_POLL_SECONDS)

    return sse.sse_response(event_stream())


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request) -> JSONResponse:
    trace = trace_id_of(request)
    try:
        user = require_user(request)
        with session_scope() as session:
            task = task_store.get_task(session, task_id)
            if task is None:
                raise ImageError("IMG_TASK_NOT_FOUND", f"任务不存在：{task_id}")
            if not user.is_admin and task.username != user.username:
                raise ImageError("IMG_FORBIDDEN", "无权取消他人的任务")
            if task.status in _TERMINAL:
                raise ImageError("IMG_TASK_CONFLICT",
                                 f"任务已结束（{task.status}），不能取消")
        from app.agents_image.dispatch.task_dispatcher import get_dispatcher

        canceled = await get_dispatcher().cancel(task_id)
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200 if canceled else 409,
                        content={"ok": bool(canceled), "task_id": task_id, "trace_id": trace})


@router.get("/quota")
def my_quota(request: Request) -> JSONResponse:
    trace = trace_id_of(request)
    try:
        user = require_user(request)
        with session_scope() as session:
            state = quota_store.check(session, user.username, 0, is_admin=user.is_admin)
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={**state, "username": user.username,
                                                 "trace_id": trace})
