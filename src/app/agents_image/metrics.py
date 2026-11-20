"""进程度量：进程内计数（任务/图片/在途）+ 被静默吞掉的失败 + 阶段耗时直方图。

为什么要有 `swallowed`：链路里大量"降级不阻断"的 `except Exception`（用量记账、样本归档、
outbox 入队…）本身是对的，但**吞掉之后没人知道**。这里给每类降级一个计数，`/health`、
`/api/image/metrics`（Prometheus text format）与管理员面板都能直接看到。

第五轮 P1-7：新增 provider_calls / llm_calls / llm_tokens / llm_cost / stage histogram，
供 `/api/image/metrics` 导出（Prometheus 抓取）。
"""

from __future__ import annotations

import threading

from app.observability import latency

_LOCK = threading.Lock()
_COUNTERS = {
    "tasks_created": 0,
    "tasks_done": 0,
    "tasks_failed": 0,
    "images_attempted": 0,
    "images_delivered": 0,
    "gate_skipped": 0,
    "partial_deliveries": 0,
    # P1-7：成本/发起量/LLM 用量（Prometheus 导出用；health 也会带出来）
    "provider_calls": 0,
    "llm_calls": 0,
    "llm_tokens": 0,
    "llm_cost_cny": 0.0,
    "cost_cny": 0.0,
}
_SWALLOWED: dict[str, int] = {}
_IN_FLIGHT = 0
# 阶段耗时直方图（毫秒桶），导出为 Prometheus histogram 的 _bucket/_sum/_count
_STAGE_BUCKETS_MS = (50, 100, 250, 500, 1000, 2000, 5000, 10000, 30000)
_STAGES: dict[str, dict] = {}


def record_task_created(count: int = 1) -> None:
    """一次批量请求可能建多个任务（每个选中商品一个），按实际数量累加。"""
    with _LOCK:
        _COUNTERS["tasks_created"] += max(1, int(count or 1))


def record_task_done(*, delivered: int, attempted: int) -> None:
    with _LOCK:
        _COUNTERS["tasks_done"] += 1
        _COUNTERS["images_delivered"] += max(0, int(delivered))
        _COUNTERS["images_attempted"] += max(0, int(attempted))


def record_task_failed() -> None:
    with _LOCK:
        _COUNTERS["tasks_failed"] += 1


def record_gate_skipped(count: int = 1) -> None:
    """像素门禁因"图片格式不支持解码"被跳过（非 PNG 且没有 Pillow）。"""
    with _LOCK:
        _COUNTERS["gate_skipped"] += max(0, int(count or 0))


def record_partial_delivery() -> None:
    """交付张数少于计划张数（部分成功）。"""
    with _LOCK:
        _COUNTERS["partial_deliveries"] += 1


def record_swallowed(kind: str) -> None:
    """记一次"被安静吃掉"的降级（kind 是调用点标识，例如 feedback_usage）。"""
    key = (kind or "unknown")[:48]
    with _LOCK:
        _SWALLOWED[key] = _SWALLOWED.get(key, 0) + 1


def record_stage(stage: str, elapsed_ms: float) -> None:
    latency.record(f"image.{stage}", elapsed_ms)
    with _LOCK:
        entry = _STAGES.setdefault(stage, {"buckets": [0] * len(_STAGE_BUCKETS_MS),
                                           "sum": 0.0, "count": 0})
        for index, bound in enumerate(_STAGE_BUCKETS_MS):
            if elapsed_ms <= bound:
                entry["buckets"][index] += 1
        entry["sum"] += float(elapsed_ms)
        entry["count"] += 1


def record_provider_calls(count: int = 0, *, cost_cny: float = 0.0) -> None:
    """第三方发起次数与真实成本（P1-7）：供 Prometheus 计数器导出。"""
    with _LOCK:
        _COUNTERS["provider_calls"] += max(0, int(count or 0))
        _COUNTERS["cost_cny"] += max(0.0, float(cost_cny or 0.0))


def record_llm(calls: int = 0, tokens: int = 0, *, cost_cny: float = 0.0) -> None:
    """图像解析/评审用到的 LLM 调用量（第五轮 4.4 接上模型后才有值）。"""
    with _LOCK:
        _COUNTERS["llm_calls"] += max(0, int(calls or 0))
        _COUNTERS["llm_tokens"] += max(0, int(tokens or 0))
        _COUNTERS["llm_cost_cny"] += max(0.0, float(cost_cny or 0.0))


def stage_snapshot() -> dict[str, dict]:
    """`{stage: {buckets, sum, count}}`（Prometheus histogram 导出用）。"""
    with _LOCK:
        return {stage: {"buckets": list(entry["buckets"]), "sum": entry["sum"],
                        "count": entry["count"]}
                for stage, entry in _STAGES.items()}


def stage_bounds_ms() -> tuple[int, ...]:
    return _STAGE_BUCKETS_MS


def set_in_flight(value: int) -> None:
    global _IN_FLIGHT
    with _LOCK:
        _IN_FLIGHT = max(0, int(value))


def snapshot() -> dict:
    with _LOCK:
        return {**_COUNTERS, "swallowed": dict(_SWALLOWED), "in_flight_images": _IN_FLIGHT}


def reset() -> None:
    global _IN_FLIGHT
    with _LOCK:
        for key in _COUNTERS:
            _COUNTERS[key] = 0
        _SWALLOWED.clear()
        _STAGES.clear()
        _IN_FLIGHT = 0
