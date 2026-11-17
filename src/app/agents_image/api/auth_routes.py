"""生图 Agent 的登录 / 登出 / 当前用户（独立账号体系，与 SKU 无关）。

- `POST /api/image/auth/login`  {username, password} → {token, expires_in, user}
- `POST /api/image/auth/logout`  Bearer → 把 jti 记入黑名单（立即失效）
- `GET  /api/image/auth/me`      Bearer → 当前账号信息
登录失败按「用户名 + IP」限流（默认 8 次 / 5 分钟），超限返回 429 + Retry-After。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.agents_image.api.common import error_response
from app.agents_image.api.deps import client_ip, require_user, trace_id_of
from app.agents_image.auth import AuthUser, get_throttle, issue_token, parse_token
from app.agents_image.config import get_image_settings
from app.agents_image.store import tokens as token_store
from app.agents_image.store import users as user_store
from app.core.errors import ImageError
from app.db.engine import session_scope

logger = logging.getLogger("sku-agent.image.auth")

router = APIRouter(tags=["image-auth"])   # 由 `api/routes.py` 以 `/api/image` 前缀挂载


def _user_payload(user: AuthUser) -> dict:
    return {"username": user.username, "role": user.role, "department": user.department,
            "is_admin": user.is_admin}


@router.post("/auth/login")
async def login(request: Request) -> JSONResponse:
    trace = trace_id_of(request)
    settings = get_image_settings()
    ip = client_ip(request)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ImageError("IMG_INPUT_INVALID", "请求体必须是 JSON 对象")
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        if not username or not password:
            raise ImageError("IMG_INPUT_INVALID", "用户名和密码都不能为空")

        throttle = get_throttle()
        allowed, retry_after = throttle.allowed(username, ip)
        if not allowed:
            exc = ImageError("IMG_LOGIN_THROTTLED",
                             f"登录失败次数过多，请 {retry_after} 秒后再试", retryable=True)
            response = error_response(exc, trace)
            response.headers["Retry-After"] = str(retry_after)
            return response

        with session_scope() as session:
            row = user_store.authenticate(session, username, password)
            snapshot = None
            if row is not None:
                snapshot = {"username": row.username, "role": row.role or "user",
                            "department": row.department}
        if snapshot is None:
            throttle.record_failure(username, ip)
            logger.info("生图登录失败：user=%s ip=%s trace=%s", username, ip, trace)
            return error_response(ImageError("IMG_UNAUTHORIZED", "用户名或密码不正确"), trace)

        throttle.reset(username, ip)
        user = AuthUser(username=snapshot["username"], role=snapshot["role"],
                        department=snapshot["department"])
        token = issue_token(user)
        payload = {
            "token": token, "token_type": "Bearer",
            "expires_in": int(settings.token_ttl_hours * 3600),
            "user": _user_payload(user), "trace_id": trace,
            "secret_is_default": settings.secret_is_default,
        }
        if settings.secret_is_default:
            logger.warning("IMAGE_AUTH_SECRET 仍是默认值，上线前必须改掉")
    except ImageError as exc:
        return error_response(exc, trace)
    except Exception as exc:  # noqa: BLE001
        logger.exception("生图登录异常")
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content=payload)


@router.post("/auth/logout")
def logout(request: Request) -> JSONResponse:
    trace = trace_id_of(request)
    try:
        user = require_user(request)
        with session_scope() as session:
            token_store.revoke(session, user.jti, user.exp, reason="logout")
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={"ok": True, "trace_id": trace})


@router.get("/auth/me")
def me(request: Request) -> JSONResponse:
    trace = trace_id_of(request)
    try:
        user = require_user(request)
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={**_user_payload(user), "trace_id": trace})


@router.post("/auth/verify")
def verify(request: Request) -> JSONResponse:
    """轻量校验：只验签名/过期，不查库（前端刷新时先用它判断是否要弹登录卡片）。"""
    trace = trace_id_of(request)
    try:
        from app.agents_image.auth import bearer_from_header

        parse_token(bearer_from_header(request.headers.get("Authorization")))
    except ImageError as exc:
        return error_response(exc, trace)
    return JSONResponse(status_code=200, content={"ok": True, "trace_id": trace})
