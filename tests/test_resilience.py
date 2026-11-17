"""韧性底座（B2/C4）：唯一的错误分类、退避、熔断实现。

回归背景：同一件事「这个错误该不该重试」曾有 4 份实现（chat_agent / LLMClient /
MCPHub / embedding），其中 LLMClient 的 `LLM_MAX_RETRIES` 只作用于 JSON 修复、
根本不重试网络错误；熔断器的「半开」到点后把所有并发流量一起放行。
"""

from __future__ import annotations

import pytest

from app.resilience import (
    Breaker,
    backoff_seconds,
    is_retryable,
    retry_after_seconds,
    status_code_of,
)


class _RateLimit(Exception):
    def __init__(self, message: str = "slow down", status_code: int | None = 429) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


class _APITimeoutError(Exception):
    pass


class _BadRequest(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------- 分类
def test_status_code_probe_covers_openai_shapes():
    assert status_code_of(_BadRequest("x", 400)) == 400

    class _WithResponse(Exception):
        def __init__(self) -> None:
            super().__init__("boom")
            self.response = type("R", (), {"status_code": 503})()

    assert status_code_of(_WithResponse()) == 503
    assert status_code_of(RuntimeError("no code")) is None


def test_class_name_and_status_are_retryable():
    assert is_retryable(_APITimeoutError("boom")) is True
    assert is_retryable(_RateLimit()) is True
    for code in (429, 500, 502, 503, 504):
        assert is_retryable(_BadRequest("upstream", code)) is True


def test_business_and_parameter_errors_are_not_retryable():
    assert is_retryable(_BadRequest("invalid_request_error: messages too long")) is False
    # 关键回归：业务文案里的「500 件」不是 HTTP 500
    assert is_retryable(ValueError("补货数量必须 >= 500 件才进人工确认")) is False
    assert is_retryable(ValueError("库存 500 件，无需补货")) is False


def test_explicit_opt_out_and_partial_stream():
    exc = ConnectionError("connection reset")
    exc.retryable = False           # type: ignore[attr-defined]
    assert is_retryable(exc) is False

    business = TimeoutError("timed out")
    business.error_kind = "business"  # type: ignore[attr-defined]
    assert is_retryable(business) is False

    partial = TimeoutError("timed out")
    partial.partial_deltas = 3        # type: ignore[attr-defined]
    assert is_retryable(partial) is False


def test_message_level_hints():
    assert is_retryable(RuntimeError("Connection reset by peer")) is True
    assert is_retryable(RuntimeError("request timed out")) is True
    assert is_retryable(RuntimeError("service unavailable")) is True
    assert is_retryable(RuntimeError("订单不存在：ORD-1")) is False


# ---------------------------------------------------------------- 退避 / Retry-After
def test_backoff_is_bounded_and_grows():
    values = [backoff_seconds(i) for i in range(8)]
    assert values[0] < values[3]
    assert all(v <= 8.0 * 1.2 for v in values)
    assert len(set(values)) > 1


def test_retry_after_from_headers():
    class _Resp:
        headers = {"retry-after": "7"}

    exc = RuntimeError("429 too many requests")
    exc.response = _Resp()  # type: ignore[attr-defined]
    assert retry_after_seconds(exc) == 7.0
    assert retry_after_seconds(RuntimeError("x")) is None


# ---------------------------------------------------------------- 熔断器
def test_breaker_opens_then_allows_single_probe():
    breaker = Breaker(threshold=2, reset_seconds=60.0)
    assert breaker.allow() is True
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state()["state"] == "open"
    assert breaker.allow() is False

    breaker.opened_at -= 61.0                    # 模拟熔断窗口结束
    assert breaker.state()["state"] == "half_open"
    assert breaker.allow() is True               # 首个请求拿到探测资格
    assert breaker.allow() is False              # 半开期间其它请求一律快速失败（不再放大流量）
    assert breaker.allow() is False

    breaker.record_success()
    assert breaker.state() == {"state": "closed", "failures": 0, "retry_in_s": 0.0}
    assert breaker.allow() is True


def test_breaker_probe_self_heals_when_caller_is_cancelled():
    """探测请求被取消（没回写结果）时不能永久卡住半开状态。"""
    breaker = Breaker(threshold=1, reset_seconds=10.0, probe_ttl=0.01)
    breaker.record_failure()
    breaker.opened_at -= 11.0
    assert breaker.allow() is True
    assert breaker.allow() is False

    import time as _time

    _time.sleep(0.02)          # 超过 probe_ttl：视为探测丢失
    assert breaker.allow() is True


def test_breaker_release_probe_allows_next_attempt():
    breaker = Breaker(threshold=1, reset_seconds=10.0)
    breaker.record_failure()
    breaker.opened_at -= 11.0
    assert breaker.allow() is True
    assert breaker.allow() is False
    breaker.release_probe()
    assert breaker.allow() is True


def test_half_open_failure_reopens():
    breaker = Breaker(threshold=2, reset_seconds=10.0)
    breaker.record_failure()
    breaker.record_failure()
    breaker.opened_at -= 11.0
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state()["state"] == "open"
    assert breaker.allow() is False


@pytest.mark.parametrize("threshold", [1, 3])
def test_threshold_is_respected(threshold):
    breaker = Breaker(threshold=threshold, reset_seconds=1.0)
    for _ in range(threshold - 1):
        breaker.record_failure()
    assert breaker.state()["state"] == "closed"
    breaker.record_failure()
    assert breaker.state()["state"] == "open"
