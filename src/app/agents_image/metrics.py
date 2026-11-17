"""进程度量：进程内计数（任务/图片/在途），管理员面板与 `/api/health.image` 用。"""

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
}
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


def record_stage(stage: str, elapsed_ms: float) -> None:
    latency.record(f"image.{stage}", elapsed_ms)


def set_in_flight(value: int) -> None:
    global _IN_FLIGHT
    with _LOCK:
        _IN_FLIGHT = max(0, int(value))


def snapshot() -> dict:
    with _LOCK:
        return {**_COUNTERS, "in_flight_images": _IN_FLIGHT}


def reset() -> None:
    global _IN_FLIGHT
    with _LOCK:
        for key in _COUNTERS:
            _COUNTERS[key] = 0
        _IN_FLIGHT = 0
