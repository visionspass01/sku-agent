"""FastAPI 路由：登录鉴权、部门数据域、运营查询、巡检触发、RAG 检索、指标与队列状态。

权限模型：
- 公开：/api/health、/api/auth/login；
- 需登录：其余接口；部门用户的数据域 = 本部门品类（管理员无限制）；
- 管理员点击卡片可用 metrics.departments 查看各部门明细。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy import select

from app.api.schemas import (
    FollowupRequest,
    InspectRequest,
    InspectStartResponse,
    LoginRequest,
    LoginResponse,
    PatrolRequest,
    PatrolResponse,
    RunStatusResponse,
    SecurityRecordsResponse,
)
from app.auth import (
    AuthUser,
    authenticate,
    clear_login_failures,
    extract_token,
    issue_token,
    login_throttle_state,
    optional_user,
    record_login,
    record_login_failure,
    require_user,
    revoke_token,
    scope_categories,
    security_records,
)
from app.cache.redis_factory import redis_mode
from app.cache.stream_queue import get_stream_queue
from app.cache.ttl_cache import get_ttl_cache
from app.config import get_settings
from app.core import health as health_registry
from app.db import repos
from app.db.engine import session_scope
from app.db.models import PatrolLog
from app.observability import latency
from app.observability import trace as trace_mod
from app.pipeline import progress as patrol_progress
from app.pipeline.metrics import metrics_snapshot
from app.pipeline.patrol import inspect_sku, run_patrol
from app.scheduler import JOBS as SCHEDULER_JOBS
from app.scheduler import next_run_time, scheduler_status
from app.security.scope import kb_department_filter
from app.static_assets import app_features, asset_versions

logger = logging.getLogger("sku-agent")
router = APIRouter(prefix="/api")
_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _client_ip(request: Request) -> str:
    """客户端 IP：优先取反向代理写的 `X-Forwarded-For` 首段，回落到直连地址。

    为什么需要：容器部署时 uvicorn 前面是 nginx（独立容器的非 127.0.0.1 地址），
    只读 `request.client.host` 会把所有请求记成 nginx 内网 IP —— 登录安全记录失真，
    按「账号 + IP」维度的登录限流也无法按真实来源区分。仅在**可信代理之后**使用，
    直连部署（无代理）时请求头不存在，行为与以前一致。
    """
    forwarded = (request.headers.get("x-forwarded-for") or "").strip()
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    real_ip = (request.headers.get("x-real-ip") or "").strip()
    if real_ip:
        return real_ip
    return request.client.host if request.client else "-"


def _dept_filter(user: AuthUser | None, departments=None) -> dict:
    """RAG 检索的部门可见性：匿名/管理员无限制；用户 = 本部门内部 SOP + 通用 SOP。

    实现已收口到 `app.security.scope.kb_department_filter()`（对话工具用同一个函数），
    避免「路由全量、工具只看通用」这类同一规则两处实现的漂移（复审 P1-1）。
    `departments` 是问题部门面：管理员问具体部门时把检索收敛到「该部门 + 通用」（复审 P1）。
    """
    return kb_department_filter(user, departments)


def _ensure_visible(sku_category: str | None, user: AuthUser | None) -> None:
    if user is None:
        return
    cats = scope_categories(user)
    if cats is not None and sku_category not in cats:
        raise HTTPException(404, "SKU 不存在")


def _platform_breaker_state() -> dict:
    """中台熔断状态（health 用）：不可用时也要能报出来，所以吞掉异常。"""
    try:
        from app.tools.platform_tools import get_platform_client

        return get_platform_client().breaker_state()
    except Exception as exc:  # noqa: BLE001
        return {"state": "unknown", "failures": 0, "retry_in_s": 0.0,
                "error": type(exc).__name__}


def _runtime_health() -> dict:
    """进程内状态自述：多副本部署时哪些计数是「按进程」的、调度器该由谁跑。"""
    import os

    return {
        "pid": os.getpid(),
        "in_process_state": [
            "对话限流计数（ChatRATE_LIMIT_PER_MINUTE 每副本独立）",
            "对话并发闸门（CHAT_MAX_CONCURRENCY 每副本独立）",
            "Agent 注册表缓存（每副本各自热加载）",
            "checkpoint 线程计数缓存",
        ],
        "scheduler_note": "定时巡检每个进程各起一个调度器：多副本时必须只让一个副本开 SCHEDULER_ENABLED",
        "multi_replica_requirements": [
            "REDIS_URL：限流/锁/缓存跨副本一致",
            "CHECKPOINT_BACKEND=redis：SQLite 是单写者",
            "SCHEDULER_ENABLED 仅单副本开启",
        ],
    }


_DEDUPE_HEALTH_CACHE: dict[str, object] = {"at": 0.0, "data": None}


def _rag_conflict_health() -> dict:
    """口径冲突的运行时计数（本进程）：ok/none/unavailable/intra/suppressed/skipped。

    为什么要暴露：冲突检测的失败模式几乎都是"静默"——作用域算错、落败片段没召回、
    小节名拼错、语料没填事实，表现都是"看起来没问题"。只有计数能回答
    「最近到底比对了几次、多少次没比对成」。
    """
    try:
        from app.rag import conflict

        stats = conflict.conflict_stats()
        checked = max(1, int(stats.get("checked") or 0))
        # 口径：checked = 全部检索（含被跳过的），resolved = 真的完成比对的（ok / none）
        stats["resolved_rate"] = round(
            (int(stats.get("ok") or 0) + int(stats.get("none") or 0)) / checked, 4)
        stats["note"] = ("本进程累计；checked=检索次数（含 skipped），"
                         "unavailable/skipped=没有可比或相关口径（未比对），"
                         "resolved_rate=(ok+none)/checked")
        return stats
    except Exception as exc:  # noqa: BLE001 —— health 不能因为读不到计数就 500
        return {"checked": 0, "error": type(exc).__name__}


def _rag_facts_health() -> dict:
    """语料侧口径覆盖率：多少文档/事实声明了口径、有几个小节名写错。"""
    try:
        from app.rag.loader import kb_fact_coverage

        return kb_fact_coverage()
    except Exception as exc:  # noqa: BLE001
        return {"documents": 0, "facts": 0, "error": type(exc).__name__}


def _analysis_dedupe_health() -> dict:
    """档案去重/合并状态：唯一索引、历史重复组数、待整合摘要数量。

    带 TTL 缓存（`HEALTH_CACHE_SECONDS`）：`/api/health` 被前端每 3s 轮询，
    每次都反射 schema 属于纯浪费（与 checkpoint 线程计数同一套处理方式）。
    """
    settings = get_settings()
    ttl = max(0.0, float(getattr(settings, "health_cache_seconds", 10.0)))
    now = time.monotonic()
    cached = _DEDUPE_HEALTH_CACHE.get("data")
    if cached is not None and now - float(_DEDUPE_HEALTH_CACHE.get("at") or 0) < ttl:
        return dict(cached)  # type: ignore[arg-type]
    try:
        from app.db.engine import analysis_dedupe_status
        from app.memory import analysis_merge

        status = analysis_dedupe_status()
        status["pending_merges"] = len(analysis_merge.list_pending())
        status["merge_log"] = str(analysis_merge.merge_log_path())
        _DEDUPE_HEALTH_CACHE["at"] = now
        _DEDUPE_HEALTH_CACHE["data"] = dict(status)
        return status
    except Exception as exc:  # noqa: BLE001 —— health 不能因为读不到状态就 500
        return {"unique_index": False, "duplicates": 0, "degraded": False,
                "error": type(exc).__name__}


def _merge_health() -> dict:
    """档案主文档合并的健康度（积压 + SLO）。读不到时返回 error 而不是让 /health 500。"""
    try:
        from app.memory import analysis_merge, merge_stats

        pending = analysis_merge.list_pending(limit=50)
        return merge_stats.snapshot(pending)
    except Exception as exc:  # noqa: BLE001
        return {"degraded": False, "error": f"{type(exc).__name__}: {exc}"}


def _security_health() -> dict:
    """部署自检：密钥是否仍是仓库里的公开默认值（只返回布尔，不泄露具体值）。

    为什么需要：`AUTH_SECRET` 保持默认值时，任何读过仓库的人都能签出合法 Token；
    `PLATFORM_INTERNAL_TOKEN` 保持默认值时，能访问中台端口的人即拥有全权身份。
    容器部署时若 `.env` 放错位置（如只放在仓库根却没带 `--env-file`），
    这两个变量会静默退回默认值——所以把判定结果放进 /api/health 便于上线检查。
    """
    from app.config import DEFAULT_AUTH_SECRET, DEFAULT_PLATFORM_INTERNAL_TOKEN

    settings = get_settings()
    secret_default = settings.auth_secret == DEFAULT_AUTH_SECRET
    token_default = settings.platform_internal_token == DEFAULT_PLATFORM_INTERNAL_TOKEN
    return {
        "secret_is_default": secret_default,
        "platform_token_is_default": token_default,
        "ok": not (secret_default or token_default),
        "note": ("演示默认密钥可在本地使用；对外部署请用 openssl rand -hex 32 覆盖 "
                 "AUTH_SECRET / PLATFORM_INTERNAL_TOKEN，并确认 .env 被 compose 正确读取"
                 if (secret_default or token_default) else ""),
    }


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    from app.agents.chat_agent import chat_limits
    from app.agents.checkpoint import checkpoint_health
    from app.agents.prompt_loader import prompt_versions
    from app.agents.registry import get_registry
    from app.config import department_mapping_status
    from app.mcp_client.client import get_mcp_hub
    from app.observability.otel import otel_status
    from app.rag.embedding import embedding_status
    from app.rag.vector_store import store_status
    from app.tools import catalog as sku_catalog
    from app.tools import platform_contract
    from app.tools.platform_tools import capability_status

    cache_snap = get_ttl_cache().snapshot()
    cache_label = redis_mode() + ("(degraded)" if cache_snap.get("degraded") else "")
    return {
        "status": "ok",
        "time": dt.datetime.now().isoformat(timespec="seconds"),
        "database": "sqlite" if settings.is_sqlite else "mysql",
        "cache": cache_label,
        "llm_model": settings.llm_model,
        "mcp_transport": settings.mcp_transport,
        "scheduler_enabled": settings.scheduler_enabled,
        "scheduler_next_run": next_run_time(),
        "scheduler_note": scheduler_status()["note"],
        # 检索栈（可选组件）：向量库 + embedding 后端，降级时给出原因
        "vector_store": store_status(),
        "embedding": embedding_status(),
        "checkpoint": checkpoint_health(),
        # MCP 连接池 + 熔断状态：回答「这个工具为什么立刻失败」
        "mcp": get_mcp_hub().health(),
        # 中台熔断 + 契约版本：回答「工具报 contract/auth 是什么意思」
        "platform": {"breaker": _platform_breaker_state(),
                     "contract": platform_contract.stats()},
        # 能力目录（工具分组/作用域的唯一真相在中台）：漂移在这里显式暴露
        "capabilities": capability_status(),
        # SKU 参考目录：Agent 侧部门作用域判定用的元数据（不读私有业务表）
        "catalog": sku_catalog.status(),
        # 部门映射（中台为准 + 本地带指纹缓存）
        "departments": department_mapping_status(),
        # 对话并发与频次上限：回答「并发多了怎么办、会不会把上游打爆」
        "limits": chat_limits(),
        # 提示词版本：确认线上跑的是哪一版规则（改了提示词，这里会变）
        "prompts": prompt_versions(),
        # L3 档案：唯一索引是否就位（并发写同结论不重复）+ 待合并（摘要待整合）数量
        "analysis_dedupe": _analysis_dedupe_health(),
        # 档案主文档合并（后台 fire-and-forget）：积压条数、最老年龄、失败率、SLO 判定
        "merge": _merge_health(),
        # 全链路 trace：当前 trace_id + 日志是否已注入 trace 字段
        "trace": trace_mod.trace_status(),
        # 进程内状态自述 + 多副本前置条件（回答「多副本会怎样」）
        "runtime": _runtime_health(),
        # Agent 注册表：多 Agent 热插拔的当前状态（版本/默认/加载错误）
        "agents": get_registry().health(),
        # 标准 trace 导出（可选）：未配 OTLP endpoint 时为 enabled=false
        "observability": otel_status(),
        # 知识库口径冲突：计数（ok/none/unavailable/suppressed）+ 语料覆盖率。
        # 这个机制的失败模式几乎都是"静默"（作用域/召回/小节拼错/未覆盖），
        # 没有这两块就只能靠猜（复审 P2）。
        "rag_conflicts": _rag_conflict_health(),
        "rag_facts": _rag_facts_health(),
        # 部署自检：密钥是否仍是仓库里的公开默认值（便于上线检查，不泄露值本身）
        "security": _security_health(),
        # 前端自检用：后端能力自述 + 静态资源版本（与 index.html 注入的 ?v= 一致）
        "features": app_features(),
        "assets": asset_versions(),
        "latency_window": latency.MAX_SAMPLES,
        # image Agent 等其它模块注册进来的健康段（模块自己负责内容，本路由不反向依赖它们）
        **health_registry.collect(),
    }


# ---------------------------------------------------------------- 认证
@router.post("/auth/login", response_model=LoginResponse)
def login(req: LoginRequest, request: Request) -> LoginResponse:
    ip = _client_ip(request)
    throttle = login_throttle_state(req.username, ip)
    if not throttle["allowed"]:
        # 429 而不是一直 401：把「被限流」这件事说清楚，并给出还需等待的秒数
        raise HTTPException(
            429,
            f"登录尝试过于频繁（{throttle['limit']} 次/{int(throttle['window_s'])}s），"
            f"请 {throttle['retry_after_s']}s 后重试",
            headers={"Retry-After": str(int(throttle["retry_after_s"]) or 1)},
        )
    user = authenticate(req.username, req.password)
    if user is None:
        record_login_failure(req.username, ip)
        with session_scope() as session:
            record_login(session, req.username, ip, request.headers.get("user-agent", ""),
                         result="failed")
        raise HTTPException(401, "用户名或密码错误")
    clear_login_failures(req.username, ip)
    token = issue_token(user)
    with session_scope() as session:
        record_login(session, user.username, ip, request.headers.get("user-agent", ""))
    return LoginResponse(token=token, user=user.to_dict())


@router.get("/auth/me")
def me(user: AuthUser = Depends(require_user)) -> dict:
    return {"user": user.to_dict()}


@router.post("/auth/logout")
def logout(request: Request, user: AuthUser = Depends(require_user)) -> dict:
    revoke_token(extract_token(request))
    return {"status": "ok", "message": "已退出登录"}


@router.get("/auth/security-records", response_model=SecurityRecordsResponse)
def get_security_records(user: AuthUser = Depends(require_user)) -> SecurityRecordsResponse:
    with session_scope() as session:
        records = security_records(session, None if user.is_admin else user.username)
    return SecurityRecordsResponse(
        window_hours=get_settings().security_record_window_hours,
        note="仅保留近 4 小时登录记录",
        records=records,
    )


@router.get("/metrics")
def metrics(user: AuthUser | None = Depends(optional_user)) -> dict:
    return metrics_snapshot(user)


@router.get("/metrics/agents")
def metrics_agents(hours: int = Query(24, ge=1, le=168),
                   user: AuthUser = Depends(require_user)) -> dict:
    """per-agent 运行画像：轮次 / 延迟分位 / token 与成本 / 工具调用 / 降级率 / grounding。

    回答的是「多 Agent 一起在线时，谁慢、谁贵、谁老降级」——这是多 Agent 场景最常被问、
    而单 Agent 时代最容易漏掉的一块。部门用户只能看到本部门账号产生的轮次。
    """
    from app.agents.chat_store import agent_turn_stats

    visible = None if user.is_admin else {user.department}
    with session_scope() as session:
        stats = agent_turn_stats(session, hours=hours,
                                 departments=None if visible is None else sorted(visible))
    stats["scope"] = {"role": "admin" if user.is_admin else "user",
                      "department": user.department}
    return stats


@router.get("/metrics/trajectory")
def metrics_trajectory(hours: int = Query(24, ge=1, le=168),
                       user: AuthUser = Depends(require_user)) -> dict:
    """Trajectory 指标（E6）：每轮工具数/步数、重复调用率、预算利用率、循环中止率、多轮成功率。

    回答的是「Agent 是不是在瞎调工具 / 卡住了 / 追问到底成不成」——单看成功率或延迟
    看不出来的那一层。数据来自 `chat_turn_log`（含中止轮次的中止台账），纯统计、零 token。
    部门用户只看到本部门账号产生的轮次。
    """
    from app.agents import trajectory
    from app.agents.chat_store import filter_rows_for_departments, load_turn_rows
    from app.observability import tool_stats

    visible = None if user.is_admin else sorted({user.department})
    with session_scope() as session:
        rows = filter_rows_for_departments(load_turn_rows(session, hours=hours), visible)
    data = trajectory.aggregate(rows, hours=hours)
    # 进程内计数器与持久化的「中止台账」是两份口径，并列展示（重启后前者清零、后者不清）
    overall = (tool_stats.snapshot().get("overall") or {})
    data["process"] = {
        "loop_aborted_since_start": int((overall.get("outcomes") or {}).get("loop_aborted") or 0),
        "note": "进程内计数：重启清零；跨重启请用 aborted_turns（来自中止台账）",
    }
    data["scope"] = {"role": "admin" if user.is_admin else "user",
                     "department": user.department}
    return data


@router.get("/skus")
def list_skus(
    q: str | None = None,
    category: str | None = None,
    limit: int = Query(50, le=200),
    user: AuthUser | None = Depends(optional_user),
) -> dict:
    cats = scope_categories(user)
    with session_scope() as session:
        rows = repos.list_skus(session, q=q, category=category, limit=limit, categories=cats)
        items = [
            {
                "sku_id": s.sku_id, "style_id": s.style_id, "style_name": s.style_name,
                "category": s.category, "season": s.season, "color": s.color, "size": s.size,
                "warehouse_id": s.warehouse_id, "platform": s.platform,
                "price": s.price, "stock": s.stock, "return_rate": s.return_rate,
            }
            for s in rows
        ]
    return {"count": len(items), "items": items}


@router.get("/skus/{sku_id}")
def get_sku(sku_id: str, user: AuthUser | None = Depends(optional_user)) -> dict:
    with session_scope() as session:
        sku = repos.get_sku(session, sku_id)
        if sku is None:
            raise HTTPException(404, f"SKU 不存在: {sku_id}")
        _ensure_visible(sku.category, user)
        anomaly = repos.get_anomaly(session, sku_id)
        result = repos.get_result_by_sku(session, sku_id)
        latest = repos.latest_stat_hour(session)
        recent = (
            repos.get_stat_hours(
                session, sku_id,
                latest - dt.timedelta(days=7), latest,
            )
            if latest else []
        )
        payload = {
            "sku": {
                "sku_id": sku.sku_id, "style_id": sku.style_id, "style_name": sku.style_name,
                "category": sku.category, "season": sku.season, "color": sku.color,
                "size": sku.size, "warehouse_id": sku.warehouse_id, "platform": sku.platform,
                "price": sku.price, "stock": sku.stock, "return_rate": sku.return_rate,
                "supplier_id": sku.supplier_id,
            },
            "anomaly": None if anomaly is None else {
                "hour": anomaly.hour.isoformat(timespec="minutes"),
                "severity": anomaly.severity,
                "reasons": anomaly.reasons,
            },
            "latest_result": None if result is None else {
                "run_id": result.run_id,
                "risk_level": result.risk_level,
                "plan": result.plan,
                "verifier": result.verifier,
                "degraded": result.degraded,
                "created_at": result.created_at.isoformat(timespec="seconds"),
            },
            "sales_last_7d": [{"hour": h.isoformat(timespec="minutes"), "qty": q} for h, q in recent],
        }
    return payload


@router.get("/anomalies")
def list_anomalies(limit: int = Query(100, le=500), user: AuthUser | None = Depends(optional_user)) -> dict:
    with session_scope() as session:
        rows = repos.list_anomalies(session, limit=limit, categories=scope_categories(user))
        items = [
            {
                "sku_id": a.sku_id,
                "hour": a.hour.isoformat(timespec="minutes"),
                "severity": a.severity,
                "reasons": a.reasons,
            }
            for a in rows
        ]
    return {"count": len(items), "items": items}


@router.post("/inspect", response_model=InspectStartResponse)
def inspect(req: InspectRequest, background: BackgroundTasks, user: AuthUser = Depends(require_user)) -> InspectStartResponse:
    with session_scope() as session:
        sku = repos.get_sku(session, req.sku_id)
        if sku is None:
            raise HTTPException(404, f"SKU 不存在: {req.sku_id}")
        _ensure_visible(sku.category, user)
        run = repos.create_run(session, kind="single", params={"sku_id": req.sku_id, "as_of": req.as_of})
        run_id = run.id

    async def _run() -> None:
        result = await inspect_sku(req.sku_id, run_id, as_of=req.as_of)
        with session_scope() as session:
            repos.finish_run(
                session, run_id,
                status=result["status"] if result["status"] != "failed" else "failed",
                stats={"elapsed_ms": result["elapsed_ms"], "sku_id": req.sku_id},
                error=result.get("error"),
            )

    background.add_task(_run)
    return InspectStartResponse(run_id=run_id)


@router.post("/inspect/followup")
async def inspect_followup(req: FollowupRequest,
                           user: AuthUser = Depends(require_user)) -> dict:
    """多轮追问：复用同一分析 thread 的上一轮采集数据，只重跑研判（不重复调 MCP）。

    与 `/inspect` 的区别：`/inspect` 会从零跑完整链路（采集→Skill→研判→校验）；
    追问直接用 checkpointer 里的上一轮 state，从 refine 节点进入，1~2s 出结论。
    """
    from app.pipeline.patrol import ThreadAccessError, ThreadNotFoundError, followup_sku

    with session_scope() as session:
        sku = repos.get_sku(session, req.sku_id)
        if sku is None:
            raise HTTPException(404, f"SKU 不存在: {req.sku_id}")
        _ensure_visible(sku.category, user)
    try:
        result = await followup_sku(req.sku_id, req.instruction, thread_id=req.thread_id)
    except ThreadAccessError as exc:
        # 线程与请求 SKU 不一致：既可能是调用方拼错了 thread_id，也可能是越权探测，一律 403
        raise HTTPException(403, str(exc)) from exc
    except ThreadNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    final = result.get("final_state") or {}
    plan = final.get("plan") or {}
    return {
        "thread_id": result["thread_id"],
        "sku_id": result["sku_id"],
        "instruction": result["instruction"],
        "status": result.get("status"),
        "elapsed_ms": result.get("elapsed_ms"),
        "plan": plan,
        "verification": final.get("verification") or {},
        "trace": final.get("trace") or [],
        "note": "追问复用了上一轮采集数据（未重新调用 MCP）；如需最新数据请重新发起研判",
    }


@router.get("/inspections/{run_id}", response_model=RunStatusResponse)
def get_inspection(run_id: str, user: AuthUser | None = Depends(optional_user)) -> RunStatusResponse:
    with session_scope() as session:
        run = repos.get_run(session, run_id)
        if run is None:
            raise HTTPException(404, f"巡检任务不存在: {run_id}")
        results = repos.get_results(session, run_id, categories=scope_categories(user))
        items = [
            {
                "sku_id": r.sku_id,
                "risk_level": r.risk_level,
                "plan": r.plan,
                "citations": r.citations,
                "verifier": r.verifier,
                "data_bundle": r.data_bundle,
                "degraded": r.degraded,
                "elapsed_ms": r.elapsed_ms,
                "created_at": r.created_at.isoformat(timespec="seconds"),
            }
            for r in results
        ]
        payload = RunStatusResponse(
            run_id=run.id, kind=run.kind, status=run.status, stats=run.stats or {},
            started_at=run.started_at.isoformat(timespec="seconds") if run.started_at else None,
            finished_at=run.finished_at.isoformat(timespec="seconds") if run.finished_at else None,
            error=run.error, results=items,
            progress=patrol_progress.get(run.id) or {},
        )
    return payload


@router.post("/patrol", response_model=PatrolResponse)
async def patrol(req: PatrolRequest, user: AuthUser = Depends(require_user)) -> PatrolResponse:
    department = None if user.is_admin else user.department
    with session_scope() as session:
        run = repos.create_run(session, kind=req.mode, params={"mode": req.mode, "limit": req.limit,
                                                               "department": department})
        run_id = run.id

    async def _run() -> None:
        try:
            summary = await run_patrol(
                mode=req.mode, limit=req.limit, run_id=run_id, department=department,
                operator=user.username,
            )
            with session_scope() as session:
                repos.finish_run(session, run_id, status=summary.get("run_status", "ok"), stats=summary)
        except Exception as exc:  # 让任务状态可见，而不是静默丢异常
            logger.exception("巡检任务失败 run_id=%s", run_id)
            with session_scope() as session:
                repos.finish_run(session, run_id, status="failed", stats={}, error=f"{type(exc).__name__}: {exc}")

    _spawn(_run())
    return PatrolResponse(run_id=run_id)


@router.post("/patrol/{run_id}/cancel")
def cancel_patrol(run_id: str, user: AuthUser = Depends(require_user)) -> dict:
    """协作式终止巡检：当前批次结束后停止，输出已巡检/未巡检与中断原因。"""
    with session_scope() as session:
        run = repos.get_run(session, run_id)
        if run is None:
            raise HTTPException(404, f"巡检任务不存在: {run_id}")
        if run.status != "running":
            raise HTTPException(409, f"巡检任务状态为 {run.status}，不可终止")
    if not patrol_progress.request_stop(run_id):
        raise HTTPException(409, "巡检任务未处于可终止状态（可能已结束）")
    return {"run_id": run_id, "status": "terminating",
            "message": "已发送终止信号，将在当前批次结束后停止巡检"}


@router.get("/patrol/recent")
def recent_patrol_logs(limit: int = Query(10, le=50), user: AuthUser = Depends(require_user)) -> dict:
    """最近巡检日志与简报（管理员看全部；部门用户看本部门或自己发起的巡检）。"""
    with session_scope() as session:
        stmt = select(PatrolLog).order_by(PatrolLog.created_at.desc()).limit(limit)
        if not user.is_admin:
            stmt = stmt.where(
                (PatrolLog.department == user.department) | (PatrolLog.operator == user.username)
            )
        rows = session.scalars(stmt).all()
        items = [
            {
                "run_id": r.run_id, "operator": r.operator, "mode": r.mode,
                "department": r.department,
                "screened": r.screened, "skipped_recent": r.skipped_recent,
                "candidates": r.candidates, "selected": r.selected,
                "processed": r.processed, "ok": r.ok, "degraded": r.degraded,
                "failed": r.failed, "elapsed_ms": r.elapsed_ms,
                "report": r.report or {},
                "created_at": r.created_at.isoformat(timespec="seconds"),
            }
            for r in rows
        ]
    return {"count": len(items), "items": items}


@router.get("/patrol/{run_id}", response_model=RunStatusResponse)
def get_patrol(run_id: str, user: AuthUser | None = Depends(optional_user)) -> RunStatusResponse:
    resp = get_inspection(run_id, user)
    with session_scope() as session:
        log = session.scalar(select(PatrolLog).where(PatrolLog.run_id == run_id))
        if log is not None:
            resp.report = log.report or {}
    return resp



@router.get("/rag/search")
async def rag_search(
    q: str = Query(..., min_length=1),
    topk: int = Query(5, le=10),
    backend: str = Query("", description="覆盖重排后端：none/heuristic/llm/cross（消融与排障用）"),
    user: AuthUser | None = Depends(optional_user),
) -> dict:
    """知识库检索：走统一 pipeline（改写 → RRF 混合召回 → 重排 → 多样性 → 引用）。

    与对话里的 `search_knowledge` 工具**同一条链路**（同一份缓存），不再各写一套。
    """
    from app.rag.facets import query_departments
    from app.rag.pipeline import search_kb
    from app.security.scope import kb_scope_label

    departments = query_departments(q)
    filters = _dept_filter(user, departments)
    result = await search_kb(q, filters=filters, topk=topk, backend=backend or None)
    payload = result.to_dict()
    # 与工具 search_knowledge 的 scope_used 同一句话（复审 P2-2：文案收口到 scope 模块）
    payload["scope"] = kb_scope_label(user, departments)
    payload["cached"] = bool(result.diagnostics.get("cached"))
    return payload


# ---------------------------------------------------------------- 定时任务（Demo 默认关闭，手动可演练）
@router.get("/scheduler/status")
def get_scheduler_status(user: AuthUser | None = Depends(optional_user)) -> dict:
    """定时任务状态：是否启用、job 列表与下一次运行时间（只读，匿名可看）。"""
    return scheduler_status()


@router.post("/scheduler/run-once")
async def run_scheduler_once(
    job: str = Query("daily", pattern="^(hourly|daily)$"),
    limit: int | None = Query(None, ge=1, le=200),
    user: AuthUser = Depends(require_user),
) -> dict:
    """单次演练定时任务（管理员）：走与 scheduler 相同的 hourly/daily job，但不留定时器。

    Demo 默认关闭定时巡检后，用它可以证明「定时能力还在」，又不产生后台常驻消耗。
    """
    if not user.is_admin:
        raise HTTPException(403, "仅管理员可触发定时任务演练")
    spec = SCHEDULER_JOBS[job]
    with session_scope() as session:
        run = repos.create_run(session, kind=job, params={
            "mode": job, "limit": limit, "department": None, "trigger": f"scheduler-{job}-manual",
        })
        run_id = run.id

    async def _run() -> None:
        try:
            summary = await spec["func"](limit=limit, operator=user.username)
            with session_scope() as session:
                repos.finish_run(
                    session, run_id, status="ok",
                    stats={**summary, "trigger": f"scheduler-{job}-manual"},
                )
        except Exception as exc:  # 让演练失败可见
            logger.exception("定时任务演练失败 job=%s", job)
            with session_scope() as session:
                repos.finish_run(session, run_id, status="failed", stats={},
                                 error=f"{type(exc).__name__}: {exc}")

    _spawn(_run())
    return {"run_id": run_id, "job": job, "status": "running",
            "note": "已按定时任务逻辑在后台执行（等价 Cron 触发），可在巡检日志查看进度"}


@router.get("/queue/stats")
def queue_stats(user: AuthUser | None = Depends(optional_user)) -> dict:
    queue = get_stream_queue()
    return queue.stats()


# ---------------------------------------------------------------- 缓存明细（Agent 旁路 + 中台）
@router.get("/cache/breakdown")
def cache_breakdown(user: AuthUser | None = Depends(optional_user)) -> dict:
    """看板「缓存命中率」卡片点击：Agent 旁路缓存 + 数据中台侧缓存两层明细。

    中台是 best-effort：1.5s 超时，不可用则 platform=None 并给出原因，不影响页面。
    """
    settings = get_settings()
    agent = get_ttl_cache().stats.snapshot()
    platform_cache = None
    platform_error = None
    try:
        with httpx.Client(timeout=1.5) as client:
            resp = client.get(
                f"{settings.platform_api_url.rstrip('/')}/api/v1/health",
            )
            payload = resp.json()
        platform_cache = payload.get("cache_keys")
    except Exception as exc:  # noqa: BLE001
        platform_error = f"{type(exc).__name__}: {exc}"
    return {
        "agent": agent,
        "platform": platform_cache,
        "platform_error": platform_error,
        "ttl": {
            "platform": settings.cache_ttl_platform,
            "mcp": settings.cache_ttl_mcp,
            "rag": settings.cache_ttl_rag,
        },
        "note": "命中率 = 命中 ÷(命中+未命中)；Agent 侧覆盖对话取数（GET）、MCP 工具与 RAG 召回，"
                "中台侧是它自己的 TTL 抖动缓存。",
    }
