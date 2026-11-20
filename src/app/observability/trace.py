"""全链路 trace（评审 2.4-13）。

问题：Agent 侧有 `turn_id`（run_detail）、中台有 `request_id`（api_audit_log）、MCP 有
自己的调用记录、RAG 与 LLM 各自一套延迟采样——**同一次用户提问产生的 5 类日志互相串不起来**，
排障时只能靠时间戳猜。

现在的约定：
- **一个轮次 = 一个 trace_id**（复用 `turn_id`，例如 `t_11a853b730`）；
- 它随 `X-Trace-Id` 传给中台（`request_id` 是 `trace_id:序号`，可对账到审计行）；
- 写进 `tool_stats` / LLM 回调 / RAG 日志 / `result_store` 台账 / `chat_turn_log`；
- 日志格式化器统一注入 `%(trace_id)s`，没有轮次上下文时打 `-`。

为什么用 contextvar 而不是显式传参：工具签名由模型决定（LangChain 那一层拿不到额外参数），
contextvar 是唯一能穿过 ReAct 循环、线程池回调与子图的通道。线程池不会自动继承 contextvar，
所以 `to_thread` 的调用点要显式把 trace 带过去（见 `run_in_trace`）。
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import logging
import threading

_TRACE: contextvars.ContextVar[dict] = contextvars.ContextVar("trace_context", default={})

# 没有轮次上下文时（脚本、启动期、单测）占位符
NO_TRACE = "-"

# P2-4（第十一轮）：`to_thread` 把同步 IO 丢进默认执行器后，池子本身没有任何可观测性。
# 默认 `max_workers = min(32, cpu+4)`；打满以后调用**在队列里无声排队**——接口变慢，
# 指标却还是一片绿。这里记调用数/当前在飞/累计占用/单次最长，导出到 metrics 端点。
_POOL_STATS = {"calls": 0, "in_flight": 0, "busy_seconds": 0.0, "busy_max_seconds": 0.0}
_POOL_LOCK = threading.Lock()


def threadpool_stats() -> dict:
    """线程池（同步 IO 卸载）的进程内统计快照。"""
    with _POOL_LOCK:
        return dict(_POOL_STATS)


def reset_threadpool_stats() -> None:
    """测试用：清零统计（进程内单例）。"""
    with _POOL_LOCK:
        _POOL_STATS.update({"calls": 0, "in_flight": 0, "busy_seconds": 0.0,
                            "busy_max_seconds": 0.0})


def current_trace() -> dict:
    """当前 trace 上下文：`{trace_id, agent_id, session_id}`（缺失字段为空串）。"""
    data = _TRACE.get() or {}
    if data:
        return data
    # 兜底：ReAct 轮次里 turn_id 一定在 turn_log 上下文里，避免每个调用点都要显式 set
    try:
        from app.memory.runtime import current_turn_log

        state = current_turn_log() or {}
        if state.get("turn_id"):
            return {"trace_id": str(state["turn_id"]), "agent_id": "", "session_id":
                    str(state.get("session_id") or "")}
    except Exception:  # noqa: BLE001 —— 观测代码不能影响主流程
        pass
    return {"trace_id": "", "agent_id": "", "session_id": ""}


def current_trace_id() -> str:
    return str(current_trace().get("trace_id") or "")


@contextlib.contextmanager
def trace_context(trace_id: str, *, agent_id: str = "", session_id: str = ""):
    """显式设置 trace（脚本/后台任务用；轮次内自动从 turn_id 推导，不用手写）。"""
    token = _TRACE.set({"trace_id": str(trace_id or ""), "agent_id": agent_id,
                        "session_id": session_id})
    try:
        yield
    finally:
        _TRACE.reset(token)


def bind_trace(trace_id: str, *, agent_id: str = "") -> contextvars.Token:
    """同步设置 trace 上下文（中间件里用；配套 `unbind_trace` 还原）。

    P1-1（第十轮）：HTTP 入口必须**真正建立** trace 上下文，否则日志格式化器只能打 `-`，
    中台/MCP 的 `X-Trace-Id` 也会是空串（它们读的是 `current_trace_id()`）。
    """
    return _TRACE.set({"trace_id": str(trace_id or ""), "agent_id": agent_id,
                       "session_id": ""})


def unbind_trace(token: contextvars.Token) -> None:
    try:
        _TRACE.reset(token)
    except ValueError:  # pragma: no cover —— 跨上下文还原
        pass


async def run_in_trace(func, /, *args, **kwargs):
    """在线程池里跑同步函数，并保证 trace 上下文不丢（`asyncio.to_thread` 不继承 contextvar）。

    P2-4（第十一轮）：顺带记录线程池统计（调用数/在飞/占用时长），
    用于发现"同步 IO 打满默认执行器"这种只在延迟上体现的故障。
    """
    import asyncio
    import time

    snapshot = current_trace()
    wrapped = functools.partial(_with_trace, snapshot, func, *args, **kwargs)
    with _POOL_LOCK:
        _POOL_STATS["calls"] += 1
        _POOL_STATS["in_flight"] += 1
    started = time.monotonic()
    try:
        return await asyncio.to_thread(wrapped)
    finally:
        elapsed = max(0.0, time.monotonic() - started)
        with _POOL_LOCK:
            _POOL_STATS["in_flight"] -= 1
            _POOL_STATS["busy_seconds"] += elapsed
            if elapsed > _POOL_STATS["busy_max_seconds"]:
                _POOL_STATS["busy_max_seconds"] = elapsed


def _with_trace(snapshot: dict, func, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    token = _TRACE.set(snapshot or {})
    try:
        return func(*args, **kwargs)
    finally:
        _TRACE.reset(token)


class TraceIdFilter(logging.Filter):
    """给每条日志记录注入 `trace_id`（没有就填 `-`）。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        if not getattr(record, "trace_id", ""):
            record.trace_id = current_trace_id() or NO_TRACE
        return True


_TRACE_FORMAT = "%(asctime)s %(levelname)s [%(name)s] [trace=%(trace_id)s] %(message)s"
_installed = False


def install_logging() -> None:
    """把 trace_id 注入日志格式（幂等）。未安装时日志仍可正常输出，只是没有 trace 字段。"""
    global _installed
    if _installed:
        return
    _installed = True
    root = logging.getLogger()
    handler_added = False
    for handler in root.handlers:
        if isinstance(handler.formatter, logging.Formatter):
            continue
        handler.setFormatter(logging.Formatter(_TRACE_FORMAT))
        handler.addFilter(TraceIdFilter())
        handler_added = True
    if not handler_added:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_TRACE_FORMAT))
        handler.addFilter(TraceIdFilter())
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            if not any(isinstance(f, TraceIdFilter) for f in handler.filters):
                handler.addFilter(TraceIdFilter())


def trace_status() -> dict:
    info = current_trace()
    return {
        "trace_id": info.get("trace_id") or "",
        "agent_id": info.get("agent_id") or "",
        "session_id": info.get("session_id") or "",
        "logging_installed": _installed,
        "note": "一个对话轮次 = 一个 trace_id；它同时出现在中台审计（X-Trace-Id）、"
                "MCP/tool_stats、LLM 回调、RAG 日志与 chat_turn_log.trace_id",
    }
