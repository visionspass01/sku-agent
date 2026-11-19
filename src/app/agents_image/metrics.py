"""进程度量：进程内计数（任务/图片/在途）+ 被静默吞掉的失败（第三轮 P2-18）。

为什么要有 `swallowed`：链路里大量"降级不阻断"的 `except Exception`（用量记账、样本归档、
outbox 入队…）本身是对的，但**吞掉之后没人知道**。这里给每类降级一个计数，`/health`
与管理员面板能直接看到"今天有多少次降级被吞了"，排查时不用翻日志。
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
}
_SWALLOWED: dict[str, int] = {}
_IN_FLIGHT = 0


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
        _IN_FLIGHT = 0
