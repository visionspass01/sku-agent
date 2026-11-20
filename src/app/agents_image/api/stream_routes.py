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

from app.agents_image.api.batch_routes import cancel_one
from app.agents_image.api.common import (
    TERMINAL_STATUSES,
    asset_payload,
    error_response,
    is_terminal,
)
from app.agents_image.api.deps import require_user, require_user_async, trace_id_of
from app.agents_image.dispatch.task_dispatcher import get_dispatcher
from app.agents_image.store import assets as asset_store
from app.agents_image.store import quota as quota_store
from app.agents_image.store import tasks as task_store
from app.core import sse
from app.core.errors import ImageError
from app.db.engine import session_scope

router = APIRouter(tags=["image-stream"])
# 第五轮 P1-11：终态集合统一从 common 取，不再各文件各写一份
_TERMINAL = TERMINAL_STATUSES
_POLL_SECONDS = 3.0   # P1-7：图片场景对时延不敏感，3s 轮询把 DB 压力降一个数量级
_KEEPALIVE_SECONDS = 10


def _max_stream_seconds() -> int:
    """单连接最长时长（默认 300s）：覆盖 provider 60s×3 重试 + 反射轮次。"""
    from app.agents_image.config import get_image_settings

    return int(getattr(get_image_settings(), "sse_max_seconds", 300) or 300)


def _sse_limit() -> int:
    from app.agents_image.config import get_image_settings

    return max(1, int(getattr(get_image_settings(), "sse_max_connections", 32) or 32))


_sse_guard: asyncio.Semaphore | None = None
_sse_guard_limit = 0
_sse_guard_loop: object | None = None


def _guard() -> asyncio.Semaphore:
    """SSE 并发连接信号量（P1-6）：每个连接 3s 轮询一次 DB，连接数必须有上限。

    P2-3（第十轮）：信号量会绑定首次使用的事件循环（`_LoopBoundMixin` 缓存 loop），
    跨 loop 复用会 RuntimeError。这里按 (limit, loop) 缓存/重建，与
    `generator/handler._provider_semaphore` 同款处理（多 loop：脚本+服务混跑、测试多 client）。
    """
    global _sse_guard, _sse_guard_limit, _sse_guard_loop
    limit = _sse_limit()
    import asyncio as _asyncio

    loop = _asyncio.get_running_loop()
    if _sse_guard is None or _sse_guard_limit != limit or _sse_guard_loop is not loop:
        _sse_guard = asyncio.Semaphore(limit)
        _sse_guard_limit = limit
        _sse_guard_loop = loop
    return _sse_guard


async def _try_acquire(sem: asyncio.Semaphore) -> bool:
    try:
        await asyncio.wait_for(sem.acquire(), timeout=0.01)
        return True
    except (asyncio.TimeoutError, TimeoutError):
        return False


def reset_sse_guard() -> None:
    """测试/热重载用：重建信号量（换事件循环时必须重建）。"""
    global _sse_guard, _sse_guard_limit, _sse_guard_loop
    _sse_guard = None
    _sse_guard_limit = 0
    _sse_guard_loop = None


def _snapshot_payload(task) -> dict:
    expected = int(getattr(task, "image_count", 0) or 0)
    delivered = int(getattr(task, "delivered_images", 0) or 0)
    reused = int(getattr(task, "reused_images", 0) or 0)
    status = str(getattr(task, "status", "") or "")
    # P0-3：只有 done 且少图才是"部分成功"；failed/canceled 由 error_code 解释
    partial = bool(status == "done" and expected and delivered < expected)
    reused_note = f"（另有 {reused} 张复用）" if reused else ""
    return {
        "task_id": task.task_id, "batch_id": task.batch_id, "product_id": task.product_id,
        "platform": task.platform, "variant_id": getattr(task, "variant_id", "") or "",
        "color_key": getattr(task, "color_key", "") or "",
        "color": (str(getattr(task, "color_key", "") or "").split("#", 1)[1]
                  if "#" in str(getattr(task, "color_key", "") or "") else ""),
        "base_task_id": getattr(task, "base_task_id", "") or "",
        "status": status, "attempts": task.attempts,
        # P1-10：图位数/出图张数/参考图来源/重试/发起量都进快照，
        # 前端 iterate 才能用任务的真实参数，而不是回读 UI 输入框
        "slot_count": int(getattr(task, "slot_count", 0) or 0),
        "image_count": expected,
        "ref_source": getattr(task, "ref_source", "") or "",
        "retries": int(getattr(task, "retries", 0) or 0),
        "provider_calls": int(getattr(task, "provider_calls", 0) or 0),
        "attempted_images": task.attempted_images,
        "delivered_images": delivered,
        "reused_images": reused,
        "total_images": delivered + reused,
        "expected_images": expected,
        "partial": partial,
        "in_progress": not is_terminal(status),
        "message": (f"部分成功，预期 {expected} 张{reused_note}，输出 {delivered} 张"
                    if partial else ""),
        "warnings": list(getattr(task, "warnings_json", None) or []),
        "blocked": list(getattr(task, "blocked_json", None) or []),
        "backfill_status": task.backfill_status,
        "error_code": task.error_code, "slots": list(task.slots_json or []),
    }


@router.get("/tasks/{task_id}/stream")
async def stream_task(task_id: str, request: Request):
    """SSE：先给快照，再推状态/新图/回写，最后 `done`（终态或超时都会收尾）。

    P1-6（第九轮）：并发连接数有上限（`IMAGE_SSE_MAX_CONNECTIONS`），
    超限直接 429 `IMG_RATE_LIMITED`，避免连接数把 DB 轮询压力放大。
    """
    trace = trace_id_of(request)
    try:
        user = await require_user_async(request)

        def _snapshot_of() -> dict:
            with session_scope() as session:
                task = task_store.get_task(session, task_id)
                if task is None:
                    raise ImageError("IMG_TASK_NOT_FOUND", f"任务不存在：{task_id}")
                if not user.is_admin and task.username != user.username:
                    raise ImageError("IMG_FORBIDDEN", "无权访问他人的任务")
                return _snapshot_payload(task)

        # P1-3：首帧快照的同步 DB 查询丢线程池
        from app.observability.trace import run_in_trace

        snapshot = await run_in_trace(_snapshot_of)
    except ImageError as exc:
        return error_response(exc, trace)

    guard = _guard()
    if not await _try_acquire(guard):
        return error_response(
            ImageError("IMG_RATE_LIMITED",
                       f"SSE 并发连接已达上限（{_sse_limit()}），请稍后重试",
                       extra={"retry_after": 5}), trace)

    async def event_stream():
        """薄包装：保证连接结束（正常/超时/客户端断开）一定释放并发槽位。"""
        try:
            async for chunk in _stream_body(task_id, snapshot, trace):
                yield chunk
        finally:
            guard.release()

    return sse.sse_response(event_stream())


async def _stream_body(task_id: str, snapshot: dict, trace: str):
    from app.observability.trace import run_in_trace

    seen_assets: set[str] = set()
    last_status = ""
    last_keepalive = time.time()
    started = time.time()
    max_stream_seconds = _max_stream_seconds()

    def _poll() -> tuple[str | None, dict | None, list[dict]]:
        """一次轮询：同步 DB（快照 + 新图），必须在**线程池**里跑。

        P1-2（第十二轮）：上一版只有首帧快照 offload，循环体里每 3s 直接开同步会话——
        连接数一多（上限 32）就是持续占着事件循环；而每个连接本来是为了"看进度"，
        却把服务本身拖慢。现在整块丢线程池。
        """
        with session_scope() as session:
            task = task_store.get_task(session, task_id)
            if task is None:
                return None, None, []
            state = _snapshot_payload(task)
            fresh = [asset_payload(asset) for asset in asset_store.list_outputs(session, task_id)
                     if asset.asset_id not in seen_assets]
            return str(task.status or ""), state, fresh

    yield sse.sse_event("snapshot", {**snapshot, "trace_id": trace})
    while True:
        status, state, fresh = await run_in_trace(_poll)
        if status is None:
            yield sse.sse_event("fatal", {"message": f"任务不存在：{task_id}",
                                          "error_code": "IMG_TASK_NOT_FOUND",
                                          "trace_id": trace, "fatal": True})
            return
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
        if time.time() - started > max_stream_seconds:
            yield sse.sse_event("done", {**state, "timed_out": True,
                                         "trace_id": trace})
            return
        if time.time() - last_keepalive > _KEEPALIVE_SECONDS:
            last_keepalive = time.time()
            yield sse.sse_comment("keepalive")
        await asyncio.sleep(_POLL_SECONDS)


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request) -> JSONResponse:
    """取消任务（P1-6：区分"还没调模型"与"模型已提交"两种语义）。

    - **未调用生图模型** → 真正取消，消息 `未调用生图模型的任务请求已取消。`
    - **已调用生图模型** → 无法撤回（第三方请求已发出、钱已花），置 `cancel_requested`，
      本次结果返回后直接丢弃；消息说明"无法撤回、结果将被丢弃"。

    判定逻辑与批次取消共用 `batch_routes.cancel_one`，避免两处语义漂移。
    """
    trace = trace_id_of(request)
    try:
        user = await require_user_async(request)
        from app.observability.trace import run_in_trace

        # P1-3：取消判定是同步 DB，丢线程池
        outcome = await run_in_trace(cancel_one, user, task_id)
        if not outcome.get("needs_dispatch"):
            return JSONResponse(status_code=409, content={
                "ok": False, "canceled": False, "provider_in_flight": True,
                "task_id": task_id, "message": outcome.get("message", ""),
                "trace_id": trace,
            })
        canceled = await get_dispatcher().cancel(task_id)
        if not canceled:
            raise ImageError("IMG_TASK_CONFLICT", "任务已结束，不能取消")
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={
        "ok": True, "canceled": True, "provider_in_flight": False,
        "task_id": task_id, "message": "未调用生图模型的任务请求已取消。", "trace_id": trace,
    })


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
