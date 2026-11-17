"""对话模型的调用延迟采样（LangChain 回调）。

为什么需要它，做两件事：

1. 采样 `llm_chat`：对话 Agent 用的是自己 new 的 `ChatOpenAI`，不经过
   `src/app/llm.py::LLMClient`，所以 `latency` 里的 `llm`（JSON 模式）永远为 0。
2. **让「等模型」在界面上可见**：模型调用往往是整轮对话里最耗时的一段
   （实测一次 1.2~4.9s），而工具只有几十毫秒。这里在每次调用开始/结束时推
   `llm_start` / `llm_end` 事件，并写进本轮运行日志的 `llm_calls`，
   前端进度卡与「运行详情→时间轴」据此把时间归因到模型而不是工具。

关于实现的两个要点：
- chat 模型（`ChatOpenAI`）的钩子是 `on_chat_model_start` + `on_llm_end`
  （`langchain_core 1.7.3` 的 `language_models/chat_models.py` 里确认过，没有
  `on_chat_model_end` 的分发），所以开始与结束要分别挂这两个钩子；
- 回调可能在线程池里执行，`contextvars` 不保证传得到，所以构造时**显式**接收
  sink 与本轮日志对象，不依赖 `emit_event` 的隐式上下文。

任何异常都吞掉：采样失败绝不能影响对话主流程。
"""

from __future__ import annotations

import time

try:  # langchain-core 提供 BaseCallbackHandler；版本异常时降级为空实现
    from langchain_core.callbacks import BaseCallbackHandler
except Exception:  # noqa: BLE001
    BaseCallbackHandler = object  # type: ignore[assignment,misc]

from app.observability import latency


def _call_kind(response) -> str:
    """这次模型调用是「决定取数」还是「生成答复」（拿不到就返回空串）。"""
    try:
        generations = getattr(response, "generations", None) or []
        message = getattr(generations[0][0], "message", None) if generations else None
        if message is None:
            return ""
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls is None:
            tool_calls = (getattr(message, "additional_kwargs", None) or {}).get("tool_calls")
        return "tool_call" if tool_calls else "answer"
    except Exception:  # noqa: BLE001
        return ""


def _call_tokens(response) -> tuple[int, int]:
    """本次调用的输入/输出 token（取不到就是 (0, 0)，不臆造数字）。"""
    try:
        generations = getattr(response, "generations", None) or []
        message = getattr(generations[0][0], "message", None) if generations else None
        usage = getattr(message, "usage_metadata", None) or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        if input_tokens or output_tokens:
            return input_tokens, output_tokens
        llm_output = getattr(response, "llm_output", None) or {}
        token_usage = (llm_output.get("token_usage") or {}) if isinstance(llm_output, dict) else {}
        input_tokens = int(token_usage.get("prompt_tokens") or token_usage.get("input_tokens") or 0)
        output_tokens = int(token_usage.get("completion_tokens") or token_usage.get("output_tokens") or 0)
        return input_tokens, output_tokens
    except Exception:  # noqa: BLE001
        return 0, 0


class LatencyCallbackHandler(BaseCallbackHandler):  # type: ignore[misc]
    """记录每次模型调用耗时到 `latency.llm_chat`，并向进度流推送 llm_start/llm_end。

    sink / turn_log 由构建 agent 的调用方显式传入（可为 None）：
    - sink：`_QueueSink`，`emit(event, payload)` 塞进 SSE 队列；
    - turn_log：`turn_log_context` 的 state（含 started_at / llm_calls），
      用来算「第几秒开始调用」并把本次调用追加进 `run_detail`。
    """

    def __init__(self, sink=None, turn_log: dict | None = None) -> None:
        super().__init__()
        self._sink = sink
        self._turn_log = turn_log
        self._starts: dict[str, float] = {}
        self._start_offsets: dict[str, int] = {}
        self._call_index: dict[str, int] = {}
        self._call_seq = 0

    # ---------------------------------------------------------------- 内部
    def _turn_offset_ms(self) -> int:
        state = self._turn_log
        if not state:
            return 0
        try:
            return int((time.perf_counter() - state["started_at"]) * 1000)
        except Exception:  # noqa: BLE001
            return 0

    def _emit(self, event: str, payload: dict) -> None:
        sink = self._sink
        if sink is None:
            return
        try:
            body = dict(payload)
            body["elapsed_ms"] = self._turn_offset_ms()  # 本轮内偏移（与 stage/tick 口径一致）
            sink.emit(event, body)
        except Exception:  # noqa: BLE001 —— 事件推送失败不能影响对话主流程
            pass

    def _start(self, run_id) -> None:
        try:
            key = str(run_id)
            if key in self._starts:  # on_llm_start / on_chat_model_start 可能同名重复，只记一次
                return
            self._call_seq += 1
            self._starts[key] = time.perf_counter()
            self._start_offsets[key] = self._turn_offset_ms()
            self._call_index[key] = self._call_seq
            self._emit("llm_start", {
                "call": self._call_seq,
                "label": f"模型调用 #{self._call_seq}",
            })
        except Exception:  # noqa: BLE001
            pass

    def _end(self, run_id, response=None) -> None:
        try:
            key = str(run_id)
            started = self._starts.pop(key, None)
            offset = self._start_offsets.pop(key, 0)
            call_no = self._call_index.pop(key, 0)
            if started is None:
                return
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            latency.record("llm_chat", elapsed_ms)
            kind = _call_kind(response)
            input_tokens, output_tokens = _call_tokens(response)
            self._emit("llm_end", {
                "call": call_no,
                "kind": kind,
                "duration_ms": elapsed_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            })
            # 写本轮运行日志：run_detail 的 timeline/phases 与「模型与成本」分段要用
            state = self._turn_log
            if state is not None:
                try:
                    from app.observability import trace as trace_mod

                    state["llm_calls"].append({
                        "index": call_no,
                        "start_offset_ms": offset,
                        "elapsed_ms": elapsed_ms,
                        "kind": kind,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        # trace 跟着模型调用一起落：跨层排障时不用再按时间戳猜
                        "trace_id": trace_mod.current_trace_id(),
                    })
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 钩子
    def on_llm_start(self, *args, **kwargs):  # noqa: D401,ANN001
        run_id = kwargs.get("run_id")
        if run_id is None and len(args) >= 3:
            run_id = args[2]
        self._start(run_id)

    def on_chat_model_start(self, *args, **kwargs):  # noqa: D401,ANN001
        run_id = kwargs.get("run_id")
        if run_id is None and len(args) >= 3:
            run_id = args[2]
        self._start(run_id)

    def on_llm_end(self, *args, **kwargs):  # noqa: D401,ANN001
        run_id = kwargs.get("run_id")
        if run_id is None and len(args) >= 2:
            run_id = args[1]
        response = kwargs.get("response")
        if response is None and args:
            response = args[0]
        self._end(run_id, response)

    def on_llm_error(self, *args, **kwargs):  # noqa: D401,ANN001
        run_id = kwargs.get("run_id")
        if run_id is None and len(args) >= 2:
            run_id = args[1]
        self._end(run_id, None)
