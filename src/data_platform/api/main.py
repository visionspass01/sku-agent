"""数据中台 FastAPI 服务（:8001）。

治理入口在中间件里（认证 → 配额 → 审计），路由只负责"业务 + scope/部门校验"：
- 身份与 scope：`data_platform/api/authz.py`
- 按调用方限流：`data_platform/api/quota.py`
- 审计日志：`data_platform/api/audit.py`

这样"谁调了、允许吗、超配额吗、结果如何"都不再依赖调用方自觉（早先只有一个共享令牌，
中台不知道调用方是谁，部门隔离与配额只能写在每个 Agent 里）。
"""

from __future__ import annotations

import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from data_platform.api import audit, quota
from data_platform.api.agents import router as agents_router
from data_platform.api.authz import resolve_principal
from data_platform.api.routes import router
from data_platform.config import ensure_dirs
from data_platform.contract import CONTRACT_HEADER, CONTRACT_VERSION, DATA_VERSION_HEADER, compatible
from data_platform.db import init_schema

# 探活接口不要求令牌：compose healthcheck 与 Agent 启动预热都用它
PUBLIC_PATHS = frozenset({"/api/v1/health"})

# 声明式调用方标识的合法形态（共享令牌时才允许用它归因，见 `_audit_agent_id`）
_DECLARED_AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


def _audit_agent_id(request: Request, principal) -> str:  # noqa: ANN001
    """审计里的「谁」。

    专属 key（注册表 / PLATFORM_API_KEYS）→ 用 key 对应的 agent_id（可信、不可冒充）。
    **共享 legacy 令牌** → 允许用 `X-Agent-Id` 声明式归因，否则同一个 MCP/脚本通道的调用
    在中台眼里全是 `legacy-internal`，"谁调了什么"就答不出来（早先正是这个问题）。
    防冒充：只接受合法形态的标识，且真实主体会写进 `detail`。
    """
    agent_id = getattr(principal, "agent_id", "") or ""
    if getattr(principal, "source", "") != "legacy":
        return agent_id
    declared = (request.headers.get("X-Agent-Id") or "").strip().lower()
    if declared and _DECLARED_AGENT_RE.match(declared):
        return declared
    return agent_id


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    init_schema()
    yield


app = FastAPI(title="轻量电商数据中台", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def governance(request: Request, call_next):
    """认证 → 配额 → 放行/拒绝 → 审计（所有 /api/v1/* 调用都会留痕）。"""
    started = time.perf_counter()
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    request.state.request_id = request_id
    path = request.url.path
    is_api = path.startswith("/api/v1/")
    public = path in PUBLIC_PATHS

    principal = resolve_principal(request) if is_api else None
    if is_api and not public:
        if principal is None:
            response = JSONResponse(
                {"detail": "缺少或无效的中台调用令牌（X-Internal-Token / X-Api-Key）"},
                status_code=401,
            )
            return _finish(request, response, started, request_id,
                           agent_id="", scopes=(), decision="unauthenticated")
        try:
            state = quota.check(principal)
        except Exception as exc:      # noqa: BLE001 —— HTTPException(429) 也要审计
            response = JSONResponse({"detail": getattr(exc, "detail", str(exc))}, status_code=429)
            for name, value in (getattr(exc, "headers", None) or {}).items():
                response.headers[name] = value
            return _finish(request, response, started, request_id,
                           agent_id=principal.agent_id, scopes=principal.scopes,
                           decision="rate_limited", detail=f"limit={principal.rate_limit}",
                           audit_agent_id=_audit_agent_id(request, principal))
        request.state.rate_state = state

    response = await call_next(request)

    if is_api and not public and principal is not None:
        headers = getattr(response, "headers", None)
        if headers is not None:
            if "X-RateLimit-Limit" not in headers:
                for name, value in quota.headers_of(getattr(request.state, "rate_state", {})
                                                    or {"limit": principal.rate_limit,
                                                        "remaining": 0}).items():
                    headers[name] = value
    trusted_id = getattr(principal, "agent_id", "")
    audit_id = _audit_agent_id(request, principal) if principal is not None else ""
    extra_detail = getattr(request.state, "audit_detail", "")
    if audit_id and audit_id != trusted_id:
        # 声明式归因时把真实主体留下：审计追责用的是 trusted_id，不能因为自报名字就找不到人
        extra_detail = f"principal={trusted_id} {extra_detail}".strip()
    return _finish(request, response, started, request_id,
                   agent_id=trusted_id,
                   scopes=getattr(principal, "scopes", ()),
                   decision=getattr(request.state, "audit_decision", ""),
                   detail=extra_detail, audit_agent_id=audit_id)


def _finish(request: Request, response, started: float, request_id: str, *, agent_id: str,
            scopes, decision: str = "", detail: str = "",
            audit_agent_id: str | None = None):  # noqa: ANN001, ANN002, ANN003
    """回填关联 id / 契约版本 / 数据版本，按状态码推导决策，写审计。"""
    latency_ms = int((time.perf_counter() - started) * 1000)
    status = int(getattr(response, "status_code", 500))
    headers = getattr(response, "headers", None)
    if headers is not None:
        headers["X-Request-Id"] = request_id
        if agent_id:
            headers["X-Agent-Id"] = agent_id
        # 契约版本：调用方据此判断自己是否还能解析这个响应（见 data_platform/contract.py）
        headers[CONTRACT_HEADER] = CONTRACT_VERSION
        data_version = getattr(request.state, "data_version", None)
        if data_version is not None:
            headers[DATA_VERSION_HEADER] = str(data_version)
        client_version = request.headers.get("X-Agent-Contract", "")
        if client_version and not compatible(client_version):
            headers["X-Platform-Contract-Warning"] = (
                f"调用方契约 {client_version} 与中台 {CONTRACT_VERSION} 主版本不一致"
            )
    if request.url.path.startswith("/api/v1/") and request.url.path not in PUBLIC_PATHS:
        if not decision:
            if status == 401:
                decision = "unauthenticated"
            elif status == 429:
                decision = "rate_limited"
            elif status == 403:
                decision = "denied_department" if detail else "denied_scope"
            elif status == 404:
                decision = "not_found"
            elif status == 409:
                decision = "conflict"
            elif status >= 500:
                decision = "error"
            else:
                decision = "allow"
        audit.record(agent_id=audit_agent_id if audit_agent_id is not None else agent_id,
                     scopes=scopes, method=request.method,
                     path=request.url.path, status=status, latency_ms=latency_ms,
                     request_id=request_id, decision=decision, detail=detail)
    return response


app.include_router(router)
app.include_router(agents_router)
