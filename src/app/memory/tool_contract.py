"""L0 工作记忆：工具装饰器。

用法（src/app/tools/platform_tools.py）：
    @with_result_contract("get_inventory")
    async def get_inventory(sku_id: str) -> dict: ...

效果：工具返回值自动补齐 result_id / as_of / summary / truncated / artifact_path，
把明细登记到 tool_result 表，并把本次调用（入参/耗时/结果 id/是否截断）记入
本轮运行日志（`turn_log`），供看板在回答下方展示「运行详情」。

口径说明（界面上"工具 16ms、却等了好几秒"的由来）：
`elapsed_ms` 只覆盖「工具函数体 + 契约封装（finalize / 台账回写 / 必要时落 artifact）」；
工具返回给模型之后，LangGraph 会**再调一次模型**决定下一步或生成答复，那段时间
属于 `llm_chat`（秒级），由 `app/observability/llm_callback.py` 单独采样并推事件。

异常也会被包装成 `{"error": ...}` 的契约结构，避免工具异常直接把 ReAct 循环打断。
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import json
import time
from typing import Callable

from app.memory.result_store import finalize
from app.memory.runtime import append_turn_step, current_tool_context, emit_event, turn_elapsed_ms
from app.observability import latency, tool_stats
from app.observability import trace as trace_mod


def _trace_id() -> str:
    return trace_mod.current_trace_id()


def _short_json(payload: dict, limit: int = 200) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(payload)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bound_args(fn, args: tuple, kwargs: dict) -> dict:
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        return {k: v for k, v in bound.arguments.items() if k not in ("self", "cls")}
    except Exception:  # noqa: BLE001 —— 拿不到签名就退化为 kwargs
        return dict(kwargs)


def with_result_contract(tool_name: str, *, max_rows: int | None = None) -> Callable:
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            ctx = current_tool_context()
            bound = _short_json(_bound_args(fn, args, kwargs))
            start_offset_ms = turn_elapsed_ms()  # 本轮内第几毫秒开始，供时间轴排序
            emit_event("tool_start", {"tool": tool_name, "args": bound,
                                      "label": f"取数：{tool_name}"})
            started = time.perf_counter()
            try:
                from app.observability.otel import span as otel_span

                span_ctx = otel_span(f"tool.{tool_name}", tool=tool_name, session_id=ctx.get("session_id") or "")
            except Exception:  # noqa: BLE001 —— 观测埋点失败不能影响工具调用
                span_ctx = contextlib.nullcontext()
            with span_ctx:
                try:
                    data = await fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 —— 工具失败也要回契约结构
                    data = {"error": f"{type(exc).__name__}: {exc}"}
            envelope = finalize(
                tool_name, data,
                session_id=ctx.get("session_id") or None,
                username=ctx.get("username") or "",
                max_rows=max_rows,
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            latency.record("tool", elapsed_ms)
            # 对话侧工具可靠性：成功/失败按 tool 维度累计（与 MCP 工具同一张看板）；
            # 失败再细分 business（权限/参数错误，重试无用）与 transport（中台/网络，可重试）。
            error_text = str(envelope.get("error") or "")
            error_kind = str(data.get("error_kind") or "") if isinstance(data, dict) else ""
            if not error_text:
                outcome = "ok"
            elif error_kind == "business" or any(
                hint in error_text for hint in ("无权限", "不存在", "必填", "参数")
            ):
                outcome = "business_error"
            else:
                outcome = "transport_error"
            # 多 Agent 场景下按 chat:<agent_id> 归集，看板可以按 agent 拆分工具可靠性
            agent_id = str(ctx.get("agent_id") or "")
            server_name = f"chat:{agent_id}" if agent_id else "chat"
            tool_stats.record(server_name, tool_name, not error_text, elapsed_ms,
                              outcome=outcome, trace_id=_trace_id())
            step = {
                "tool": tool_name,
                "args": bound,
                "start_offset_ms": start_offset_ms,
                "status": "error" if envelope.get("error") else "ok",
                "elapsed_ms": elapsed_ms,
                "result_id": envelope.get("result_id", ""),
                "as_of": envelope.get("as_of", ""),
                # 中台调用的关联 id：Agent 轮次 ↔ 中台审计行可对账（治理下沉后必备）
                "request_id": envelope.get("request_id", ""),
                "trace_id": _trace_id(),
                "row_count": int(envelope.get("row_count") or 0),
                "truncated": bool(envelope.get("truncated")),
                "artifact_path": envelope.get("artifact_path", ""),
                "agent_cached": bool(envelope.get("agent_cached")),
                "summary": str(envelope.get("summary") or "")[:200],
                "error": str(envelope.get("error") or "")[:200],
            }
            append_turn_step(step)
            emit_event("tool_end", {
                "tool": tool_name, "status": step["status"], "elapsed_ms": elapsed_ms,
                "result_id": step["result_id"], "as_of": step["as_of"],
                "request_id": step["request_id"],
                "row_count": step["row_count"], "truncated": step["truncated"],
                "agent_cached": step["agent_cached"], "error": step["error"],
            })
            return envelope

        return wrapper

    return decorator
