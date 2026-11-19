"""统一错误契约：错误码 → HTTP 状态 + `X-Error-Code` / `X-Trace-Id` + SSE `fatal` 载荷。

约定（与 SKU 侧一致）：
- HTTP 响应体：{"detail": "...", "error_code": "IMG_XXX", "trace_id": "...", "retryable": bool}
- 响应头：`X-Error-Code` / `X-Trace-Id`
- SSE：`event: fatal`，data 里带同样字段（前端据此**不降级重发**）
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 错误码 → HTTP 状态。新增错误码时先在这里登记，避免各处散落魔法数字。
IMAGE_CODES: dict[str, int] = {
    "IMG_REFERENCE_REQUIRED": 422,
    "IMG_TOO_MANY_REFS": 422,
    "IMG_REFERENCE_INVALID": 422,
    "IMG_INPUT_INVALID": 422,
    "IMG_BATCH_TOO_LARGE": 422,
    "IMG_REFS_BUDGET": 422,
    "IMG_PRODUCT_NOT_FOUND": 404,
    "IMG_LIBRARY_FULL": 422,
    "IMG_SIMILAR_NOT_FOUND": 422,
    "IMG_UPSTREAM_MCP_UNAVAILABLE": 503,
    "IMG_UPSTREAM_INVALID": 502,
    "IMG_QUOTA_EXCEEDED": 429,
    "IMG_COST_BUDGET_EXCEEDED": 429,
    "IMG_FORBIDDEN": 403,
    "IMG_UNAUTHORIZED": 401,
    "IMG_LOGIN_THROTTLED": 429,
    "IMG_PROVIDER_UNAVAILABLE": 503,
    "IMG_PROVIDER_RATE_LIMITED": 429,
    "IMG_CONTENT_BLOCKED": 422,
    "IMG_SCHEMA_INVALID": 422,
    "IMG_CRITIC_FAILED": 422,
    "IMG_BACKFILL_FAILED": 502,
    "IMG_TASK_NOT_FOUND": 404,
    "IMG_TASK_CONFLICT": 409,
    "IMG_TASK_CANCELED": 409,
    "IMG_RECOVERY_EXHAUSTED": 409,
    "IMG_REFERENCE_EXPIRED": 409,
    "IMG_USER_NOT_FOUND": 404,
    "IMG_TIMEOUT": 504,
    "IMG_INTERNAL": 500,
}


@dataclass
class ImageError(Exception):
    """业务错误：code 必须在 IMAGE_CODES 登记。"""

    code: str
    detail: str = ""
    retryable: bool = False
    trace_id: str = ""
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    @property
    def status(self) -> int:
        return IMAGE_CODES.get(self.code, 500)

    @property
    def message(self) -> str:
        return f"{self.code}: {self.detail}" if self.detail else self.code


def raise_image(code: str, detail: str = "", *, retryable: bool = False,
                trace_id: str = "", **extra: object) -> None:
    raise ImageError(code=code, detail=detail, retryable=retryable,
                     trace_id=trace_id, extra=dict(extra))


def normalize(exc: BaseException, trace_id: str = "") -> ImageError:
    """把任意异常收敛成 ImageError（未知异常一律 IMG_INTERNAL）。"""
    if isinstance(exc, ImageError):
        if trace_id and not exc.trace_id:
            exc.trace_id = trace_id
        return exc
    return ImageError(code="IMG_INTERNAL", detail=f"{type(exc).__name__}: {exc}",
                      retryable=False, trace_id=trace_id)


def failure_payload(exc: BaseException, trace_id: str = "") -> dict:
    err = normalize(exc, trace_id)
    payload = {
        "detail": err.detail or err.code,
        "error_code": err.code,
        "trace_id": err.trace_id or trace_id or "-",
        "retryable": bool(err.retryable),
    }
    if err.extra:
        payload["extra"] = err.extra
    return payload


def error_headers(exc: BaseException, trace_id: str = "") -> dict[str, str]:
    err = normalize(exc, trace_id)
    return {
        "X-Error-Code": err.code,
        "X-Trace-Id": err.trace_id or trace_id or "-",
    }


def sse_fatal(exc: BaseException, trace_id: str = "") -> dict:
    err = normalize(exc, trace_id)
    return {
        "message": err.detail or err.code,
        "status": err.status,
        "error_code": err.code,
        "trace_id": err.trace_id or trace_id or "-",
        "retryable": bool(err.retryable),
        "fatal": True,
    }
