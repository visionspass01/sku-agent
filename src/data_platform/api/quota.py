"""中台侧按调用方限流（配额下沉）。

为什么要放在中台：Agent 侧的 `CHAT_RATE_LIMIT_PER_MINUTE` 只能约束"一个 Agent 自己的对话频次"，
多 Agent 同时接入时，**中台不知道谁在打它**——所以配额必须由被调用方按 `agent_id` 记。
这里用 1 分钟滑窗（与 Agent 侧算法一致，但独立实现：中台不能 import `app`，保持单向依赖）。
"""

from __future__ import annotations

import threading
import time

from fastapi import HTTPException

from data_platform import config
from data_platform.api.authz import Principal


class _Window:
    """按 key 记录时间戳的滑窗计数（线程安全；中台端点是同步端点，跑在线程池）。"""

    def __init__(self, max_keys: int = 10_000) -> None:
        self._stamps: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self.max_keys = max(1, int(max_keys))

    def _prune(self, key: str, window: float, now: float) -> list[float]:
        stamps = [t for t in self._stamps.get(key, []) if now - t < window]
        if stamps:
            self._stamps[key] = stamps
        else:
            self._stamps.pop(key, None)
        return stamps

    def check_and_hit(self, key: str, *, limit: int, window: float) -> dict:
        limit = max(1, int(limit))
        window = max(1.0, float(window))
        now = time.monotonic()
        with self._lock:
            stamps = self._prune(key, window, now)
            allowed = len(stamps) < limit
            if allowed:
                stamps.append(now)
                self._stamps[key] = stamps
                if len(self._stamps) > self.max_keys:
                    self._stamps.clear()      # 兜底：key 数量异常增长时整体重建
            count = len(stamps)
        remaining = max(0, limit - count)
        retry_after = 0.0
        if not allowed and stamps:
            retry_after = round(window - (now - min(stamps)), 1)
        return {"allowed": allowed, "limit": limit, "count": count,
                "remaining": remaining, "retry_after_s": max(0.0, retry_after)}


_counter = _Window()
WINDOW_SECONDS = 60.0


def reset() -> None:
    with _counter._lock:      # noqa: SLF001 —— 测试清理内部状态
        _counter._stamps.clear()  # noqa: SLF001


def check(principal: Principal) -> dict:
    """返回限流状态；超限时抛 429（带 Retry-After 与 X-RateLimit-* 头）。"""
    limit = int(principal.rate_limit or config.RATE_LIMIT_PER_MINUTE)
    state = _counter.check_and_hit(f"caller:{principal.agent_id}", limit=limit,
                                   window=WINDOW_SECONDS)
    if not state["allowed"]:
        raise HTTPException(
            429,
            f"调用方 {principal.agent_id} 超过配额（{state['limit']} 次/分钟），"
            f"请 {state['retry_after_s']}s 后重试",
            headers={
                "Retry-After": str(int(state["retry_after_s"]) or 1),
                "X-RateLimit-Limit": str(state["limit"]),
                "X-RateLimit-Remaining": "0",
            },
        )
    return state


def headers_of(state: dict) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(state["limit"]),
        "X-RateLimit-Remaining": str(state["remaining"]),
    }
