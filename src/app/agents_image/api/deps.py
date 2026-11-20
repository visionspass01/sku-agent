"""生图 API 的依赖：独立账号令牌校验（Bearer）与角色判定。

错误语义：
- 未登录 / 令牌无效 / 过期 / 已登出 → 401 `IMG_UNAUTHORIZED`
- 普通用户访问管理员能力 / 越权看别人数据 → 403 `IMG_FORBIDDEN`
"""

from __future__ import annotations

from fastapi import Request

from app.agents_image.auth import (
    AuthUser,
    bearer_from_header,
    get_throttle,
    parse_token,
)
from app.agents_image.store import tokens as token_store
from app.core.errors import ImageError, raise_image


def _load_user(request: Request) -> AuthUser | None:
    """解析 Bearer → 校验签名/过期/黑名单 → 查账号是否启用；失败抛 401。"""
    cached = getattr(request.state, "image_user", None)
    if cached is not None:
        return cached
    from app.db.engine import session_scope

    token = bearer_from_header(request.headers.get("Authorization"))
    user = parse_token(token)
    with session_scope() as session:
        if token_store.is_revoked(session, user.jti):
            raise_image("IMG_UNAUTHORIZED", "令牌已登出，请重新登录")
        from app.agents_image.store.users import get_user

        row = get_user(session, user.username)
        if row is None or not row.enabled:
            raise_image("IMG_UNAUTHORIZED", "账号不存在或已停用")
        user.role = row.role or user.role
        user.department = row.department
    request.state.image_user = user
    return user


def current_user_optional(request: Request) -> AuthUser | None:
    """没有 Authorization 头 → None；带了但无效 → 401（不静默降级成匿名）。"""
    if not request.headers.get("Authorization"):
        return None
    return _load_user(request)


def require_user(request: Request) -> AuthUser:
    user = current_user_optional(request)
    if user is None:
        raise ImageError("IMG_UNAUTHORIZED", "请先登录（Authorization: Bearer <token>）")
    return user


def require_admin(request: Request) -> AuthUser:
    user = require_user(request)
    if not user.is_admin:
        raise ImageError("IMG_FORBIDDEN", "需要管理员权限")
    return user


def trace_id_of(request: Request) -> str:
    """本次请求的 trace：**优先用中间件已建立的**（响应头/响应体/日志三处必须一致）。

    P1-1（第十轮）：上一版这里各自生成 uuid，中间件又生成一个 → 未带 `X-Trace-Id` 时
    响应头与响应体是两个不同的值，而日志里根本没有（格式化器读 contextvar，没人 set）。
    现在统一以 `request.state.image_trace_id` 为准（中间件负责设置并绑定上下文）。
    """
    import uuid

    cached = str(getattr(request.state, "image_trace_id", "") or "")
    if cached:
        return cached
    trace = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:12]
    request.state.image_trace_id = trace
    return trace


def client_ip(request: Request) -> str:
    """登录限流用：优先 X-Forwarded-For 首跳（容器/nginx 后），否则直连 IP。"""
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    real = (request.headers.get("X-Real-IP") or "").strip()
    if real:
        return real
    return request.client.host if request.client else "-"


__all__ = ["AuthUser", "current_user_optional", "require_user", "require_admin",
           "trace_id_of", "client_ip", "get_throttle"]
