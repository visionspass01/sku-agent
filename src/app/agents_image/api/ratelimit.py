"""请求级限流（P1-6，第九轮）：进程内滑动窗口。

为什么要有：配额是按周期（天/周/月）算的，挡不住"一分钟内狂点建任务"——
建任务与素材上传都会触发外部调用（中台 MCP 取数/下载），限速必须单独做。
这里的实现刻意保持**零依赖**（与登录限流同款思路）：单进程内按 "账号 + scope" 记窗口，
多副本部署时每个副本各限一份（总量 = 副本数 × limit），如需全局精确限速再接 Redis
滑动窗口（技术债里登记过）。
"""

from __future__ import annotations

import threading
import time

from app.core.errors import raise_image

_DEFAULT_WINDOW_SECONDS = 60.0


class SlidingWindow:
    """每 key 的滑动窗口计数（O(窗口内事件数)）。"""

    def __init__(self, limit: int, window: float = _DEFAULT_WINDOW_SECONDS) -> None:
        self.limit = max(1, int(limit))
        self.window = max(1.0, float(window))
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        moment = time.monotonic()
        with self._lock:
            events = [item for item in self._events.get(key, [])
                      if moment - item < self.window]
            if len(events) >= self.limit:
                self._events[key] = events
                return False
            events.append(moment)
            self._events[key] = events
            return True

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


_LIMITERS: dict[str, SlidingWindow] = {}
_LOCK = threading.Lock()


def _limit_of(scope: str) -> int:  # noqa: ARG001 —— 目前所有 scope 共用同一个每分钟上限
    from app.agents_image.config import get_image_settings

    return max(1, int(getattr(get_image_settings(), "rate_limit_per_minute", 60) or 60))


def _limiter(scope: str) -> SlidingWindow:
    """按 (scope, limit) 复用限流器；改了配置就重建（测试/热更新友好）。"""
    limit = _limit_of(scope)
    key = f"{scope}:{limit}"
    with _LOCK:
        limiter = _LIMITERS.get(key)
        if limiter is None:
            limiter = SlidingWindow(limit)
            _LIMITERS[key] = limiter
        return limiter


def check_rate_limit(username: str | None, scope: str) -> None:
    """超过上限抛 `IMG_RATE_LIMITED`（429, retryable）。scope 用于区分接口族。"""
    from app.agents_image.config import get_image_settings

    settings = get_image_settings()
    if not bool(getattr(settings, "rate_limit_enabled", True)):
        return
    if _limiter(scope).allow(username or "anonymous"):
        return
    raise_image(
        "IMG_RATE_LIMITED",
        f"操作过于频繁（{scope}：每分钟最多 "
        f"{int(getattr(settings, 'rate_limit_per_minute', 60) or 60)} 次），请稍后再试",
        retryable=True)


def reset_rate_limits() -> None:
    """测试/热重载用：清空所有窗口。"""
    with _LOCK:
        for limiter in _LIMITERS.values():
            limiter.clear()
        _LIMITERS.clear()


__all__ = ["SlidingWindow", "check_rate_limit", "reset_rate_limits"]
