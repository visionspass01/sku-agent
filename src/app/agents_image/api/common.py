"""HTTP 层共用的小工具（错误响应、资产序列化），供 routes / auth_routes 复用。"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from app.core.errors import error_headers, failure_payload, normalize


def error_response(exc: BaseException, trace_id: str) -> JSONResponse:
    """统一错误响应：body 带 error_code/trace_id/retryable，头带 X-Error-Code/X-Trace-Id。"""
    err = normalize(exc, trace_id)
    return JSONResponse(status_code=err.status, content=failure_payload(err, trace_id),
                        headers=error_headers(err, trace_id))


def signed_asset_url(asset_id: str, *, ttl_seconds: int = 300) -> str:
    """资产短签名 URL（M7）：给 <img src> 用，省掉"每张图先发一次 401 请求再取 blob"。"""
    import hashlib
    import hmac
    import time

    from app.agents_image.config import get_image_settings

    exp = int(time.time()) + max(30, int(ttl_seconds))
    body = f"{asset_id}:{exp}"
    sig = hmac.new(get_image_settings().auth_secret.encode("utf-8"),
                   body.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"/api/image/assets/{asset_id}?exp={exp}&sig={sig}"


def verify_asset_signature(asset_id: str, exp: str, sig: str) -> bool:
    import hashlib
    import hmac
    import time

    from app.agents_image.config import get_image_settings

    try:
        moment = int(exp or 0)
    except (TypeError, ValueError):
        return False
    if moment < int(time.time()):
        return False
    expected = hmac.new(get_image_settings().auth_secret.encode("utf-8"),
                        f"{asset_id}:{moment}".encode("utf-8"),
                        hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expected, sig or "")


def asset_payload(asset) -> dict:
    return {"asset_id": asset.asset_id, "slot": asset.slot, "kind": asset.kind,
            "mime": asset.mime, "width": asset.width, "height": asset.height,
            "bytes": asset.bytes, "sha256": asset.sha256,
            "url": f"/api/image/assets/{asset.asset_id}",
            "signed_url": signed_asset_url(asset.asset_id),
            "adopt_status": getattr(asset, "adopt_status", "pending"),
            "blocked_reason": getattr(asset, "blocked_reason", ""),
            "purged": bool(getattr(asset, "purged_at", None))}


__all__ = ["error_response", "asset_payload", "signed_asset_url",
           "verify_asset_signature"]
