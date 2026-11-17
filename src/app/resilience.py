"""韧性底座：**唯一**的错误分类、退避与熔断实现。

为什么要收拢（评审缺陷 B2/C4）：
在这之前「这个错误该不该重试」有 4 份各写各的实现——
`chat_agent._is_transient`（类名/状态码/文本三级）、`LLMClient.invoke_json`（只重试 JSON
校验失败，可 `LLM_MAX_RETRIES` 听起来像网络重试其实不是）、`MCPHub._call_raw`（把所有
异常一起重试，并全部计入熔断）、`rag.embedding`（又一套）。同一件事四套策略，
调参和排障都没法统一回答「为什么这里重试了、那里没有」。

现在：分类只有 `is_retryable()`，退避只有 `backoff_seconds()`，熔断只有 `Breaker`。
调用方负责的只是「重试几次」（各自的配置项）。
"""

from __future__ import annotations

import random
import time

# 类名命中即视为瞬时（openai/httpx/自定义异常都覆盖；大小写无关）
TRANSIENT_EXC_NAMES = (
    "ratelimit", "timeout", "apiconnection", "internalerror", "internalserver",
    "serviceunavailable", "overloaded", "connecterror", "connectionerror", "readtimeout",
)
TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})

# 只匹配**文字信号**，不含裸数字：业务文案里的「500 件」不是 HTTP 500。
_TRANSIENT_HINTS = (
    "timed out", "timeout", "rate limit", "ratelimit", "too many requests",
    "connection reset", "connection refused", "connection aborted",
    "temporarily unavailable", "service unavailable", "overloaded", "try again",
)


def status_code_of(exc: BaseException) -> int | None:
    """尽量从异常里取 HTTP 状态码（openai/httpx 的异常形态不统一，保守探测）。"""
    for attr in ("status_code", "status", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    return None


def is_retryable(exc: BaseException) -> bool:
    """是否值得重试：网络抖动 / 限流 / 超时才重试，业务与参数错误不重试。

    三条否定优先：
    1. 已经吐出流式增量的失败 → 不重试（重试会把半截答案重复推给前端）；
    2. 显式标注 `retryable=False` 的异常 → 不重试（工具层/服务端判定为业务错误时用）；
    3. `error_kind` 为 business/validation/auth 的异常 → 不重试。

    判定顺序：类名 → HTTP 状态码 → 消息文本（不含裸数字）。
    """
    if getattr(exc, "partial_deltas", 0):
        return False
    if getattr(exc, "retryable", None) is False:
        return False
    if str(getattr(exc, "error_kind", "") or "").lower() in {"business", "validation", "auth"}:
        return False
    name = type(exc).__name__.lower()
    if any(hint in name for hint in TRANSIENT_EXC_NAMES):
        return True
    if status_code_of(exc) in TRANSIENT_STATUS:
        return True
    text = str(exc).lower()
    return any(hint in text for hint in _TRANSIENT_HINTS)


def retry_after_seconds(exc: BaseException, default: float | None = None) -> float | None:
    """从异常/响应头取服务端建议的等待秒数（Retry-After）。"""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers:
        try:
            raw = headers.get("retry-after") or headers.get("Retry-After")
            if raw is not None:
                return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass
    return default


def backoff_seconds(attempt: int, base: float = 0.4, cap: float = 8.0) -> float:
    """指数退避 + 抖动（避免多个请求同一时刻一起重试）。"""
    delay = min(cap, base * (2 ** max(0, int(attempt))))
    return round(delay * (1 + random.uniform(0, 0.2)), 3)


class Breaker:
    """极简熔断器（进程内、按依赖名）：连续失败 N 次 → 打开 M 秒 → **单次**探测 → 闭合。

    与早先实现的差别：半开窗口内**只放一个**请求去试探（早先是到点后把并发流量全放进来，
    恢复瞬间等于对刚病好的下游再打一次雪崩）。探测结果没回写时靠 `probe_ttl` 自愈，
    避免调用方被取消后熔断器永久卡在「已发探测」状态。
    """

    def __init__(self, threshold: int = 3, reset_seconds: float = 60.0,
                 probe_ttl: float | None = None) -> None:
        self.threshold = max(1, int(threshold or 1))
        self.reset_seconds = max(0.0, float(reset_seconds or 0.0))
        self.probe_ttl = float(probe_ttl if probe_ttl is not None else self.reset_seconds)
        self.failures = 0
        self.opened_at: float | None = None
        self._probe_at: float | None = None

    # ------------------------------------------------------------ 判定
    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        elapsed = time.monotonic() - self.opened_at
        if elapsed < self.reset_seconds:
            return False
        # 半开：只允许一个探测请求；探测超时未回写则自愈重开放行
        if self._probe_at is not None and (time.monotonic() - self._probe_at) < self.probe_ttl:
            return False
        self._probe_at = time.monotonic()
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None
        self._probe_at = None

    def record_failure(self) -> None:
        self.failures += 1
        self._probe_at = None
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()

    def release_probe(self) -> None:
        """调用方被取消/异常退出时清掉探测标记（让下一个请求还能试探）。"""
        self._probe_at = None

    def state(self) -> dict:
        if self.opened_at is None:
            return {"state": "closed", "failures": self.failures, "retry_in_s": 0.0}
        remain = max(0.0, self.reset_seconds - (time.monotonic() - self.opened_at))
        return {"state": "open" if remain > 0 else "half_open",
                "failures": self.failures, "retry_in_s": round(remain, 1)}
