"""ReAct 对话 Agent 与 HITL 审批 API（需登录）。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import weakref
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.agents import chat_store
from app.agents.chat_agent import TurnBudgetExceeded, run_chat, run_chat_stream
from app.agents.tool_guard import InvalidToolCallError, ToolLoopError
from app.auth import AuthUser, require_admin, require_user, scope_categories
from app.cache.lock import redis_lock
from app.config import all_departments, get_settings
from app.db.engine import session_scope
from app.memory import org_memory, session_memory
from app.memory.result_store import list_results
from app.memory.runtime import chat_turn_context, event_sink_context
from app.observability import runtime_state
from app.security.scope import sku_category_map, visible_categories
from app.security.throttle import SlidingWindowCounter
from app.static_assets import app_features
from app.tools.platform_tools import get_platform_client

logger = logging.getLogger("sku-agent.chat")
router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1, max_length=2000)
    # 显式指定 Agent（空=由路由决定：关键词 > plan.intent > 默认）。热插拔：注册表里的 id 可直接用
    agent_id: str = Field(default="", max_length=32)
    include_query_plan: bool = False  # UI「Query 重写」勾选：勾选才在响应里回传本轮计划


class ActionDecision(BaseModel):
    reason: str = ""


class ProfileUpdate(BaseModel):
    """用户偏好画像：只允许白名单字段（output_style/metric_pref/time_window/note）。"""

    prefs: dict | None = None
    watch_skus: list[str] | None = None


class AnalysisPayload(BaseModel):
    """L3 组织记忆：手动沉淀一份分析结论。"""

    question: str = ""
    conclusion: str = Field(min_length=1, max_length=8000)
    title: str = ""
    department: str | None = None
    key_metrics: dict = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    plan_id: str = ""


def _org_scope(user: AuthUser) -> list[str] | None:
    """档案可见范围：管理员 None（全量），部门用户取其品类集合（女装含童装）。

    统一走 `app.security.scope`：这份逻辑原来在 auth / chat_routes / chat_agent /
    platform_tools 各有一份，已经出过「路由过滤了、工具没过滤」的事故。
    """
    return visible_categories(user)


def _sku_categories(sku_ids: list[str]) -> dict[str, str]:
    return sku_category_map(sku_ids)


def _ensure_department(user: AuthUser, sku_id: str) -> None:
    cats = scope_categories(user)
    if cats is None:
        return
    category = _sku_categories([sku_id]).get(sku_id)
    if category is not None and category not in cats:
        raise HTTPException(403, "无权限：该 SKU 不属于当前账号所属部门")


# ---------------------------------------------------------------- 对话
_chat_counter = SlidingWindowCounter()
_global_qps_counter = SlidingWindowCounter()


def chat_throttle_state(username: str) -> dict:
    """每用户每分钟对话轮次限流（滑动窗口）。"""
    settings = get_settings()
    return _chat_counter.state(
        f"chat:{username}",
        limit=max(1, int(settings.chat_rate_limit_per_minute)),
        window=60.0,
    )


def reset_chat_throttle() -> None:
    """测试用。"""
    _chat_counter.reset()
    _global_qps_counter.reset()


def _enforce_global_qps() -> None:
    """全局 QPS 保护（`CHAT_GLOBAL_QPS`，0=关闭）。

    每用户限流挡不住「很多用户同时来」：几百个账号各来 1 次也能把上游打满。
    这里用 1 秒滑窗做整体闸门，超限直接 429（前端会提示稍后再试）。
    """
    qps = max(0, int(getattr(get_settings(), "chat_global_qps", 0) or 0))
    if qps <= 0:
        return
    state = _global_qps_counter.check_and_hit("chat:global", limit=qps, window=1.0)
    if not state["allowed"]:
        raise HTTPException(
            429,
            f"当前对话流量过高（全局 {qps} QPS 上限），请 1~2 秒后重试",
            headers={"Retry-After": "1"},
        )


def _enforce_chat_rate(user: AuthUser) -> None:
    """超过频次上限直接 429，并给出建议等待秒数（前端据此提示「稍后再试」）。

    `check_and_hit` 是**单次持锁**完成的「判断 + 计数」：早先是 state() 再 hit() 两段式，
    单事件循环下靠「中间没有 await」侥幸安全，换成多线程/多 worker 就是超发竞态。
    顺序：先过全局 QPS 闸门（挡整体洪水），再过每用户窗口（挡单账号刷）。
    """
    _enforce_global_qps()
    settings = get_settings()
    state = _chat_counter.check_and_hit(
        f"chat:{user.username}",
        limit=max(1, int(settings.chat_rate_limit_per_minute)),
        window=60.0,
    )
    if not state["allowed"]:
        raise HTTPException(
            429,
            f"对话请求过于频繁（{state['limit']} 次/分钟），请 {state['retry_after_s']}s 后重试",
            headers={"Retry-After": str(int(state["retry_after_s"]) or 1)},
        )


# ---------------------------------------------------------------- 会话级串行
# 为什么需要：同一会话的两轮对话并发跑，会同时 read-modify-write L1 记忆
# （working_set 并集 / 事实卡 / facts_version，见 session_memory.refresh_memory），
# 后提交者静默覆盖前者 —— 前端只有一个 `sending` 标记，挡不住两个标签页或一次重试。
# 这里按会话 id 做互斥：拿不到就直接 409，让「同一会话一次只有一轮」成为接口约定。
_SESSION_LOCKS: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def _session_lock(session_id: str) -> asyncio.Lock:
    lock = _SESSION_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _SESSION_LOCKS[session_id] = lock
    return lock


# 锁 TTL 的余量秒数：覆盖"最后一刻才释放"的边界（LLM 超时 + 重写 + 网络抖动）
SESSION_LOCK_TTL_MARGIN_SECONDS = 30.0


def _session_lock_ttl_ms(settings) -> int:  # noqa: ANN001 —— app.config.Settings
    """会话锁 TTL：必须**覆盖最坏单轮**，否则锁会中途过期（复审 P1-2）。

    早先取 `max(30, LLM_TIMEOUT_SECONDS)`=120s，而单轮硬超时 `CHAT_TURN_TIMEOUT_SECONDS`
    默认 300s。于是多副本下一次慢轮（重试 + 出口重写 + 长流）跑到第 120s 之后，Redis 锁
    自然过期，另一实例就能进入同一会话 → 正是这把锁要防的「L1 记忆双写」。
    单副本还有进程内 `asyncio.Lock` 兜住，所以这个洞只有在多副本 + 慢轮时才暴露，
    也正因为如此，它必须由「配置关系」保证，而不是靠人工记得调。
    """
    budget = max(float(getattr(settings, "llm_timeout_seconds", 0.0) or 0.0),
                 float(getattr(settings, "chat_turn_timeout_seconds", 0.0) or 0.0))
    return int((max(budget, 1.0) + SESSION_LOCK_TTL_MARGIN_SECONDS) * 1000)


class _SessionGuard:
    """可显式 acquire/release 的会话锁（流式接口必须在返回响应**之前**拿到它）。

    为什么不能只用 `async with`：`/chat/stream` 返回的是 `StreamingResponse`，
    一旦流开始，状态码就已经发出去了（冲突只能变成 SSE 里的 error 事件，客户端拿不到 409）。
    所以拆成两步：端点在建流前 acquire（冲突→真实 409），释放交给流结束/后台任务。
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._lock: asyncio.Lock | None = None
        self._redis_cm = None
        self.degraded: str = ""
        self.released = True

    async def acquire(self) -> None:
        settings = get_settings()
        lock = _session_lock(self.session_id)
        if lock.locked():
            raise HTTPException(409, "该会话正在处理上一轮，请稍候再发（同一会话不支持并发对话）")
        await lock.acquire()
        self._lock = lock
        self.released = False
        if not settings.redis_url:
            return
        from app.cache.lock import LockBackendError, LockTimeoutError

        # 两层锁：进程内 asyncio.Lock 保证同一副本串行，Redis 锁保证跨副本串行。
        # 这里必须 await（redis.asyncio）——同步客户端一旦 Redis 抖动，3s 的
        # socket_timeout 会把整个事件循环连同所有 SSE 流一起冻住（评审 v3-①）。
        # TTL 取「最坏单轮 + 余量」，短于单轮上限就等着多副本双写（复审 P1-2）。
        cm = redis_lock(f"lock:chat:{self.session_id}",
                        ttl_ms=_session_lock_ttl_ms(settings),
                        wait_timeout=0)
        try:
            await cm.__aenter__()
        except LockTimeoutError as exc:
            await self.release()
            raise HTTPException(
                409, "该会话正在处理上一轮（可能在其它实例上），请稍候再发"
            ) from exc
        except LockBackendError as exc:
            await self.release()
            # fail-closed 是默认：锁服务不可用时放行，等于放弃"同一会话一次一轮"的保证，
            # 两个标签页同时提问就会互相覆盖 L1 记忆。要放行请显式关掉这一项。
            if getattr(settings, "chat_session_lock_required", True):
                raise HTTPException(
                    503, f"会话锁后端不可用，暂时无法保证同一会话串行：{exc}"
                ) from exc
            self.degraded = str(exc)
            _LOCK_DEGRADED["count"] += 1
            _LOCK_DEGRADED["last"] = str(exc)[:200]
            logger.warning("会话锁降级放行（CHAT_SESSION_LOCK_REQUIRED=false）：%s", exc)
            return
        self._redis_cm = cm

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        if self._redis_cm is not None:
            with contextlib.suppress(Exception):
                await self._redis_cm.__aexit__(None, None, None)
            self._redis_cm = None
        if self._lock is not None and self._lock.locked():
            self._lock.release()
        self._lock = None


@asynccontextmanager
async def _session_guard(session_id: str):
    """兼容旧用法的上下文管理器（内部就是 acquire/release）。"""
    guard = _SessionGuard(session_id)
    await guard.acquire()
    try:
        yield guard
    finally:
        await guard.release()


_LOCK_DEGRADED = {"count": 0, "last": ""}


def session_lock_state() -> dict:
    """会话锁自述（/api/health）：用的哪个后端、是否 fail-closed、降级过几次。

    没有这块信息时，"同一会话并发被放行"这类事故只能靠翻日志猜。
    """
    settings = get_settings()
    backend = "redis" if settings.redis_url else "memory"
    return {
        "backend": backend,
        "required": bool(getattr(settings, "chat_session_lock_required", True)),
        "degraded_events": _LOCK_DEGRADED["count"],
        "last_degraded": _LOCK_DEGRADED["last"],
        "note": ("memory=仅进程内（单副本）；redis=跨副本互斥；"
                 "required=true 时锁后端不可用会直接 503，不放弃串行保证"),
    }


class _QueueSink:
    """把本轮进度事件塞进 asyncio.Queue，供 SSE 生成器消费。"""
    def __init__(self, queue: "asyncio.Queue[tuple[str, dict]]") -> None:
        self.queue = queue
        self.seq = 0

    def emit(self, event: str, payload: dict) -> None:
        self.seq += 1
        body = dict(payload)
        body.setdefault("seq", self.seq)
        try:
            self.queue.put_nowait((event, body))
        except Exception:  # noqa: BLE001 —— 队列满/已关闭都不影响主流程
            pass


class _Turn:
    """一轮对话的「前半段」：会话已解析、用户消息已落库、会话锁已持有。"""

    __slots__ = ("session_id", "history", "guard")

    def __init__(self, session_id: str, history: list[dict], guard: _SessionGuard) -> None:
        self.session_id = session_id
        self.history = history
        self.guard = guard


def _resolve_session(user: AuthUser, req: ChatRequest) -> tuple[str, list[dict]]:
    """同步部分（会在线程里跑）：解析/创建会话 + 取历史（**不**落库）。

    为什么整体丢线程池：这几步都是**同步** SQLAlchemy 调用，跑在事件循环里会把
    1 个会话的写库时间摊给所有连接（SSE 心跳、其它会话）。`to_thread` 之后事件循环
    始终可调度。
    """
    if req.session_id:
        with session_scope() as session:
            row = chat_store.get_session(session, req.session_id, user.username)
            if row is None:
                raise HTTPException(404, "会话不存在或无权访问")
            return req.session_id, chat_store.get_history(session, req.session_id)
    with session_scope() as session:
        row = chat_store.create_session(session, user.username)
        return row.id, []


def _persist_user_message(session_id: str, message: str) -> None:
    """同步部分：把用户这句话落库（在拿锁**之后**调用，见 `_begin_turn`）。"""
    with session_scope() as session:
        chat_store.append_message(session, session_id, "user", message)


async def _begin_turn(user: AuthUser, req: ChatRequest) -> _Turn:
    """一轮对话的入口：会话解析（线程池）→ 取会话锁 → 用户消息落库（线程池）。

    顺序是刻意的：**先拿锁，再落库**。反过来的话，第二轮会先把用户问题写进历史、
    再被 409 挡住，回看时就会出现"用户问了但没有任何回应"的幽灵消息。
    历史必须在落库之前取，否则本轮问题会被当成历史再注入一次。
    """
    session_id, history = await asyncio.to_thread(_resolve_session, user, req)
    guard = _SessionGuard(session_id)
    await guard.acquire()
    try:
        await asyncio.to_thread(_persist_user_message, session_id, req.message)
    except Exception:
        await guard.release()
        raise
    return _Turn(session_id, history, guard)


def _persist_reply(user: AuthUser, session_id: str, result: dict) -> None:
    """同步部分：把助手答复与运行详情落库（在线程里跑）。"""
    with session_scope() as session:
        assistant_row = chat_store.append_message(
            session, session_id, "assistant", result["reply"], result.get("tool_trace") or [],
        )
        # 运行详情落库：历史回看时展示「当时的」日志与事实卡快照，而不是当前状态
        if result.get("run_detail"):
            chat_store.save_turn_log(
                session, session_id, assistant_row.id, user.username, result["run_detail"],
            )


def _payload_of(session_id: str, result: dict) -> dict:
    return {
        "session_id": session_id,
        "reply": result["reply"],
        "tool_trace": result.get("tool_trace") or [],
        "metadata_version": result.get("metadata_version"),
        "query_plan": result.get("query_plan"),
        "clarify": result.get("clarify"),
        "cited_result_ids": result.get("cited_result_ids") or [],
        "memory": result.get("memory") or {},
        "session_references": result.get("session_references") or [],
        "run_detail": result.get("run_detail"),
        "agent": result.get("agent") or {},
        "streamed": bool(result.get("streamed")),
        # 流式先给初稿、出口闸门才可能改写：前端据此提示「你看到的是未校验初稿」
        "streamed_unverified": bool(result.get("streamed_unverified")),
        # 复审 R3：模型未给出正文（已用兜底文案）时显式透出，别让"空答复"看起来像一次成功
        "reply_empty": bool(result.get("reply_empty")),
        "features": app_features(),
    }


async def _run_agent(user: AuthUser, turn: _Turn, req: ChatRequest, *,
                     token_stream: bool) -> dict:
    """跑模型（在会话锁内、本轮上下文里）。"""
    with chat_turn_context(session_id=turn.session_id, include_query_plan=req.include_query_plan,
                            agent_id=req.agent_id):
        # 只有显式指定 agent_id 时才透传（未指定时由 agent 内部路由决定，保持签名稳定）
        extra = {"agent_id": req.agent_id} if req.agent_id else {}
        if token_stream:
            return await run_chat_stream(user, turn.history, req.message, **extra)
        return await run_chat(user, turn.history, req.message, **extra)


# ---------------------------------------------------------------- 预算/循环类错误契约（P1-2）
# 这些错误的文案本来就写给用户看（含「请再次重试」），但早先它们只会变成通用 500：
# 非流式拿不到 error_code/trace，流式还会因为前端把业务失败当传输失败而**自动降级 POST**
# —— 等于把同一个循环再跑一遍。这里统一成一个契约：
#   非流式 → HTTP 422 + detail（含错误码与追踪标记）+ X-Error-Code / X-Trace-Id 头
#   流式   → SSE error 事件 + fatal=true（前端据此不再自动 POST 重试）
AGENT_FAILURE_TEMPLATES = {
    "TOOL_LOOP_REPEAT": "工具调用陷入循环，请再次重试",
    "TOOL_LOOP_FAILURES": "工具调用陷入循环，请再次重试",
    "TOOL_BUDGET_EXCEEDED": "本轮工具调用已达上限，请把问题拆小后再试",
    "TURN_BUDGET_EXCEEDED": "本轮处理预算/超时用尽，请再次重试",
    "AGENT_STEP_LIMIT": "本轮推理步数超限，请把问题拆小后再试",
    # 复审 A3：模型点了不存在的工具名（Multi-Agent 下工具集按 Agent 裁剪时最易发生）。
    # 纠错重试之后仍然失败才走到这里：给可读文案 + 错误码，且标 fatal（重发只会再撞一次）。
    "AGENT_INVALID_TOOL_NAME": "模型选择了不可用的工具，已中止本轮（可用工具见 /api/agents）",
    # 复审 R2：工具参数不是合法 JSON（`AIMessage.invalid_tool_calls`）。同一条纠错重试路径，
    # 只是文案不同——告诉用户重试或换个问法。
    "AGENT_INVALID_TOOL_ARGS": "模型给出的工具参数不合法，本轮已中止（请重试或把问题描述得更具体）",
}


def _agent_failure(exc: BaseException) -> dict | None:
    """把「循环/预算/步数/工具名」类异常翻译成统一错误契约；其它异常返回 None（保持原 500）。"""
    code = ""
    detail = ""
    if isinstance(exc, ToolLoopError):
        code = str(getattr(exc, "code", "") or "TOOL_LOOP_REPEAT")
        detail = str(exc)          # 守护层已经拼好「循环 + 错误码 + 追踪」
    elif isinstance(exc, InvalidToolCallError):
        code = str(getattr(exc, "code", "") or "AGENT_INVALID_TOOL_NAME")
        detail = str(exc)          # 校验层已经拼好「不存在的工具 + 可用工具 + 错误码 + 追踪」
    elif isinstance(exc, TurnBudgetExceeded):
        code = "TURN_BUDGET_EXCEEDED"
    elif type(exc).__name__ == "GraphRecursionError":
        code = "AGENT_STEP_LIMIT"
    else:
        return None
    trace = str(getattr(exc, "trace_id", "") or "") or "-"
    if not detail:
        base = AGENT_FAILURE_TEMPLATES.get(code, "本轮处理失败，请再次重试")
        detail = f"{base}（错误码 {code}｜追踪 {trace}）"
    return {
        "status": 422,
        "detail": detail,
        "error_code": code,
        "trace_id": trace,
        # fatal：同一条请求重试没有意义（问题本身需要拆小/换个问法），
        # 与「网络抖动」这类可重试错误分开，前端不自动降级重发。
        "fatal": True,
        "retryable": True,
        "reason": str(exc)[:200] if not isinstance(exc, ToolLoopError) else "",
    }


# 中止轮次的标记消息（复审 N2）：**刻意不带数字/追踪标记**——
# `session_memory.build_facts_card` 只收「含数字且 ≤100 字」的短句，
# 这条标记因此不会被当成"结论事实"塞进下一轮的事实卡。
ABORT_MARKER = "〔上一轮已中止，未给出结论：{code}〕请重新提问，或把问题拆小后再试。"


def _persist_aborted_turn(user: AuthUser, session_id: str, exc: BaseException) -> None:
    """把被中止的一轮写进台账 + 补一条标记消息（E6 的 abort_rate 数据源）。

    两件事都要做（复审 N2）：
    1. `chat_turn_log` 写一行 `grounding_status=aborted`：没有这行，「守护拦下了多少轮」
       只能翻日志，`trajectory.abort_rate` 永远是 0；
    2. `chat_message` 补一条 assistant **标记消息**：中止轮次没有答复，若不补，
       下一轮 `get_history` 会带出上一条 user 消息，模型就看到两条连续的用户提问
       （其中一条没人答），UI 里也会留下"问了没回应"的悬空气泡。
    记录失败只告警——不能因为它掩盖真正的中止原因。
    """
    try:
        guard = dict(getattr(exc, "guard", None) or {})
        failure = _agent_failure(exc) or {}
        code = str(getattr(exc, "code", "") or failure.get("error_code") or "")
        marker = ABORT_MARKER.format(code=code or "未知原因")
        with session_scope() as session:
            chat_store.append_message(session, session_id, "assistant", marker)
            chat_store.save_aborted_turn(
                session,
                session_id=session_id,
                username=user.username,
                turn_id=str(getattr(exc, "trace_id", "") or ""),
                error_code=code,
                guard=guard,
                agent_id=str(getattr(exc, "agent_id", "") or ""),
            )
    except Exception:  # noqa: BLE001 —— 台账失败不影响错误上抛
        logger.warning("中止轮次写台账失败（session=%s）", session_id, exc_info=True)


async def _execute_turn(user: AuthUser, req: ChatRequest, *, token_stream: bool = False) -> dict:
    """POST /chat 的完整一轮：begin → 跑模型 → 落答复 → 释放锁。"""
    turn = await _begin_turn(user, req)
    try:
        result = await _run_agent(user, turn, req, token_stream=token_stream)
        await asyncio.to_thread(_persist_reply, user, turn.session_id, result)
    except Exception as exc:  # noqa: BLE001 —— 预算/循环类错误额外落一行「中止台账」
        if _agent_failure(exc) is not None:
            await asyncio.to_thread(_persist_aborted_turn, user, turn.session_id, exc)
        raise
    finally:
        await turn.guard.release()
    return _payload_of(turn.session_id, result)



@router.post("/chat")
async def chat(req: ChatRequest, user: AuthUser = Depends(require_user)) -> dict:
    _enforce_chat_rate(user)
    try:
        return await _execute_turn(user, req)
    except Exception as exc:  # noqa: BLE001 —— 只翻译「循环/预算/步数」类错误
        failure = _agent_failure(exc)
        if failure is None:
            raise
        logger.warning("chat aborted（%s）trace=%s: %s",
                       failure["error_code"], failure["trace_id"], failure["detail"])
        raise HTTPException(
            failure["status"], failure["detail"],
            headers={"X-Error-Code": failure["error_code"], "X-Trace-Id": failure["trace_id"]},
        ) from exc


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, user: AuthUser = Depends(require_user)):
    """SSE 流式对话：先推进度事件（阶段/工具开始结束/tick），最后推 done（完整响应体）。

    **错误用 HTTP 状态码表达**（评审 2.2-7）：会话不存在 404、同会话并发 409、
    频次超限 429、锁后端不可用 503 —— 这些都发生在建流之前，客户端能按状态码分支处理；
    只有"模型跑起来之后"的失败才退化成 SSE 的 error 事件（那时状态码已经发出去了）。

    前端用 fetch + ReadableStream 消费；只有**传输层**失败才回退到 POST /api/chat
    （业务冲突类错误不能回退：重试只会再撞一次）。
    """
    _enforce_chat_rate(user)   # 超限返回 429 JSON，不建流
    turn = await _begin_turn(user, req)     # 会话解析 + 取锁（失败即真实状态码）
    queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
    sink = _QueueSink(queue)
    started = time.perf_counter()
    runtime_state.chat_stream_started()

    async def runner() -> None:
        try:
            with event_sink_context(sink):
                result = await _run_agent(user, turn, req, token_stream=True)
            await asyncio.to_thread(_persist_reply, user, turn.session_id, result)
            queue.put_nowait(("done", _payload_of(turn.session_id, result)))
        except HTTPException as exc:
            queue.put_nowait(("error", {"message": str(exc.detail), "status": exc.status_code}))
        except Exception as exc:  # noqa: BLE001 —— 失败也要把原因推给前端
            failure = _agent_failure(exc)
            if failure is not None:
                # 预算/循环类错误：带 error_code + trace_id + fatal，前端据此**不**降级 POST
                logger.warning("chat stream aborted（%s）trace=%s: %s",
                               failure["error_code"], failure["trace_id"], failure["detail"])
                await asyncio.to_thread(_persist_aborted_turn, user, turn.session_id, exc)
                queue.put_nowait(("error", {
                    "message": failure["detail"],
                    "status": failure["status"],
                    "error_code": failure["error_code"],
                    "trace_id": failure["trace_id"],
                    "fatal": True,
                    "retryable": True,
                }))
            else:
                # 复审 N6：**已经推过增量**之后才失败，说明用户屏幕上已有半截答案；
                # 这时前端若按"传输失败"自动降级 POST，等于把同一条问题再跑一整轮
                # （成本翻倍，写工具还会再建一单）。所以这里也标 fatal，让用户手动决定。
                mid_stream = bool(getattr(exc, "partial_deltas", 0))
                logger.exception("chat stream failed（mid_stream=%s）", mid_stream)
                if mid_stream:
                    queue.put_nowait(("error", {
                        "message": (f"本轮已输出部分内容，随后失败，已中止（不会自动重发）："
                                    f"{type(exc).__name__}: {exc}"),
                        "status": 502,
                        "error_code": "STREAM_INTERRUPTED",
                        "trace_id": str(getattr(exc, "trace_id", "") or "-"),
                        "fatal": True,
                        "retryable": True,
                    }))
                else:
                    queue.put_nowait(("error", {"message": f"{type(exc).__name__}: {exc}"}))
        finally:
            # 锁一定在这里释放：客户端断连时生成器会被取消，但 runner 的 finally 仍会跑
            await turn.guard.release()
            runtime_state.chat_stream_finished()
            queue.put_nowait(("__end__", {}))

    task = asyncio.create_task(runner())

    async def event_gen():
        # 服务端心跳：每 1s 推一次已等待毫秒，浏览器后台被降频时计时也准确
        try:
            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    yield _sse("tick", {"elapsed_ms": int((time.perf_counter() - started) * 1000)})
                    continue
                if event == "__end__":
                    break
                yield _sse(event, data)
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(Exception):
                await task

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx 不缓冲，事件即时到达
        },
        # 兜底：响应对象被构造出来但生成器从未被消费（客户端立刻断开）时，锁也必须释放
        background=BackgroundTask(_release_quietly, turn),
    )


async def _release_quietly(turn: _Turn) -> None:
    with contextlib.suppress(Exception):
        await turn.guard.release()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------- Agent 注册表（热插拔）
@router.get("/agents")
def list_agents(user: AuthUser = Depends(require_user)) -> dict:
    """列出可用 Agent（注册表目录扫描结果）：热插拔后无需重启即可见。"""
    from app.agents.registry import get_registry

    registry = get_registry()
    registry.maybe_reload()
    return {
        "default": registry.default_id(),
        "count": len(registry.all()),
        "total": len(registry.all(include_disabled=True)),
        "items": registry.public_list(),
        "errors": registry.errors(),
        "note": "在 src/app/agents/registry/ 新增 json + prompts/<name>.md 即可热加载，无需重启",
    }


@router.post("/admin/agents/reload")
def reload_agents(user: AuthUser = Depends(require_admin)) -> dict:
    """手动触发注册表重载（管理员）：用于改完 manifest/提示词后立即生效。"""
    from app.agents.prompt_loader import reload_prompts
    from app.agents.registry import get_registry

    reload_prompts()
    return {"reloaded": True, "registry": get_registry().reload()}


@router.get("/chat/sessions")
def list_sessions(user: AuthUser = Depends(require_user)) -> dict:
    settings = get_settings()
    hours = max(1, int(settings.chat_retention_hours))
    with session_scope() as session:
        pruned = chat_store.prune_sessions(session, hours)
        rows = chat_store.list_sessions(session, user.username, within_hours=hours)
        counts = chat_store.session_message_counts(session, [r.id for r in rows])
        items = [
            {"session_id": r.id, "title": r.title,
             "message_count": counts.get(r.id, 0),
             "created_at": r.created_at.isoformat(timespec="seconds"),
             "updated_at": r.updated_at.isoformat(timespec="seconds")}
            for r in rows
        ]
    return {"count": len(items), "items": items,
            "retention_hours": hours, "pruned": pruned}


@router.get("/chat/{session_id}")
def get_history(session_id: str, user: AuthUser = Depends(require_user)) -> dict:
    with session_scope() as session:
        row = chat_store.get_session(session, session_id, user.username)
        if row is None:
            raise HTTPException(404, "会话不存在或无权访问")
        turn_logs = chat_store.get_turn_logs(session, session_id)
        messages = [
            {"role": m.role, "content": m.content,
             "tool_trace": m.tool_trace or [],
             "run_detail": turn_logs.get(m.id),
             "created_at": m.created_at.isoformat(timespec="seconds")}
            for m in chat_store.list_messages(session, session_id)
        ]
    return {"session_id": session_id, "messages": messages}


@router.post("/chat/{session_id}/reset")
def reset(session_id: str, user: AuthUser = Depends(require_user)) -> dict:
    with session_scope() as session:
        row = chat_store.get_session(session, session_id, user.username)
        if row is None:
            raise HTTPException(404, "会话不存在或无权访问")
        chat_store.reset_session(session, session_id)
    return {"session_id": session_id, "status": "ok"}


# ---------------------------------------------------------------- HITL 审批
@router.get("/actions/list")
async def list_actions(user: AuthUser = Depends(require_user)) -> dict:
    """补货单列表：含待确认/已确认/已驳回，仅保留近 4 小时记录（附审批账号、确认时间、当前库存）。"""
    client = get_platform_client()
    data = await client.get("/api/v1/approvals", params={"status": "all", "within_hours": 4})
    if "error" in data:
        raise HTTPException(_status_of(data, 502), data["error"])
    items = data.get("items") or []
    cats = scope_categories(user)
    if cats is not None and items:
        cat_map = _sku_categories([i["sku_id"] for i in items])
        items = [i for i in items if cat_map.get(i["sku_id"]) in cats]
    return {"window_hours": 4, "count": len(items), "items": items}


@router.get("/actions/pending")
async def pending_actions(user: AuthUser = Depends(require_user)) -> dict:
    client = get_platform_client()
    data = await client.get("/api/v1/approvals", params={"status": "pending"})
    if "error" in data:
        raise HTTPException(_status_of(data, 502), data["error"])
    items = data.get("items") or []
    cats = scope_categories(user)
    if cats is not None and items:
        cat_map = _sku_categories([i["sku_id"] for i in items])
        items = [i for i in items if cat_map.get(i["sku_id"]) in cats]
    return {"count": len(items), "items": items}


async def _find_approval(approval_id: str) -> dict:
    client = get_platform_client()
    # 绕过旁路缓存：审批/驳回前后要看到"刚刚发生的那次写入"，否则重复操作会被缓存糊成 502
    data = await client.get_fresh("/api/v1/approvals", params={"status": "all"})
    if "error" in data:
        raise HTTPException(_status_of(data, 502), data["error"])
    for item in data.get("items") or []:
        if item["approval_id"] == approval_id:
            return item
    raise HTTPException(404, "审批单不存在")


@router.post("/actions/{approval_id}/approve")
async def approve(approval_id: str, decision: ActionDecision | None = None,
                  user: AuthUser = Depends(require_user)) -> dict:
    item = await _find_approval(approval_id)
    _ensure_department(user, item["sku_id"])
    client = get_platform_client()
    result = await client.post(f"/api/v1/approvals/{approval_id}/approve",
                               {"approver": user.username})
    if "error" in result:
        raise HTTPException(_status_of(result, 502), result["error"])
    result["operator"] = user.username
    return result


@router.post("/actions/{approval_id}/reject")
async def reject(approval_id: str, decision: ActionDecision | None = None,
                 user: AuthUser = Depends(require_user)) -> dict:
    item = await _find_approval(approval_id)
    _ensure_department(user, item["sku_id"])
    reason = (decision.reason if decision else "") or ""
    client = get_platform_client()
    result = await client.post(f"/api/v1/approvals/{approval_id}/reject",
                               {"approver": user.username, "reason": reason})
    if "error" in result:
        raise HTTPException(_status_of(result, 502), result["error"])
    result["operator"] = user.username
    return result


# ---------------------------------------------------------------- HITL：售后工单
# 售后工单和补货单一样是「Agent 只建草稿、人来做决定」的动作。**工单本身在中台**
# （业务事实只有一个真相），这里只做三件事：按部门过滤可见性、代理审批、透传状态码。
AFTER_SALES_STATUS = {
    "pending": "pending_confirmation",
    "approved": "approved",
    "rejected": "rejected",
    "all": None,
}


@router.get("/actions/after-sales")
async def list_after_sales_tickets(status: str = "pending", limit: int = 50,
                                   user: AuthUser = Depends(require_user)) -> dict:
    """售后工单列表：pending=待人工确认，approved/rejected=已处理，all=全部。"""
    client = get_platform_client()
    data = await client.get("/api/v1/after-sales",
                            params={"status": status, "limit": max(1, min(limit, 200))})
    if "error" in data:
        raise HTTPException(_status_of(data, 502), data["error"])
    items = data.get("items") or []
    # 中台已按调用方 key 的部门过滤；这里再按**登录账号**的部门兜一次
    # （同一个 key 可能服务多个账号，登录维度才是最终可见性）
    cats = scope_categories(user)
    if cats is not None and items:
        cat_map = _sku_categories([i["sku_id"] for i in items])
        items = [i for i in items if cat_map.get(i["sku_id"]) in cats]
    pending = len([i for i in items if i["status"] == "pending_confirmation"])
    return {
        "status": status,
        "count": len(items),
        "pending": pending,
        "items": items,
        "note": "售后工单只建草稿，确认后也不会自动退款（Mock），用于演示人工确认闭环",
    }


def _status_of(payload: dict, default: int = 502) -> int:
    """把中台的业务错误映射回 HTTP 状态码（越权 403 / 冲突 409 / 不存在 404）。

    优先用中台返回的 `status_code`：错误语义映射表会漏（中台新增一类 4xx 就退化成 502），
    而状态码是**边界方自己给的**，最不容易出错。
    """
    code = (payload or {}).get("status_code")
    if isinstance(code, int) and 400 <= code < 600:
        return code
    kind = str((payload or {}).get("error_kind") or "")
    return {"permission": 403, "not_found": 404, "auth": 401,
            "conflict": 409, "invalid": 400}.get(kind, default)


async def _decide_after_sales(ticket_id: str, *, approve: bool, decision: ActionDecision | None,
                              user: AuthUser) -> dict:
    client = get_platform_client()
    # 先查一次：确认存在性与部门归属（中台对 ticket 的作用域是按 key 判的，
    # 登录账号的部门还要在这里再挡一次，避免一个 key 服务多账号时越权）。
    # **必须绕过 Agent 旁路缓存**：刚批完再查一次，缓存会把旧状态还给你，
    # 于是"重复审批"变成 502 而不是 409。
    listing = await client.get_fresh("/api/v1/after-sales",
                                     params={"status": "all", "limit": 200})
    if "error" in listing:
        raise HTTPException(_status_of(listing, 502), listing["error"])
    row = next((i for i in (listing.get("items") or []) if i["ticket_id"] == ticket_id), None)
    if row is None:
        raise HTTPException(404, "售后工单不存在")
    _ensure_department(user, row["sku_id"])
    if row.get("status") != "pending_confirmation":
        raise HTTPException(409, f"该工单已处理（当前状态：{row.get('status')}）")

    path = f"/api/v1/after-sales/{ticket_id}/{'approve' if approve else 'reject'}"
    body = {"approver": user.username}
    reason = (decision.reason if decision else "") or ""
    if reason:
        body["reason"] = reason
    result = await client.post(path, body)
    if "error" in result:
        raise HTTPException(_status_of(result, 502), result["error"])
    payload = dict(row)
    payload.update({"status": result.get("status", row.get("status")),
                    "approver": result.get("approver", user.username),
                    "decided_at": result.get("decided_at") or "",
                    "note": reason[:250]})
    payload["operator"] = user.username
    payload["message"] = "已确认售后（演示：不触发真实退款）" if approve else "已驳回售后"
    return payload


@router.post("/actions/after-sales/{ticket_id}/approve")
async def approve_after_sales(ticket_id: str, decision: ActionDecision | None = None,
                              user: AuthUser = Depends(require_user)) -> dict:
    return await _decide_after_sales(ticket_id, approve=True, decision=decision, user=user)


@router.post("/actions/after-sales/{ticket_id}/reject")
async def reject_after_sales(ticket_id: str, decision: ActionDecision | None = None,
                             user: AuthUser = Depends(require_user)) -> dict:
    return await _decide_after_sales(ticket_id, approve=False, decision=decision, user=user)


# ---------------------------------------------------------------- 四层记忆
@router.get("/memory/sessions/{session_id}")
def memory_snapshot(session_id: str, user: AuthUser = Depends(require_user)) -> dict:
    """L1 会话记忆快照：事实卡 + 工作集 + 最近一次 query_plan + 取数台账（会话所有者或管理员）。"""
    with session_scope() as session:
        row = chat_store.get_session(session, session_id)
        if row is None:
            raise HTTPException(404, "会话不存在")
        if row.username != user.username and not user.is_admin:
            raise HTTPException(403, "无权限：该会话属于其他账号")
        snapshot = session_memory.get_memory_snapshot(session, session_id) or {}
        plan = session_memory.latest_query_plan(session, session_id)
        turn_count = chat_store.count_turn_logs(session, session_id)
    return {
        "session_id": session_id,
        "facts_card": snapshot.get("facts_card", ""),
        "facts_version": snapshot.get("facts_version", 0),
        "facts_trigger": snapshot.get("facts_trigger", ""),
        "facts_as_of": snapshot.get("facts_as_of"),
        "working_set": snapshot.get("working_set", {}),
        # 实体表的「可展示文本」版本：与注入系统提示的 <working-set> 完全一致，
        # 前端常驻区直接展示/复制，不必自己再拼一遍格式。
        "working_set_text": session_memory.working_set_text(snapshot.get("working_set") or {}),
        "working_version": snapshot.get("working_version", 0),
        "updated_at": snapshot.get("updated_at"),
        "latest_query_plan": plan,
        "recent_tool_results": list_results(session_id, limit=10),
        "turn_count": turn_count,
    }


# ---------------------------------------------------------------- 用户偏好画像（L1 长期记忆）
@router.get("/memory/profile")
def get_memory_profile(user: AuthUser = Depends(require_user)) -> dict:
    """读当前账号的偏好画像（只存偏好/关注清单，不存对话原文与结论档案）。"""
    from app.memory import user_profile

    with session_scope() as session:
        data = user_profile.get_profile(session, user.username)
    return data


@router.put("/memory/profile")
def put_memory_profile(req: ProfileUpdate, user: AuthUser = Depends(require_user)) -> dict:
    """写偏好画像（prefs 白名单字段；watch_skus 最多 20 个）。"""
    from app.memory import user_profile

    prefs = user_profile.parse_prefs(req.prefs) if req.prefs is not None else None
    with session_scope() as session:
        saved = user_profile.upsert_profile(
            session, user.username,
            department="" if user.is_admin else (user.department or ""),
            prefs=prefs, watch_skus=req.watch_skus,
        )
    return saved


@router.get("/memory/analyses")
def list_analyses(q: str = "", department: str = "", limit: int = 10,
                  user: AuthUser = Depends(require_user)) -> dict:
    """L3 组织记忆检索：按部门作用域过滤，BM25 相关度排序（无关键词时回退最近档案）。"""
    scope = _org_scope(user)
    if department and scope is not None and department not in scope:
        raise HTTPException(403, "无权限：该部门档案不可见")
    limit = max(1, min(limit, 30))
    with session_scope() as session:
        if (q or "").strip():
            items = org_memory.search_analyses(session, q, departments=scope, limit=limit)
        else:
            items = org_memory.recent_analyses(session, departments=scope, limit=limit)
    if department:
        items = [i for i in items if i["department"] == department]
    return {"count": len(items), "query": q, "items": items}


@router.post("/memory/analyses")
def create_analysis(payload: AnalysisPayload, user: AuthUser = Depends(require_user)) -> dict:
    """手动沉淀一份分析结论到组织记忆（写表 + data/analyses/<部门>/*.md）。"""
    scope = _org_scope(user)
    department = payload.department or (None if user.is_admin else user.department)
    if department and scope is not None and department not in scope:
        raise HTTPException(403, "无权限：只能沉淀本部门档案")
    # 部门是检索与目录划分的键：只接受已知部门，避免管理员手输把档案写进
    # data/analyses/<任意字符串>/ 这类无法枚举、也检索不到的目录
    known = all_departments()
    if department and department not in known:
        raise HTTPException(
            400,
            f"未知部门：{department}；可选：{'、'.join(known)}（留空表示全部门通用）",
        )
    with session_scope() as session:
        saved = org_memory.save_analysis(
            session=session, department=department, username=user.username,
            question=payload.question, conclusion=payload.conclusion, title=payload.title,
            key_metrics=payload.key_metrics, evidence=payload.evidence,
            sql_fingerprints=[], plan_id=payload.plan_id,
            metric_version=get_settings().metric_version, model=get_settings().llm_model,
            tags=payload.tags, source="manual",
        )
    return saved


@router.get("/memory/analyses/{analysis_id}")
def get_analysis(analysis_id: str, user: AuthUser = Depends(require_user)) -> dict:
    with session_scope() as session:
        data = org_memory.get_analysis(session, analysis_id, departments=_org_scope(user))
    if data is None:
        raise HTTPException(404, "档案不存在或无权限")
    return data
