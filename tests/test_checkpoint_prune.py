"""复审 F2：线程淘汰的保护窗口 + 长研判开始时 touch。

背景：`checkpoint.prune_threads()` 会把超过 `CHECKPOINT_MAX_THREADS` 的最旧线程真删掉
（saver 里的 checkpoint 数据一起删）。早先只在研判**结束**时 touch，一个还在跑的长研判
带着上一轮的旧时间，可能被并发裁剪删掉 → 追问复用上一轮采集数据会偶发失效。
"""

from __future__ import annotations

import asyncio
import datetime as dt

from app.agents import checkpoint
from app.db.engine import session_scope
from app.db.models import CheckpointThread


def _add_thread(thread_id: str, age_seconds: float) -> None:
    with session_scope() as session:
        session.add(CheckpointThread(
            thread_id=thread_id,
            updated_at=dt.datetime.now() - dt.timedelta(seconds=age_seconds),
        ))


def _clear_threads() -> None:
    """共享 session 级 DB：先清干净，断言才有确定的分母（其它用例也会建线程行）。"""
    with session_scope() as session:
        for row in session.query(CheckpointThread):
            session.delete(row)


def test_prune_only_evicts_threads_outside_protection_window(seeded):
    _clear_threads()
    _add_thread("old-a", 3600)
    _add_thread("old-b", 1800)
    _add_thread("fresh", 5)

    # 保护窗口 600s：只允许淘汰两条"老"线程；上限 1 → 需要淘汰 2 条
    dropped = asyncio.run(checkpoint.prune_threads(max_threads=1, min_age_seconds=600))
    assert dropped == 2

    with session_scope() as session:
        remaining = {r.thread_id for r in session.query(CheckpointThread)}
    assert "fresh" in remaining, "保护窗口内的线程不能被裁剪"
    assert "old-a" not in remaining and "old-b" not in remaining


def test_prune_keeps_recent_threads_when_only_recent_exist(seeded):
    _clear_threads()
    _add_thread("fresh-a", 1)
    _add_thread("fresh-b", 2)
    # 上限 0 但都在保护窗口内 → 一个都不能删
    dropped = asyncio.run(checkpoint.prune_threads(max_threads=0, min_age_seconds=600))
    assert dropped == 0
    with session_scope() as session:
        assert {r.thread_id for r in session.query(CheckpointThread)} >= {"fresh-a", "fresh-b"}


def test_inspect_touches_thread_at_start(seeded, monkeypatch):
    """研判开始时就要 touch：否则长研判期间线程可能被并发裁剪。"""
    from fakes import FakeLLM

    import app.llm as llm_mod
    from app.db import repos
    from app.pipeline import patrol

    llm_mod._llm = FakeLLM(passed=True)
    calls: list[str] = []

    async def _touch(tid: str) -> None:
        calls.append(tid)

    async def _no_prune(*args, **kwargs):  # noqa: ANN002, ANN003
        return 0

    monkeypatch.setattr(patrol, "touch_thread", _touch)
    monkeypatch.setattr(patrol, "prune_threads", _no_prune)

    async def run():
        with session_scope() as session:
            run_row = repos.create_run(session, kind="single", params={"sku_id": "ST-90-C1-M"})
            run_id = run_row.id
        return await patrol.inspect_sku("ST-90-C1-M", run_id)

    asyncio.run(run())
    assert calls, "研判没有登记线程活跃时间"
    assert len(calls) >= 2, f"应当开始与结束各 touch 一次，实际 {calls}"
    assert calls[0] == calls[-1], "开始与结束 touch 的应是同一个线程"
