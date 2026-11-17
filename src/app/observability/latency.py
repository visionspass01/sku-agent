"""延迟采样器（P50/P95/P99）：进程内环形缓冲，供看板"缓存命中率"卡片与延迟明细使用。

分类口径：
- `api`：Agent 的 HTTP 接口耗时（middleware 采集，含鉴权与序列化）；
- `tool`：LangChain 工具执行耗时（取数/建档等单次工具调用，含契约封装与台账回写）；
- `mcp_call`：MCP 工具调用耗时（含重试与超时，按 server.tool 明细见 `tool_stats`）；
- `platform`：Agent → 数据中台 REST 调用耗时；
- `llm`：`LLMClient` 调用耗时（巡检/研判等走 JSON 的模式）；
- `llm_chat`：对话 ReAct 模型的每次调用耗时；
- `chat_turn`：一轮对话端到端耗时（含规划、记忆、模型、工具）。

只保留最近 N 次采样，进程重启清零（Demo 足够；生产应接 Prometheus/时序库）。
"""

from __future__ import annotations

import threading
from collections import deque

MAX_SAMPLES = 1000
CATEGORIES = ("api", "tool", "mcp_call", "platform", "llm", "llm_chat", "chat_turn",
              "rag", "embedding")
CATEGORY_LABELS = {
    "api": "接口耗时（Agent HTTP）",
    "tool": "工具执行（对话侧单次工具，含台账回写）",
    "mcp_call": "MCP 工具调用（含重试/超时）",
    "platform": "数据中台调用",
    "llm": "LLM 调用（巡检/研判·JSON 模式）",
    "llm_chat": "LLM 调用（对话·ReAct）",
    "chat_turn": "对话整轮端到端",
    "rag": "RAG 检索（向量+BM25+rerank）",
    "embedding": "Embedding 调用（TokenHub）",
}

_lock = threading.Lock()
_samples: dict[str, deque[float]] = {name: deque(maxlen=MAX_SAMPLES) for name in CATEGORIES}
_totals: dict[str, int] = {name: 0 for name in CATEGORIES}


def record(category: str, ms: float) -> None:
    """记录一次耗时（毫秒）。未知分类自动登记，避免埋点写错时抛异常。"""
    if ms is None:
        return
    try:
        value = float(ms)
    except (TypeError, ValueError):
        return
    if value < 0:
        return
    with _lock:
        bucket = _samples.get(category)
        if bucket is None:
            bucket = deque(maxlen=MAX_SAMPLES)
            _samples[category] = bucket
            _totals[category] = 0
        bucket.append(round(value, 2))
        _totals[category] = _totals.get(category, 0) + 1


def _percentile(sorted_values: list[float], ratio: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return round(sorted_values[0], 2)
    # 最近秩法：index = ceil(ratio * n) - 1
    import math

    idx = max(0, math.ceil(ratio * len(sorted_values)) - 1)
    return round(sorted_values[idx], 2)


def _summary(values: list[float], total: int) -> dict:
    ordered = sorted(values)
    count = len(ordered)
    return {
        "count": total,
        "window_count": count,
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
        "p99": _percentile(ordered, 0.99),
        "avg": round(sum(ordered) / count, 2) if count else 0.0,
        "max": round(ordered[-1], 2) if count else 0.0,
        "min": round(ordered[0], 2) if count else 0.0,
    }


def snapshot() -> dict:
    """返回 {"overall": {...}, "categories": {name: {...}}, "window": N, "labels": {...}}。"""
    with _lock:
        per_cat = {name: list(bucket) for name, bucket in _samples.items()}
        totals = dict(_totals)
    merged: list[float] = []
    categories: dict[str, dict] = {}
    for name, values in per_cat.items():
        categories[name] = _summary(values, totals.get(name, 0))
        merged.extend(values)
    total_samples = sum(totals.get(name, 0) for name in per_cat)
    return {
        "overall": _summary(merged, total_samples),
        "categories": categories,
        "labels": dict(CATEGORY_LABELS),
        "window": MAX_SAMPLES,
        "note": f"进程内最近 {MAX_SAMPLES} 次采样，重启清零",
    }


def reset() -> None:
    """测试用：清空所有样本。"""
    with _lock:
        for name in list(_samples):
            _samples[name].clear()
            _totals[name] = 0
