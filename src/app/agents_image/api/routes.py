"""生图 Agent 的 HTTP 路由（`/api/image/*`）。

约定：
- 参考图上传用**原始二进制 body**（Content-Type: image/png|jpeg|webp），不引入 python-multipart；
- 除 `/health` 外**都需要登录**（`Authorization: Bearer <token>`，见 `api/auth_routes.py`）；
- 普通账号只能看自己的任务/资产，管理员可看全部；越权返回 403 `IMG_FORBIDDEN`；
- 任务创建后异步执行（本地调度器），客户端轮询 `GET /tasks/{id}` 或（D13 起）订阅 SSE；
- 错误统一走 `app.core.errors`：响应体带 error_code/trace_id，响应头带 X-Error-Code/X-Trace-Id。
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
from app.agents_image.ids import new_task_id
from app.agents_image.nodes import plan_slots
from app.agents_image.nodes import validate as validate_node
from app.agents_image.providers.image_provider import providers
from app.agents_image.state import ImageTaskState, RefImage
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
        "vision_provider": settings.vision_provider,
        "storage": settings.storage,
        "dispatcher": settings.dispatcher,
        "concurrency": settings.concurrency,
        "max_refs": settings.max_refs,
        "secret_is_default": settings.secret_is_default,
        "degraded": False,
        "degraded_reason": "",
        **metrics.snapshot(),
    }


@router.post("/upload")
async def upload_reference(request: Request) -> JSONResponse:
    """上传一张参考图（二进制 body）；返回 asset_id 供建任务时引用。"""
    trace = trace_id_of(request)
    settings = get_image_settings()
    try:
        user = require_user(request)
        data = await request.body()
        if not data:
            raise ImageError("IMG_REFERENCE_INVALID", "空请求体：请以 image/png|jpeg|webp 发送图片")
        limit = settings.upload_max_mb * 1024 * 1024
        if len(data) > limit:
            raise ImageError("IMG_UPLOAD_TOO_LARGE",
                             f"单张参考图最多 {settings.upload_max_mb}MB，收到 {len(data) // 1024}KB")
        mime = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        with session_scope() as session:
            asset = asset_store.save_bytes(
                session, data, kind="upload", mime=mime,
                username=user.username, department=user.department,
            )
            payload = asset_payload(asset)
    except ImageError as exc:
        return error_response(exc, trace)
    except Exception as exc:  # noqa: BLE001 —— 未知异常也要带 trace 返回
        logger.exception("参考图上传失败")
        return error_response(exc, trace)
    return JSONResponse(status_code=201, content={**payload, "trace_id": trace})


@router.post("/tasks")
async def create_task(request: Request) -> JSONResponse:
    """创建生图任务：`{ref_asset_ids, prompt, aspect, n, platform, compositions, scene_styles}`。"""
    trace = trace_id_of(request)
    settings = get_image_settings()
    try:
        user = require_user(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise ImageError("IMG_INPUT_INVALID", "请求体必须是 JSON 对象")
        ref_ids = [str(x) for x in (body.get("ref_asset_ids") or [])]
        prompt = str(body.get("prompt") or "").strip()
        aspect = str(body.get("aspect") or "1:1")
        count = int(body.get("n") or 1)
        platform = str(body.get("platform") or "淘系")
        compositions = [str(x) for x in (body.get("compositions") or [])]
        scene_styles = [str(x) for x in (body.get("scene_styles") or [])]

        preview = ImageTaskState(prompt=prompt, aspect=aspect, count=count,
                                 compositions=compositions, scene_styles=scene_styles,
                                 refs=[RefImage(asset_id=item) for item in ref_ids])
        preview.slots = plan_slots.build_slots(preview)
        if ref_ids:
            validate_node.validate(preview, settings=settings)
        else:
            _require_refs()

        with session_scope() as session:
            for ref_id in ref_ids:
                asset = asset_store.get_asset(session, ref_id)
                if asset is None:
                    raise ImageError("IMG_REFERENCE_INVALID", f"参考图不存在：{ref_id}")
                _ensure_owner(asset.username, user, "参考图")
            task_id = new_task_id()
            prompt_pack = plan_slots.compose(preview, _registry())
            task = task_store.create_task(
                session, task_id=task_id, username=user.username,
                department=user.department,
                prompt=prompt, platform=platform, aspect=aspect, n=count,
                ref_asset_ids=ref_ids, compositions=compositions,
                scene_styles=scene_styles, slots=preview.slots,
                prompt_pack=[d.model_dump() for d in prompt_pack],
                request_id=str(body.get("request_id") or ""), trace_id=trace,
            )
            payload = {"task_id": task.task_id, "status": task.status,
                       "slots": preview.slots, "trace_id": trace,
                       "limits": _manifest_limits(settings)}
        metrics.record_task_created()
        await get_dispatcher().submit(task_id)
    except ImageError as exc:
        return error_response(exc, trace)
    except Exception as exc:  # noqa: BLE001
        logger.exception("创建生图任务失败")
        return error_response(exc, trace)
    return JSONResponse(status_code=201, content=payload)


def _ensure_owner(owner: str | None, user, what: str) -> None:
    """普通账号只能碰自己的数据；管理员不限（演示环境不区分部门）。"""
    if user.is_admin:
        return
    if (owner or "") != user.username:
        raise ImageError("IMG_FORBIDDEN", f"无权访问他人的{what}")


def _require_refs() -> None:
    raise ImageError("IMG_REFERENCE_REQUIRED", "图生图必须上传参考图（1–6 张）")


def _manifest_limits(settings) -> dict:
    return {"max_refs": settings.max_refs, "max_total_images": settings.max_total_images}


def _registry():
    from app.agents_image.registry import get_registry

    return get_registry()


@router.get("/tasks")
def list_tasks(request: Request) -> JSONResponse:
    """我的任务列表（管理员可看全部，?all=true）；最近 50 条。"""
    trace = trace_id_of(request)
    try:
        user = require_user(request)
        include_all = user.is_admin and request.query_params.get("all") in {"1", "true", "yes"}
        with session_scope() as session:
            rows = task_store.list_tasks(
                session, username=None if include_all else user.username, limit=50,
            )
            payload = [{"task_id": row.task_id, "status": row.status, "username": row.username,
                        "prompt": row.prompt, "n": row.n,
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
            payload = {
                "task_id": task.task_id, "status": task.status,
                "username": task.username, "prompt": task.prompt,
                "prompt_final": task.prompt_final, "aspect": task.aspect, "n": task.n,
                "attempted_images": task.attempted_images,
                "delivered_images": task.delivered_images,
                "cost_cny": task.cost_cny, "error_code": task.error_code,
                "error_detail": task.error_detail, "trace_id": task.trace_id,
                "slots": list(task.slots_json or []),
                "assets": [asset_payload(asset) for asset in outputs],
                "created_at": task.created_at.isoformat() if task.created_at else "",
                "finished_at": task.finished_at.isoformat() if task.finished_at else "",
            }
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content=payload)


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
