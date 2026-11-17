"""滑动窗口计数（限流底座）。

为什么要抽出来：登录限流、对话限流、将来的写操作限流都是同一套逻辑
（「同一 key 在 window 秒内最多 N 次」），差别只在 key 的组成与是否只统计失败。
把窗口算法与配置读取分开，测试里可以直接换 limit/window 而不用碰全局状态。

实现要点：
- 内存 dict + 线程锁（uvicorn 单进程多线程足够；多副本部署时应换 Redis 计数，
  接口保持不变即可替换实现）；
- 每次读取都会剪掉过期时间戳（滑动窗口，不做固定窗口的边界突刺）；
- `state()` 只读不写，`hit()` 才计数，便于「先检查、成功后才计数」的场景。
"""

from __future__ import annotations

import threading
import time


class SlidingWindowCounter:
    """按 key 记录时间戳，支持「窗口内最多 N 次」的判断。"""

    # 空槽/过期槽的清理阈值：key 数量超过上限时按最旧时间戳淘汰。
    # 为什么需要：登录限流的 key 由 (账号, IP) 组成，请求方可以随便换账号名，
    # 若空槽永不清除，字典会随请求数单调增长（一个缓慢的内存泄漏 + DoS 面）。
    def __init__(self, max_keys: int = 10_000) -> None:
        self._stamps: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self.max_keys = max(1, int(max_keys))

    def _prune(self, key: str, window: float, now: float) -> list[float]:
        stamps = [t for t in self._stamps.get(key, []) if now - t < window]
        if stamps:
            self._stamps[key] = stamps
        else:
            # 窗口内没有记录了就直接删 key，不留空槽
            self._stamps.pop(key, None)
        return stamps

    def _evict_if_needed(self) -> None:
        """key 数量超限时淘汰最久未活跃的 key（调用方持有锁）。"""
        overflow = len(self._stamps) - self.max_keys
        if overflow <= 0:
            return
        ordered = sorted(
            self._stamps.items(), key=lambda kv: max(kv[1]) if kv[1] else 0.0
        )
        for key, _ in ordered[:overflow]:
            self._stamps.pop(key, None)

    def state(self, key: str, *, limit: int, window: float) -> dict:
        """返回 {allowed, remaining, retry_after_s, count, limit, window_s}。"""
        limit = max(1, int(limit))
        window = max(0.1, float(window))
        now = time.monotonic()
        with self._lock:
            stamps = self._prune(key, window, now)
        count = len(stamps)
        remaining = max(0, limit - count)
        retry_after = 0.0
        if remaining == 0 and stamps:
            retry_after = round(window - (now - min(stamps)), 1)
        return {
            "allowed": remaining > 0,
            "remaining": remaining,
            "retry_after_s": max(0.0, retry_after),
            "count": count,
            "limit": limit,
            "window_s": window,
        }

    def hit(self, key: str, *, window: float) -> int:
        """记一次事件，返回窗口内累计次数。"""
        window = max(0.1, float(window))
        now = time.monotonic()
        with self._lock:
            stamps = self._prune(key, window, now)
            stamps.append(now)
            self._stamps[key] = stamps
            self._evict_if_needed()
        return len(stamps)

    def check_and_hit(self, key: str, *, limit: int, window: float) -> dict:
        """原子版「先判断再计数」：一次持锁完成读+写。

        `state()` + `hit()` 两段式在单事件循环里因为没有 await 而「碰巧」安全，
        但一旦支持多线程/多 worker 就是典型的 check-then-act 竞态（超发）。
        这里把两步合并，语义仍然是「本次请求允许则计数」。
        """
        limit = max(1, int(limit))
        window = max(0.1, float(window))
        now = time.monotonic()
        with self._lock:
            stamps = self._prune(key, window, now)
            allowed = len(stamps) < limit
            if allowed:
                stamps.append(now)
                self._stamps[key] = stamps
                self._evict_if_needed()
            count = len(stamps)
        remaining = max(0, limit - count)
        retry_after = 0.0
        if not allowed and stamps:
            retry_after = round(window - (now - min(stamps)), 1)
        return {
            "allowed": allowed,
            "remaining": remaining,
            "retry_after_s": max(0.0, retry_after),
            "count": count,
            "limit": limit,
            "window_s": window,
        }

    def reset(self, key: str | None = None) -> None:
        """清空某个 key（或全部）；测试与「登录成功后清零」用。"""
        with self._lock:
            if key is None:
                self._stamps.clear()
            else:
                self._stamps.pop(key, None)

    def size(self) -> int:
        """当前跟踪的 key 数（测试/健康检查用，确认不会无限增长）。"""
        with self._lock:
            return len(self._stamps)
