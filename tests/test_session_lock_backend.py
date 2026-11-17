"""会话锁后端的失败语义（评审 2.2-16）。

问题：配了 `REDIS_URL` 之后，跨副本会话锁成了「同一会话一次一轮」的唯一保证。
但 Redis 抖动/不可达时的行为从来没定义过：
- 早先 `get_redis()`/`client.set()` 抛的异常直接冒泡 → 500「Internal Server Error」，
  既没告诉用户"稍后再试"，也没和"你在并发对话"区分开；
- 更糟的写法是悄悄放行（fail-open）：两个标签页同时提问就互相覆盖 L1 记忆。

现在：锁后端不可用 → `LockBackendError` → **503**（fail-closed，默认）；
显式配置 `CHAT_SESSION_LOCK_REQUIRED=false` 才降级为进程内锁并告警。
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture()
def dsn_settings(_settings, monkeypatch):
    """指向一个**必然连不上**的 Redis（测试不依赖真实服务）。"""
    monkeypatch.setattr(_settings, "redis_url", "redis://127.0.0.1:6399/0")
    monkeypatch.setattr(_settings, "redis_socket_timeout", 0.2)
    monkeypatch.setattr(_settings, "redis_socket_connect_timeout", 0.2)
    from app.cache.redis_factory import reset_redis

    reset_redis()
    yield _settings
    monkeypatch.setattr(_settings, "redis_url", "")
    reset_redis()


def test_unreachable_redis_fails_closed_with_503(dsn_settings):
    from fastapi import HTTPException

    from app.api import chat_routes

    async def main():
        guard = chat_routes._SessionGuard("sess-redis-down")
        try:
            await guard.acquire()
        except HTTPException as exc:
            return exc
        await guard.release()
        return None

    exc = asyncio.run(main())
    assert exc is not None, "Redis 不可达时必须拒绝，而不是静默放行"
    assert exc.status_code == 503
    assert "会话锁后端不可用" in exc.detail
    # 锁必须被释放干净，否则后续请求会被自己的残留锁卡住
    assert asyncio.run(_lock_free("sess-redis-down")) is True


async def _lock_free(session_id: str) -> bool:
    from app.api import chat_routes

    return not chat_routes._session_lock(session_id).locked()


def test_degraded_mode_allows_when_explicitly_disabled(dsn_settings, monkeypatch):
    """显式关掉 fail-closed 时才降级放行，并且要留下降级标记（可观测）。"""
    from app.api import chat_routes

    monkeypatch.setattr(dsn_settings, "chat_session_lock_required", False)

    async def main():
        guard = chat_routes._SessionGuard("sess-degraded")
        await guard.acquire()
        degraded = guard.degraded
        locked = chat_routes._session_lock("sess-degraded").locked()
        await guard.release()
        return degraded, locked

    degraded, locked = asyncio.run(main())
    assert degraded, "降级必须显式记录原因，不能悄悄降级"
    assert locked is False   # 释放干净


def test_redis_lock_raises_backend_error_not_timeout(dsn_settings):
    """锁后端故障与「别人持有锁」必须是两种错误（否则监控看不出真实故障面）。"""
    from app.cache.lock import LockBackendError, LockTimeoutError, redis_lock

    async def main():
        try:
            async with redis_lock("lock:probe", ttl_ms=1000):
                return "acquired"
        except LockBackendError as exc:
            return ("backend", str(exc))
        except LockTimeoutError as exc:      # pragma: no cover —— 不该走到这里
            return ("timeout", str(exc))

    result = asyncio.run(main())
    assert isinstance(result, tuple) and result[0] == "backend"
    assert "锁后端不可用" in result[1]


def test_health_reports_session_lock_backend(_settings):
    """/api/health 要能回答「会话锁现在用的什么后端」。"""
    from app.api import chat_routes

    state = chat_routes.session_lock_state()
    assert state["required"] is True
    assert state["backend"] in ("memory", "redis")
    assert "note" in state


def test_session_lock_ttl_covers_worst_case_turn(_settings):
    """锁 TTL 必须覆盖最坏单轮，否则多副本下锁中途过期 → 同会话双写（复审 P1-2）。

    历史坑：TTL 取 `max(30, LLM_TIMEOUT_SECONDS)` = 120s，而单轮硬超时是 300s。
    这个洞只在"多副本 + 慢轮"时暴露，正常测试跑不出来，所以用配置关系把它钉住。
    """
    from app.api import chat_routes

    assert chat_routes.SESSION_LOCK_TTL_MARGIN_SECONDS > 0
    assert _settings.chat_turn_timeout_seconds == 300
    ttl = chat_routes._session_lock_ttl_ms(_settings)
    assert ttl > _settings.chat_turn_timeout_seconds * 1000
    assert ttl >= int((_settings.chat_turn_timeout_seconds
                       + chat_routes.SESSION_LOCK_TTL_MARGIN_SECONDS) * 1000)
    # 也必须覆盖单次 LLM 上限（重试时一轮里会有多次调用）
    assert ttl > _settings.llm_timeout_seconds * 1000


def test_session_lock_ttl_follows_the_larger_budget(_settings, monkeypatch):
    """任一预算被调大，TTL 都要跟着变（不能只盯 llm_timeout）。"""
    from app.api import chat_routes

    monkeypatch.setattr(_settings, "llm_timeout_seconds", 600.0)
    assert chat_routes._session_lock_ttl_ms(_settings) >= 630_000

    monkeypatch.setattr(_settings, "llm_timeout_seconds", 120.0)
    monkeypatch.setattr(_settings, "chat_turn_timeout_seconds", 600.0)
    assert chat_routes._session_lock_ttl_ms(_settings) >= 630_000

