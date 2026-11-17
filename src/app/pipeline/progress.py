"""巡检实时进度与终止信号（进程内，用于看板展示当前链路位置与参数）。"""

from __future__ import annotations

import time

_PROGRESS: dict[str, dict] = {}
_START_TS: dict[str, float] = {}
_STOP_EVENTS: dict[str, object] = {}


def start_run(run_id: str, params: dict | None = None) -> None:
    _START_TS[run_id] = time.monotonic()
    _PROGRESS[run_id] = {
        "run_id": run_id,
        "stage": "init",
        "message": "巡检任务已启动",
        "sku_id": None,
        "node": None,
        "processed": 0,
        "total_selected": 0,
        "queue": {},
        "params": params or {},
        "interrupted": False,
        "elapsed_ms": 0,
        "updated_at": None,
    }


def update(run_id: str, **fields) -> None:
    item = _PROGRESS.setdefault(run_id, {"run_id": run_id, "params": {}, "processed": 0,
                                         "total_selected": 0, "queue": {}})
    item.update(fields)
    item["elapsed_ms"] = int((time.monotonic() - _START_TS.get(run_id, time.monotonic())) * 1000)
    item["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")


def get(run_id: str) -> dict | None:
    item = _PROGRESS.get(run_id)
    if item is None:
        return None
    item["elapsed_ms"] = int((time.monotonic() - _START_TS.get(run_id, time.monotonic())) * 1000)
    return dict(item)


def clear(run_id: str) -> None:
    _PROGRESS.pop(run_id, None)
    _START_TS.pop(run_id, None)
    _STOP_EVENTS.pop(run_id, None)


def get_stop_event(run_id: str):
    """获取/创建该 run 的终止信号（asyncio.Event）。"""
    import asyncio

    event = _STOP_EVENTS.get(run_id)
    if event is None:
        event = asyncio.Event()
        _STOP_EVENTS[run_id] = event
    return event


def request_stop(run_id: str) -> bool:
    event = _STOP_EVENTS.get(run_id)
    if event is None:
        return False
    event.set()
    return True


def is_stopped(run_id: str) -> bool:
    event = _STOP_EVENTS.get(run_id)
    return bool(event and event.is_set())


def active_runs() -> list[str]:
    return list(_STOP_EVENTS.keys())
