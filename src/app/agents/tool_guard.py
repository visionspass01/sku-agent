"""工具循环守护（P1-1）：同一工具反复调用 / 连续失败 / 超出单轮预算 → 立即终止本轮。

为什么需要这一层：`create_react_agent` 的循环由模型决定何时停，框架默认的
`recursion_limit` 在 langgraph 1.2.19 里是 10007 个超步（≈5000 轮工具调用），
几乎等于没有上限——模型一旦「卡住」就会在 300s 硬超时之前反复调工具，而每一轮
都是一次完整 LLM 调用，token 成本线性放大（历史消息越堆越长）。

判定（每次工具调用，按顺序）：
1. 本轮已终止 → 立刻抛同一条错误（不重复记账，防并发重复日志）；
2. 成功调用数已达上限 → **本次调用不执行**，抛 `TOOL_BUDGET_EXCEEDED`；
3. 执行之后：
   - **链路类**失败（transport/ratelimited/timeout/breaker_open、含 link_error/server_error）
     连续 3 次 → `TOOL_LOOP_FAILURES`；
   - 成功且 `(工具, 输入指纹)` 与上一次成功相同、累计 3 次 → `TOOL_LOOP_REPEAT`。

**业务错误不判循环**（复审 D1）：`error_kind ∈ business/permission/auth/invalid/contract`
（不存在、无权限、参数错、契约漂移）重试也不会变好，只计数（`business_errors`）并打断
「连续链路失败」的链——否则「一次问 3 个不存在的 SKU」会被判成"模型卡住"，
并给出错误的建议「请再次重试」。这类反复重试由图层 `recursion_limit`（≈15 轮）兜底。

输入指纹是「临时的」：只在本轮内用于判重，不落库、不跨轮复用。

惰性设计：没有 `tool_guard_context`（脚本直调工具、单测、巡检链路）时守护完全不生效，
因此给工具加这层包装不会改变既有行为。
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import hashlib
import inspect
import json
import logging
from typing import Any, Callable

from app.memory.runtime import (
    append_turn_step,
    current_tool_context,
    emit_event,
)
from app.observability import tool_stats

logger = logging.getLogger("sku-agent.tool_guard")

# 需求固定值：失败连续 3 次、同一工具+同一指纹连续 3 次成功 → 终止
FAILURE_LIMIT = 3
REPEAT_LIMIT = 3

# 「失败」只算**链路类**（重试有可能成功）：网络抖动 / 中台 5xx / 限流 / 超时 / 熔断。
# 业务类（不存在、无权限、参数错、契约漂移）**不算循环失败**——重试不会变好，
# 而且「一次问 3 个不存在的 SKU」是正常的排查动作，不该被判成"模型卡住了"（复审 D1）。
TRANSIENT_ERROR_KINDS = frozenset({"transport", "ratelimited", "timeout", "breaker_open"})
BUSINESS_ERROR_KINDS = frozenset({"business", "permission", "auth", "invalid", "contract"})

_MAX_FP_LEN = 12

_guard: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "tool_loop_guard", default=None
)


class ToolLoopError(RuntimeError):
    """工具调用被判定为「陷入循环」，本轮终止（不重试、不切备用模型）。

    `retryable=False` 让 `app.resilience.is_retryable()` 直接判否：
    重试只会把同一段循环再跑一遍（流式场景还会重复推送半截答案）。
    """

    retryable = False

    def __init__(self, message: str, *, code: str, tool: str = "",
                 fingerprint: str = "", success_calls: int = 0,
                 failure_streak: int = 0, trace_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.tool = tool
        self.fingerprint = fingerprint
        self.success_calls = int(success_calls)
        self.failure_streak = int(failure_streak)
        self.trace_id = trace_id


class InvalidToolCallError(RuntimeError):
    """模型给出的工具调用不可执行（复审 A3/R2）：工具名不存在，或参数不是合法 JSON。

    为什么单独一类：Multi-Agent 下工具集按 Agent 裁剪（只读 Agent 没有写工具），
    模型点到"别的 Agent 里有、当前没有"的工具名是最现实的幻觉形态；与之并列的是
    **参数 JSON 坏掉**（`AIMessage.invalid_tool_calls`，长上下文下很常见）。
    LangGraph 对这两种情况都不会报可读错误：
    - 未知工具名 → ToolNode 抛**空消息** `RuntimeError`；
    - 参数非法 → 图**直接结束**，模型正文是空的（用户只会看到一句兜底文案）。

    这里在 `post_model_hook` 里提前判定并抛出本异常：消息可读、带错误码与可用工具清单，
    `retryable=False`（换个模型也一样），路由层据此返回 422 + `X-Error-Code` + `fatal`。
    `_invoke_agent` 会先做一次**纠错重试**（把可用工具名/参数要求写进系统提示），多数能自救。
    """

    retryable = False
    code = "AGENT_INVALID_TOOL_NAME"
    reason = "unknown_tool"

    def __init__(self, bad_tools: list[str], allowed_tools: list[str], *,
                 code: str = "", reason: str = "", trace_id: str = "") -> None:
        self.bad_tools = list(bad_tools)
        self.allowed_tools = list(allowed_tools)
        self.trace_id = trace_id
        self.code = code or type(self).code
        self.reason = reason or type(self).reason
        shown = "、".join(self.allowed_tools[:12]) or "（无）"
        if self.reason == "invalid_args":
            where = "、".join(self.bad_tools[:5]) or "（未知）"
            head = (f"模型给出的工具参数不合法（{where}）：参数必须是一个合法 JSON 对象")
        else:
            head = f"模型调用了不存在的工具：{'、'.join(self.bad_tools)}"
        super().__init__(
            f"{head}；本轮可用工具只有：{shown}"
            f"（错误码 {self.code}｜追踪 {trace_id or '-'}）"
        )


def unknown_tool_error(bad_tools: list[str], allowed_tools: list[str], *,
                       trace_id: str = "") -> InvalidToolCallError:
    """模型点了不存在的工具名（`AGENT_INVALID_TOOL_NAME`）。"""
    return InvalidToolCallError(bad_tools, allowed_tools, trace_id=trace_id)


def invalid_tool_args_error(bad_tools: list[str], allowed_tools: list[str], *,
                            trace_id: str = "") -> InvalidToolCallError:
    """模型给了不可解析的工具参数（`AGENT_INVALID_TOOL_ARGS`）。"""
    return InvalidToolCallError(bad_tools, allowed_tools,
                               code="AGENT_INVALID_TOOL_ARGS", reason="invalid_args",
                               trace_id=trace_id)


def validate_tool_calls(allowed_names: set[str] | frozenset[str]) -> Callable:
    """生成 `post_model_hook`：工具调用不可执行就抛 `InvalidToolCallError`。

    白名单由装配侧的实际工具列表传入（`{t.name for t in tools}`），不另算一份清单。
    判定顺序（复审 R2 补了第一种）：
    1. `invalid_tool_calls` 非空（参数 JSON 解析失败）→ `AGENT_INVALID_TOOL_ARGS`；
    2. `tool_calls` 里有名称不在白名单 → `AGENT_INVALID_TOOL_NAME`；
    3. 纯文本答复 / 空调用 → 放行（返回 None 表示不更新 state）。
    """

    allowed = frozenset(allowed_names)

    def hook(state: dict):  # noqa: ANN202 —— langgraph post_model_hook：返回 None 表示不更新
        messages = (state or {}).get("messages") or []
        if not messages:
            return None
        last = messages[-1]
        bad_args = [str(call.get("name") or "")
                    for call in (getattr(last, "invalid_tool_calls", None) or [])]
        if bad_args:
            raise invalid_tool_args_error(sorted(set(bad_args)), sorted(allowed),
                                          trace_id=_trace_marker())
        calls = getattr(last, "tool_calls", None) or []
        bad = [str(call.get("name") or "") for call in calls
               if str(call.get("name") or "") not in allowed]
        if bad:
            raise unknown_tool_error(sorted(set(bad)), sorted(allowed), trace_id=_trace_marker())
        return None

    return hook




def fingerprint(arguments: dict[str, Any] | None) -> str:
    """工具入参的临时指纹（排序键 + 稳定序列化，同参数必得同一值）。"""
    try:
        payload = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 —— 序列化失败就退化成 repr，指认稳定即可
        payload = repr(arguments)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:_MAX_FP_LEN]


def _has_error(envelope: Any) -> bool:
    """envelope 里是否**带** error 字段（`""` 也算带：空串是契约违规，不该被当成成功）。"""
    return isinstance(envelope, dict) and envelope.get("error") is not None


def is_loop_failure(envelope: Any) -> bool:
    """这次工具结果算不算「循环式失败」（只有链路类算；业务错误不算）。

    判定顺序：
    1. 没有 error 字段 → 不是失败（成功）；
    2. `error_kind` 命中业务类（business/permission/auth/invalid/contract）→ 不算，
       重试无意义，也不该把「查 3 个不存在的 SKU」判成模型卡住；
    3. `error_kind` 命中链路类，或带 `link_error`/`server_error`/`breaker_open` → 算；
    4. 其它（工具内部抛异常被契约包成 error、没带 error_kind）→ **保守算失败**：
       这类通常是代码缺陷，重复发生就该中止。
    """
    if not _has_error(envelope):
        return False
    kind = str(envelope.get("error_kind") or "").strip().lower()
    if kind in BUSINESS_ERROR_KINDS:
        return False
    if kind in TRANSIENT_ERROR_KINDS:
        return True
    if envelope.get("link_error") or envelope.get("server_error") or envelope.get("breaker_open"):
        return True
    return True


@contextlib.contextmanager
def tool_guard_context(max_calls: int):
    """为本轮对话装载守护状态；`max_calls` = 允许的成功工具调用次数。"""
    state = {
        "max_calls": max(1, int(max_calls or 1)),
        "success_calls": 0,
        "failure_streak": 0,
        "business_errors": 0,
        "last_key": None,
        "repeat_streak": 0,
        "aborted": None,       # (code, message) —— 已终止时复用同一条错误
    }
    token = _guard.set(state)
    try:
        yield state
    finally:
        _guard.reset(token)


def current_tool_guard() -> dict | None:
    return _guard.get()


def guard_summary() -> dict | None:
    """给 run_detail / health 用的本轮守护快照（未装载时为 None）。"""
    state = _guard.get()
    if state is None:
        return None
    aborted = state.get("aborted") or ("", "")
    return {
        "aborted": bool(state.get("aborted")),
        "error_code": aborted[0],
        "max_calls": state.get("max_calls"),
        "success_calls": state.get("success_calls"),
        "failure_streak": state.get("failure_streak"),
        "repeat_streak": state.get("repeat_streak"),
        # 业务错误（不存在/无权限/参数）单独计数：它们不判循环，但「模型撞了几次」
        # 本身就是有用的信号（提示词/工具描述该改，或者用户问法有问题）
        "business_errors": state.get("business_errors"),
    }


def _trace_marker() -> str:
    from app.observability import trace as trace_mod

    return str(trace_mod.current_trace_id() or "-")


def _message(code: str, trace: str, *, extra: str = "") -> str:
    """对外文案：错误码与追踪标记都写进正文（纯文本网关/日志也能看到）。"""
    prefix = {
        "TOOL_LOOP_REPEAT": "工具调用陷入循环，请再次重试",
        "TOOL_LOOP_FAILURES": "工具调用陷入循环，请再次重试",
        "TOOL_BUDGET_EXCEEDED": "本轮工具调用已达上限",
    }.get(code, "工具调用陷入循环，请再次重试")
    if extra:
        prefix = f"{prefix}（{extra}）"
    return f"{prefix}（错误码 {code}｜追踪 {trace}）"


def _trip(tool_name: str, code: str, *, arguments_fp: str, extra: str = "") -> ToolLoopError:
    """终止本轮：记账（turn 步骤 + 工具统计 + SSE 事件 + 日志），再抛错。"""
    state = _guard.get() or {}
    trace = _trace_marker()
    message = _message(code, trace, extra=extra)
    error = ToolLoopError(
        message, code=code, tool=tool_name, fingerprint=arguments_fp,
        success_calls=int(state.get("success_calls") or 0),
        failure_streak=int(state.get("failure_streak") or 0),
        trace_id=trace,
    )
    # 哪个 Agent 把工具调进了循环（E6 的中止轮次台账要按 agent 归因）
    error.agent_id = str(current_tool_context().get("agent_id") or "")  # type: ignore[attr-defined]
    if state:
        state["aborted"] = (code, message)
    step = {
        "tool": tool_name,
        "args": "",
        "start_offset_ms": 0,
        "status": "loop_aborted",
        "elapsed_ms": 0,
        "result_id": "",
        "as_of": "",
        "row_count": 0,
        "truncated": False,
        "agent_cached": False,
        "summary": "",
        "error": message,
        "error_code": code,
        "fingerprint": arguments_fp,
        "success_calls": error.success_calls,
        "failure_streak": error.failure_streak,
        "repeat_streak": int(state.get("repeat_streak") or 0),
    }
    try:
        append_turn_step(step)
    except Exception:  # noqa: BLE001 —— 记账失败不能掩盖真正的中止
        pass
    agent_id = str(current_tool_context().get("agent_id") or "")
    try:
        tool_stats.record(f"chat:{agent_id}" if agent_id else "chat", tool_name, False, 0,
                          outcome="loop_aborted")
    except Exception:  # noqa: BLE001
        pass
    try:
        emit_event("loop_aborted", {
            "tool": tool_name, "error_code": code, "trace_id": trace,
            "message": message, "success_calls": error.success_calls,
            "failure_streak": error.failure_streak,
        })
    except Exception:  # noqa: BLE001
        pass
    logger.error(
        "工具调用被判循环并中止：code=%s tool=%s fp=%s success=%s failure_streak=%s "
        "repeat_streak=%s trace=%s message=%s",
        code, tool_name, arguments_fp, error.success_calls, error.failure_streak,
        int(state.get("repeat_streak") or 0), trace, message,
    )
    return error


def with_tool_loop_guard(tool_name: str) -> Callable:
    """给工具套上循环守护（最外层包装：能看到契约封装后的 success/error）。

    注意用 `functools.wraps` 保留签名：`StructuredTool.from_function` 依赖
    `inspect.signature` 生成 args_schema。
    """

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            state = _guard.get()
            if state is None:            # 惰性：没有轮次上下文就完全放行
                return await fn(*args, **kwargs)
            if state.get("aborted"):
                code, message = state["aborted"]
                exc = ToolLoopError(message, code=code, tool=tool_name,
                                    trace_id=_trace_marker())
                raise exc
            arguments = _bound_arguments(fn, args, kwargs)
            arg_fp = fingerprint(arguments)
            key = (tool_name, arg_fp)
            if int(state.get("success_calls") or 0) >= int(state.get("max_calls") or 0):
                raise _trip(tool_name, "TOOL_BUDGET_EXCEEDED", arguments_fp=arg_fp,
                            extra=f"{state.get('success_calls')} 次")
            envelope = await fn(*args, **kwargs)
            if is_loop_failure(envelope):
                state["failure_streak"] = int(state.get("failure_streak") or 0) + 1
                state["repeat_streak"] = 0
                state["last_key"] = None
                if int(state["failure_streak"]) >= FAILURE_LIMIT:
                    raise _trip(tool_name, "TOOL_LOOP_FAILURES", arguments_fp=arg_fp,
                                extra=f"连续 {FAILURE_LIMIT} 次链路失败")
                return envelope
            if _has_error(envelope):
                # 业务错误：不判循环，只计数；但它**打断**「连续链路失败」的链
                # （否则「挂-挂-参数错-挂」会被误算成连续 3 次链路失败）
                state["business_errors"] = int(state.get("business_errors") or 0) + 1
                state["failure_streak"] = 0
                state["repeat_streak"] = 0
                state["last_key"] = None
                return envelope
            state["failure_streak"] = 0
            state["success_calls"] = int(state.get("success_calls") or 0) + 1
            if state.get("last_key") == key:
                state["repeat_streak"] = int(state.get("repeat_streak") or 0) + 1
            else:
                state["last_key"] = key
                state["repeat_streak"] = 1
            if int(state["repeat_streak"]) >= REPEAT_LIMIT:
                raise _trip(tool_name, "TOOL_LOOP_REPEAT", arguments_fp=arg_fp,
                            extra=f"同一工具 + 同一入参已连续 {REPEAT_LIMIT} 次")
            return envelope

        return wrapper

    return decorator


def _bound_arguments(fn, args: tuple, kwargs: dict) -> dict:
    """把位置/关键字参数还原成「工具入参」字典（与 args_schema 的口径一致）。"""
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        return {k: v for k, v in bound.arguments.items() if k not in ("self", "cls")}
    except Exception:  # noqa: BLE001 —— 拿不到签名就退化为 kwargs
        return dict(kwargs or {})
