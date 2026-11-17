"""巡检顺序锁 TTL 与研判硬上限（复审 P1-2 的同源问题）。

背景：图的校验循环最多 `MAX_REVISIONS+1` 轮、每轮 5~6 次 LLM 调用（单次上限 120s），
没有任何时间上限时最坏是**几十分钟**；而顺序锁 TTL 只有 `LOCK_TTL_MS=120s`。
锁一过期，另一个副本就能对同一 SKU 再跑一遍：双倍 token + 重复写结果。

这里钉住两件事：
1. 锁 TTL 必须从"研判硬上限"派生（改 llm 超时/校验轮次时不会悄悄变短）；
2. 研判超时后本轮记失败，但**锁必须释放**（否则该 SKU 永远卡着）。
"""

from __future__ import annotations

import asyncio

import pytest


def test_inspect_lock_ttl_covers_budget(_settings):
    from app.pipeline import patrol

    ttl = patrol._inspect_lock_ttl_ms(_settings)
    timeout_ms = int(_settings.inspect_timeout_seconds * 1000)
    assert timeout_ms > 0
    assert ttl >= timeout_ms + 60_000, ttl
    assert ttl >= int(_settings.lock_ttl_ms)


def test_inspect_lock_ttl_follows_larger_budget(_settings, monkeypatch):
    from app.pipeline import patrol

    monkeypatch.setattr(_settings, "inspect_timeout_seconds", 1800.0)
    assert patrol._inspect_lock_ttl_ms(_settings) >= 1800_000 + 60_000

    # 0 = 不设硬上限 → 退回配置下限（不放大锁）
    monkeypatch.setattr(_settings, "inspect_timeout_seconds", 0.0)
    assert patrol._inspect_lock_ttl_ms(_settings) == int(_settings.lock_ttl_ms)


def test_lock_ttl_floor_wins_when_timeout_is_small(_settings, monkeypatch):
    from app.pipeline import patrol

    monkeypatch.setattr(_settings, "lock_ttl_ms", 10 * 60_000)
    monkeypatch.setattr(_settings, "inspect_timeout_seconds", 60.0)
    assert patrol._inspect_lock_ttl_ms(_settings) == 10 * 60_000


@pytest.fixture()
def tiny_timeout(_settings, monkeypatch):
    monkeypatch.setattr(_settings, "inspect_timeout_seconds", 0.05)
    return _settings


def test_inspect_timeout_marks_failed_and_releases_lock(seeded, tiny_timeout, monkeypatch):
    from app.cache.lock import is_locked
    from app.db import repos
    from app.db.engine import session_scope
    from app.pipeline import patrol

    async def _slow_graph(*_args, **_kwargs):  # noqa: ANN002, ANN003
        await asyncio.sleep(0.5)
        return {"status": "ok"}

    monkeypatch.setattr(patrol, "_invoke_graph", _slow_graph)

    with session_scope() as session:
        sku_id = repos.list_skus(session, limit=1)[0].sku_id
        run = repos.create_run(session, kind="single", params={"sku_id": sku_id})
        run_id = run.id

    async def _run():
        return await patrol.inspect_sku(sku_id, run_id)

    result = asyncio.run(_run())
    assert result["status"] == "failed"
    assert "超时" in result["error"], result
    # 关键：超时不能让锁留在库里，否则该 SKU 再也研判不了
    assert is_locked(f"lock:inspect:{sku_id}") is False


def test_inspect_happy_path_unchanged(seeded, _settings):
    """正常路径不受硬上限影响（默认 600s，测试里的假 LLM 秒级完成）。"""
    assert _settings.inspect_timeout_seconds >= 60
