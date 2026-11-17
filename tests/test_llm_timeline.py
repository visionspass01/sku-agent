"""模型调用事件 + 「时间轴」归因：把秒级的模型耗时和毫秒级的工具耗时拆开。

背景（本次修复的起因）：界面上的工具步骤显示 16~37ms，但整轮等待 7.8s，
因为 `run_detail.steps[]` 只记工具体耗时，模型调用（每次 1.2~4.9s）没有单列。
这里锁定：
1. 回调能推 `llm_start` / `llm_end`（不依赖 contextvar，sink 显式传入）；
2. 每次调用写入本轮日志（耗时/用途/token/起始偏移）；
3. `run_detail.timeline` 按发生顺序合并阶段/模型/工具，`phases` 自洽。
"""

from __future__ import annotations

import time


class FakeSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))


class FakeMessage:
    def __init__(self, tool_calls=None, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.tool_calls = tool_calls or []
        self.usage_metadata = {"input_tokens": input_tokens, "output_tokens": output_tokens}


class FakeGeneration:
    def __init__(self, message) -> None:
        self.message = message


class FakeResponse:
    def __init__(self, message) -> None:
        self.generations = [[FakeGeneration(message)]]


# ---------------------------------------------------------------- 回调 → 事件 + 日志
def test_llm_events_and_turn_log():
    from app.memory.runtime import turn_log_context
    from app.observability import latency
    from app.observability.llm_callback import LatencyCallbackHandler

    sink = FakeSink()
    before = latency.snapshot()["categories"]["llm_chat"]["count"]
    with turn_log_context("t_llm", "s_1") as state:
        handler = LatencyCallbackHandler(sink=sink, turn_log=state)
        handler.on_chat_model_start({}, [], run_id="run-tool-call")
        time.sleep(0.02)
        handler.on_llm_end(
            FakeResponse(FakeMessage(tool_calls=[{"name": "get_inventory"}],
                                     input_tokens=900, output_tokens=40)),
            "run-tool-call",
        )
        calls = list(state["llm_calls"])
    assert len(calls) == 1, calls
    call = calls[0]
    assert call["kind"] == "tool_call"
    assert call["elapsed_ms"] >= 10
    assert call["input_tokens"] == 900 and call["output_tokens"] == 40
    assert call["start_offset_ms"] >= 0 and call["index"] == 1

    kinds = [event for event, _ in sink.events]
    assert kinds == ["llm_start", "llm_end"], kinds
    start_payload = sink.events[0][1]
    end_payload = sink.events[1][1]
    assert start_payload["call"] == 1 and "elapsed_ms" in start_payload
    assert end_payload["call"] == 1 and end_payload["duration_ms"] >= 10
    assert end_payload["kind"] == "tool_call"
    assert latency.snapshot()["categories"]["llm_chat"]["count"] > before


def test_llm_end_marks_answer_and_duplicate_start_is_ignored():
    from app.observability.llm_callback import LatencyCallbackHandler

    sink = FakeSink()
    handler = LatencyCallbackHandler(sink=sink)
    handler.on_llm_start({}, [], run_id="run-same")       # 与 chat 钩子同 run_id：不应重复开一行
    handler.on_chat_model_start({}, [], run_id="run-same")
    handler.on_llm_end(FakeResponse(FakeMessage(tool_calls=[])), "run-same")
    kinds = [event for event, _ in sink.events]
    assert kinds == ["llm_start", "llm_end"], kinds
    assert sink.events[1][1]["kind"] == "answer"
    assert sink.events[0][1]["call"] == 1


def test_handler_without_sink_or_turn_log_is_safe():
    from app.observability.llm_callback import LatencyCallbackHandler

    handler = LatencyCallbackHandler()  # 脚本直调 / 单测 / 巡检：没有 sink 也不能抛错
    handler.on_chat_model_start({}, [], run_id="run-x")
    handler.on_llm_end(None, "run-x")
    handler.on_llm_error(None, "run-x")  # 已结束的 run_id：安全忽略


# ---------------------------------------------------------------- run_detail 时间轴
def test_run_detail_timeline_and_phases():
    from app.agents.chat_agent import _build_run_detail
    from app.auth import AuthUser
    from app.memory.runtime import append_turn_step, emit_stage, turn_log_context

    user = AuthUser(username="admin", role="admin", department=None)
    with turn_log_context("t_timeline", "s_1") as state:
        emit_stage("memory", "读取会话记忆（事实卡 + 工作集）")
        state["llm_calls"].append({
            "index": 1, "start_offset_ms": 10, "elapsed_ms": 1200,
            "kind": "tool_call", "input_tokens": 900, "output_tokens": 30,
        })
        append_turn_step({
            "tool": "get_inventory", "args": '{"sku_id": "ST-01-C1-M"}',
            "start_offset_ms": 1220, "status": "ok", "elapsed_ms": 16,
            "result_id": "res_timeline01", "as_of": "2026-11-04", "row_count": 0,
            "truncated": False, "artifact_path": "", "agent_cached": False,
            "summary": "", "error": "",
        })
        emit_stage("answer", "整理答复（本地汇总证据链，无模型调用）")
        detail = _build_run_detail("t_timeline", {}, {}, user)

    timeline = detail["timeline"]
    kinds = [event["kind"] for event in timeline]
    assert kinds.count("stage") == 2
    assert kinds.count("llm") == 1
    assert kinds.count("tool") == 1
    assert [event["offset_ms"] for event in timeline] == sorted(event["offset_ms"] for event in timeline)
    llm_event = next(event for event in timeline if event["kind"] == "llm")
    assert llm_event["elapsed_ms"] == 1200 and llm_event["kind_label"] == "决定取数"
    tool_event = next(event for event in timeline if event["kind"] == "tool")
    assert tool_event["elapsed_ms"] == 16 and tool_event["result_id"] == "res_timeline01"

    phases = detail["phases"]
    assert phases["llm_ms"] == 1200
    assert phases["tools_ms"] == 16
    assert phases["llm_calls"] == 1 and phases["tool_calls"] == 1
    assert phases["total_ms"] == detail["elapsed_ms"]
    assert phases["other_ms"] == max(0, phases["total_ms"] - 1200 - 16)
    assert detail["llm"]["calls_detail"][0]["elapsed_ms"] == 1200


def test_run_detail_timeline_falls_back_to_steps():
    """旧数据（没有 stages/llm_calls）也要能画出时间轴，至少列出工具。"""
    from app.agents.chat_agent import _build_run_detail
    from app.auth import AuthUser
    from app.memory.runtime import append_turn_step, turn_log_context

    user = AuthUser(username="admin", role="admin", department=None)
    with turn_log_context("t_old", "s_1"):
        append_turn_step({
            "tool": "get_sales", "args": "{}", "status": "ok", "elapsed_ms": 33,
            "result_id": "res_old0001", "as_of": "2026-11-04", "row_count": 0,
            "truncated": False, "artifact_path": "", "agent_cached": False,
            "summary": "", "error": "",
        })
        detail = _build_run_detail("t_old", {}, {}, user)

    assert [event["kind"] for event in detail["timeline"]] == ["tool"]
    assert detail["phases"]["llm_ms"] == 0 and detail["phases"]["tools_ms"] == 33
