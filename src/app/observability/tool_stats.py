"""工具调用可靠性计数（per-tool 成功/失败/重试/超时 + P50/P95 + 结果分类）。

为什么单独记：`latency` 只回答"工具平均多快"，回答不了"哪个工具在失败、重试了几次、
是不是超时、失败是业务拒绝还是链路故障"。JD 里"工具调用可靠性"要的是后者。这里按
`server.tool` 维度累计：
- calls / ok / errors / retries / timeouts / success_rate；
- 结果分类 `outcomes`：ok / business_error（参数或权限类，重试无用）/ transport_error
  （中台/网络故障，可重试）/ timeout / breaker_open（熔断快速失败）/
  loop_aborted（工具有循环守护判定终止本轮，见 `app/agents/tool_guard.py`）；
- 耗时 P50 / P95（进程内最近 500 次采样，重启清零）。

埋点位置：
- `MCPHub._call_raw`（MCP 工具：erp/pailitao/order）；
- `with_result_contract`（对话侧 LangChain 工具：server="chat"）。

只做进程内统计，不落库；生产可替换为 Prometheus 计数器（接口保持不变）。
"""

from __future__ import annotations

import logging
import threading
from collections import deque

MAX_SAMPLES = 500

OUTCOMES = ("ok", "business_error", "transport_error", "timeout", "breaker_open",
            "loop_aborted")


class _ToolAgg:
    __slots__ = ("calls", "ok", "errors", "retries", "timeouts", "samples", "outcomes")

    def __init__(self) -> None:
        self.calls = 0
        self.ok = 0
        self.errors = 0
        self.retries = 0
        self.timeouts = 0
        self.samples: deque[float] = deque(maxlen=MAX_SAMPLES)
        self.outcomes: dict[str, int] = {}

    def success_rate(self) -> float:
        return round(self.ok / self.calls, 4) if self.calls else 0.0


_lock = threading.Lock()
_tools: dict[str, _ToolAgg] = {}


def _key(server: str, tool: str) -> str:
    return f"{server}.{tool}"


def _percentile(ordered: list[float], ratio: float) -> float:
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return round(ordered[0], 2)
    import math

    idx = max(0, math.ceil(ratio * len(ordered)) - 1)
    return round(ordered[idx], 2)


def classify(ok: bool, *, timeout: bool = False, outcome: str = "",
             error_kind: str = "") -> str:
    """把一次调用归到五类结果之一（显式 outcome 优先，其次按错误类型推断）。"""
    if outcome:
        return outcome if outcome in OUTCOMES else "transport_error"
    if ok:
        return "ok"
    if timeout:
        return "timeout"
    if error_kind == "business":
        return "business_error"
    return "transport_error"


def record(
    server: str,
    tool: str,
    ok: bool,
    elapsed_ms: float,
    retries: int = 0,
    timeout: bool = False,
    outcome: str = "",
    error_kind: str = "",
    trace_id: str = "",
) -> None:
    """登记一次工具调用结果（失败也要记，成功率才有分母）。

    `trace_id` 只是**写入日志**用的（便于和本轮对话/中台审计对账）：
    进程内聚合指标不按 trace 拆分，避免采样表无限膨胀。
    """
    try:
        ms = float(elapsed_ms or 0)
    except (TypeError, ValueError):
        ms = 0.0
    kind = classify(ok, timeout=timeout, outcome=outcome, error_kind=error_kind)
    with _lock:
        agg = _tools.setdefault(_key(server, tool), _ToolAgg())
        agg.calls += 1
        if ok:
            agg.ok += 1
        else:
            agg.errors += 1
        agg.retries += max(0, int(retries or 0))
        if timeout or kind == "timeout":
            agg.timeouts += 1
        agg.outcomes[kind] = agg.outcomes.get(kind, 0) + 1
        agg.samples.append(round(max(0.0, ms), 2))
    if not ok and trace_id:
        logging.getLogger("sku-agent.tool_stats").warning(
            "工具失败 %s.%s kind=%s retries=%s %sms trace=%s",
            server, tool, kind, retries, round(ms, 2), trace_id,
        )


def snapshot() -> dict:
    """返回 {"overall": {...}, "tools": {name: {...}}, "note": str}。"""
    with _lock:
        raw = {name: (agg.calls, agg.ok, agg.errors, agg.retries, agg.timeouts,
                      list(agg.samples), dict(agg.outcomes))
               for name, agg in _tools.items()}
    tools: dict[str, dict] = {}
    total_calls = total_ok = total_errors = total_retries = total_timeouts = 0
    all_samples: list[float] = []
    outcome_total: dict[str, int] = {}
    for name, (calls, ok, errors, retries, timeouts, samples, outcomes) in sorted(raw.items()):
        ordered = sorted(samples)
        tools[name] = {
            "calls": calls,
            "ok": ok,
            "errors": errors,
            "success_rate": round(ok / calls, 4) if calls else 0.0,
            "retries": retries,
            "timeouts": timeouts,
            "outcomes": outcomes,
            "p50": _percentile(ordered, 0.50),
            "p95": _percentile(ordered, 0.95),
            "max": round(ordered[-1], 2) if ordered else 0.0,
        }
        total_calls += calls
        total_ok += ok
        total_errors += errors
        total_retries += retries
        total_timeouts += timeouts
        all_samples.extend(samples)
        for key, value in outcomes.items():
            outcome_total[key] = outcome_total.get(key, 0) + value
    ordered_all = sorted(all_samples)
    overall = {
        "calls": total_calls,
        "ok": total_ok,
        "errors": total_errors,
        "success_rate": round(total_ok / total_calls, 4) if total_calls else 0.0,
        "retries": total_retries,
        "timeouts": total_timeouts,
        "outcomes": outcome_total,
        "p50": _percentile(ordered_all, 0.50),
        "p95": _percentile(ordered_all, 0.95),
    }
    return {
        "overall": overall,
        "tools": tools,
        "note": (
            "进程内统计（per server.tool）：成功率 = 成功次数 / 调用次数，含重试后的成功；"
            "结果分类 ok/business_error/transport_error/timeout/breaker_open/loop_aborted "
            "用于区分「重试有用」与「重试无用」的失败；重启清零"
        ),
    }


def reset() -> None:
    """测试用：清空所有计数。"""
    with _lock:
        _tools.clear()
