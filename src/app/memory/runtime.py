"""工具调用上下文（contextvar）：让工具知道「本次调用属于哪个会话 / 哪个账号」。

LangChain 工具签名由模型决定，无法携带 session_id，因此用 contextvar 隐式传递：
`run_chat` 进入时 set，工具装饰器读出后写 tool_result 表与 artifacts 目录。
"""

from __future__ import annotations

import contextlib
import contextvars
import time

_tool_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("tool_call_context", default={})
_turn_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("chat_turn_context", default={})
_turn_log: contextvars.ContextVar[dict | None] = contextvars.ContextVar("chat_turn_log", default=None)


def current_tool_context() -> dict:
    return _tool_ctx.get() or {}


@contextlib.contextmanager
def tool_context(session_id: str | None = None, username: str = "", department: str | None = None,
                 agent_id: str = ""):
    token = _tool_ctx.set(
        {"session_id": session_id or "", "username": username, "department": department,
         "agent_id": agent_id}
    )
    try:
        yield
    finally:
        _tool_ctx.reset(token)


def current_chat_context() -> dict:
    """本轮对话上下文（会话 id + 是否展示 query_plan）。"""
    return _turn_ctx.get() or {}


@contextlib.contextmanager
def chat_turn_context(session_id: str | None = None, include_query_plan: bool = False,
                      agent_id: str = ""):
    token = _turn_ctx.set({"session_id": session_id or "", "include_query_plan": include_query_plan,
                           "agent_id": agent_id or ""})
    try:
        yield
    finally:
        _turn_ctx.reset(token)


# ---------------------------------------------------------------- 本轮运行日志（可观测性）
@contextlib.contextmanager
def turn_log_context(turn_id: str, session_id: str = ""):
    """收集「本轮」的工具步骤与耗时，供回答下方展示运行详情。"""
    state = {"turn_id": turn_id, "session_id": session_id, "steps": [],
             "llm_calls": [], "stages": [], "started_at": time.perf_counter()}
    token = _turn_log.set(state)
    try:
        yield state
    finally:
        _turn_log.reset(token)


def current_turn_log() -> dict | None:
    return _turn_log.get()


def append_turn_step(step: dict) -> None:
    state = _turn_log.get()
    if not state:
        return
    step = dict(step)
    step["seq"] = len(state["steps"]) + 1
    state["steps"].append(step)


def turn_elapsed_ms() -> int:
    state = _turn_log.get()
    if not state:
        return 0
    return int((time.perf_counter() - state["started_at"]) * 1000)


def append_stage_mark(stage: str, label: str, **extra) -> None:
    """把阶段标记写进本轮运行日志（时间轴用：第几秒进入了哪个阶段）。"""
    state = _turn_log.get()
    if not state:
        return
    try:
        mark = {"stage": stage, "label": label, "offset_ms": turn_elapsed_ms()}
        mark.update(extra)
        state["stages"].append(mark)
    except Exception:  # noqa: BLE001 —— 记录失败不影响主流程
        pass


def append_llm_call(record: dict) -> None:
    """把一次模型调用（耗时/起止偏移/token/用途）写进本轮运行日志。"""
    state = _turn_log.get()
    if not state:
        return
    try:
        item = dict(record)
        item.setdefault("index", len(state["llm_calls"]) + 1)
        item.setdefault("start_offset_ms", turn_elapsed_ms())
        state["llm_calls"].append(item)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 实时进度事件（SSE 用）
# 聊天接口流式返回时由 routes 注册 sink；工具/阶段埋点通过 emit_event 推送。
# 没有 sink（普通 POST / 测试 / 巡检）时全部 no-op，不影响既有行为。
_event_sink: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "chat_event_sink", default=None
)


@contextlib.contextmanager
def event_sink_context(sink):
    token = _event_sink.set(sink)
    try:
        yield sink
    finally:
        _event_sink.reset(token)


def current_event_sink():
    """当前本轮的事件 sink（没有则为 None）。

    为什么需要读它：LangChain 的模型回调可能在线程池里执行，contextvar 不会
    自动传过去，所以构建 agent 时把 sink 引用显式交给回调对象。
    """
    return _event_sink.get()


def emit_event(event: str, payload: dict | None = None) -> None:
    """向本轮事件 sink 推送一条事件（同步非阻塞；sink 由调用方实现）。"""
    sink = _event_sink.get()
    if sink is None:
        return
    try:
        sink.emit(event, dict(payload or {}, elapsed_ms=turn_elapsed_ms()))
    except Exception:  # noqa: BLE001 —— 事件推送失败不能影响主流程
        pass


def emit_stage(stage: str, label: str, **extra) -> None:
    append_stage_mark(stage, label)
    emit_event("stage", {"stage": stage, "label": label, **extra})
