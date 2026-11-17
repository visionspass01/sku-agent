"""HTTP 层共用的小工具（错误响应、资产序列化），供 routes / auth_routes 复用。"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from app.core.errors import error_headers, failure_payload, normalize


def error_response(exc: BaseException, trace_id: str) -> JSONResponse:
    """统一错误响应：body 带 error_code/trace_id/retryable，头带 X-Error-Code/X-Trace-Id。"""
    err = normalize(exc, trace_id)
    return JSONResponse(status_code=err.status, content=failure_payload(err, trace_id),
                        headers=error_headers(err, trace_id))


def asset_payload(asset) -> dict:
    return {"asset_id": asset.asset_id, "slot": asset.slot, "kind": asset.kind,
            "mime": asset.mime, "width": asset.width, "height": asset.height,
            "bytes": asset.bytes, "sha256": asset.sha256,
            "url": f"/api/image/assets/{asset.asset_id}"}


__all__ = ["error_response", "asset_payload"]
