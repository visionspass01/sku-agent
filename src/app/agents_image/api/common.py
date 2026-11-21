"""HTTP 层共用的小工具（错误响应、资产序列化），供 routes / auth_routes 复用。"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from app.core.errors import error_headers, failure_payload, normalize

# 业务部门白名单（第五轮 P0-5）：普通账号必须落在其中一个部门，缺部门一律拒绝出图
DEPARTMENTS = ("男装", "女装")

# 终态集合（第四轮 P1-9）：只有终态才谈"部分成功"，排队/运行中一律 in_progress
TERMINAL_STATUSES = frozenset({"done", "failed", "canceled"})


def is_terminal(status: str | None) -> bool:
    return str(status or "") in TERMINAL_STATUSES


def error_response(exc: BaseException, trace_id: str) -> JSONResponse:
    """统一错误响应：body 带 error_code/trace_id/retryable，头带 X-Error-Code/X-Trace-Id。

    P2-4（第十轮）：限流类错误在 `extra.retry_after` 里带等待秒数 → 出 `Retry-After` 头。
    """
    err = normalize(exc, trace_id)
    headers = error_headers(err, trace_id)
    retry_after = (err.extra or {}).get("retry_after")
    if retry_after is not None:
        headers["Retry-After"] = str(max(1, int(retry_after)))
    return JSONResponse(status_code=err.status, content=failure_payload(err, trace_id),
                        headers=headers)


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
            # 第十二轮：remote 模式下图片落在 COS / 中台文件夹，前端/调用方需要知道落点
            "storage": getattr(asset, "storage", "local") or "local",
            "storage_key": getattr(asset, "key", "") or "",
            "adopt_status": getattr(asset, "adopt_status", "pending"),
            "reused": getattr(asset, "source", "") == "reuse",
            "blocked_reason": getattr(asset, "blocked_reason", ""),
            "purged": bool(getattr(asset, "purged_at", None))}


__all__ = ["error_response", "asset_payload", "signed_asset_url",
           "verify_asset_signature"]
