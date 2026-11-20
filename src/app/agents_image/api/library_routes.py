"""素材库（W3）：人工上传参考图的全局池，按账号隔离、任务时勾选。

接口（由 `api/routes.py` 以 `/api/image` 前缀挂载）：
- `POST /api/image/library`：上传一张图（JSON base64），返回资产 id + 签名地址；
- `GET /api/image/library`：列表（普通账号只看自己的；管理员 `?scope=all` 看全部）；
- `DELETE /api/image/library/{asset_id}`：删除（本人或管理员）。

口径：素材库图**只是参考图的一种来源**，不参与颜色锁定（颜色以中台 attrs 为准）。
"""

from __future__ import annotations

import base64
import binascii
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.agents_image.api.common import asset_payload, error_response
from app.agents_image.api.deps import require_user, require_user_async, trace_id_of
from app.agents_image.config import get_image_settings
from app.agents_image.store import assets as asset_store
from app.core.errors import ImageError
from app.db.engine import session_scope

logger = logging.getLogger("sku-agent.image.api.library")

router = APIRouter(tags=["image-library"])


def _decode(data_b64: str) -> bytes:
    text = str(data_b64 or "").strip()
    if not text:
        raise ImageError("IMG_REFERENCE_INVALID", "请上传图片内容（data_b64）")
    if text.startswith("data:"):                      # 前端 FileReader 的 dataURL
        text = text.split(",", 1)[-1]
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageError("IMG_REFERENCE_INVALID", f"图片 base64 解码失败：{exc}") from exc


def _iso(moment) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S") if moment else ""


@router.post("/library")
async def upload_library_image(request: Request) -> JSONResponse:
    """上传素材图（png/jpeg/webp，单图 ≤ `IMAGE_LIBRARY_MAX_MB`，每人 ≤ `IMAGE_LIBRARY_MAX_IMAGES`）。"""
    trace = trace_id_of(request)
    try:
        user = await require_user_async(request)
        # P1-6（第九轮）：上传也会写盘+读库，单独限速（与建任务不同 scope）
        from app.agents_image.api.ratelimit import check_rate_limit

        check_rate_limit(user.username, "library_upload")
        settings = get_image_settings()
        body = await request.json()
        if not isinstance(body, dict):
            raise ImageError("IMG_INPUT_INVALID", "请求体必须是 JSON 对象")
        raw = _decode(body.get("data_b64") or "")
        max_bytes = int(float(settings.library_max_mb) * 1024 * 1024)
        if len(raw) > max_bytes:
            raise ImageError("IMG_REFERENCE_INVALID",
                             f"单张素材图不能超过 {settings.library_max_mb} MB"
                             f"（当前 {len(raw) / 1024 / 1024:.1f} MB）")
        def _save_asset() -> tuple[str, dict]:
            with session_scope() as session:
                used = asset_store.count_library(session, user.username)
                limit = int(settings.library_max_images)
                already = asset_store.find_library_by_sha(session, user.username, raw)
                if already is not None:
                    # 重复上传同一张图：复用已有素材（不占额度、不产生重复行，P2-10）
                    payload = asset_payload(already)
                    payload.update({"username": already.username,
                                    "created_at": _iso(already.created_at),
                                    "deduped": True})
                    return "deduped", payload
                if used >= limit and not user.is_admin:
                    raise ImageError("IMG_LIBRARY_FULL",
                                     f"素材库已满（每人最多 {limit} 张），请先删除不用的图")
                # P2-6（第八轮）：**不信任请求里的 mime**，一律按字节嗅探（LocalStore.put 会
                # sniff_mime，认不出 png/jpeg/webp 就 422），否则伪造扩展名可绕过白名单。
                asset = asset_store.save_bytes(
                    session, raw, kind="library", mime="",
                    username=user.username, department=user.department,
                    source="upload",
                )
                payload = asset_payload(asset)
                payload.update({"username": asset.username,
                                "created_at": _iso(asset.created_at)})
                return "created", payload

        # P1-3：上传的同步 DB/写盘丢线程池
        from app.observability.trace import run_in_trace

        outcome, payload = await run_in_trace(_save_asset)
        payload["trace_id"] = trace
        if outcome == "deduped":
            return JSONResponse(status_code=200, content=payload)
    except ImageError as exc:
        return error_response(exc, trace)
    except Exception as exc:  # noqa: BLE001
        logger.exception("素材库上传失败")
        return error_response(exc, trace)
    return JSONResponse(status_code=201, content=payload)


@router.get("/library")
def list_library_images(request: Request) -> JSONResponse:
    """素材库列表（本人；管理员可 `?scope=all` 看全部账号）。"""
    trace = trace_id_of(request)
    try:
        user = require_user(request)
        include_all = user.is_admin and \
            (request.query_params.get("scope") or "").strip().lower() == "all"
        settings = get_image_settings()
        with session_scope() as session:
            rows = asset_store.list_library(
                session, username=None if include_all else user.username, limit=200)
            items = []
            for asset in rows:
                item = asset_payload(asset)
                item.update({"username": asset.username,
                             "created_at": _iso(asset.created_at)})
                items.append(item)
        payload = {"items": items, "count": len(items), "trace_id": trace,
                   "limits": {"max_mb": settings.library_max_mb,
                              "max_images": settings.library_max_images}}
    except ImageError as exc:
        return error_response(exc, trace)
    except Exception as exc:  # noqa: BLE001
        logger.exception("素材库列表失败")
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content=payload)


@router.delete("/library/{asset_id}")
def delete_library_image(asset_id: str, request: Request) -> JSONResponse:
    """删除素材图（本人或管理员）；已引用它的历史任务不受影响（refs 仍可读）。"""
    trace = trace_id_of(request)
    try:
        user = require_user(request)
        with session_scope() as session:
            asset = asset_store.get_asset(session, asset_id)
            if asset is None or asset.kind != "library":
                raise ImageError("IMG_TASK_NOT_FOUND", f"素材图不存在：{asset_id}")
            if not user.is_admin and asset.username != user.username:
                raise ImageError("IMG_FORBIDDEN", "只能删除自己上传的素材图")
            asset_store.delete_asset(session, asset)
        payload = {"deleted": True, "asset_id": asset_id, "trace_id": trace}
    except ImageError as exc:
        return error_response(exc, trace)
    except Exception as exc:  # noqa: BLE001
        logger.exception("素材库删除失败")
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content=payload)


__all__ = ["router"]
