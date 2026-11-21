"""分析线程持久化（checkpointer）+ 线程上限淘汰 + 注销 token 持久化。

对应评审发现的两个 P0：
1. 原来用 MemorySaver：进程重启后追问 404；线程无上限（trim_threads 从未被调用）；
2. 原来注销 token 只写进程内 dict：重启后旧 token "复活"。
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest


def _run_inspection(sku: str = "ST-90-C1-M") -> dict:
    from app.db import repos
    from app.db.engine import session_scope
    from app.pipeline.patrol import inspect_sku

    async def run():
        with session_scope() as session:
            run = repos.create_run(session, kind="single", params={"sku_id": sku})
            run_id = run.id
        return await inspect_sku(sku, run_id)

    return asyncio.run(run())


def _run_followup(sku: str, instruction: str) -> dict:
    from app.pipeline.patrol import followup_sku

    return asyncio.run(followup_sku(sku, instruction))


def test_followup_survives_process_restart(seeded, monkeypatch):
    """第一次研判 → 模拟进程重启（清空进程内缓存/saver）→ 追问仍能复用线程。"""
    from fakes import FakeLLM

    import app.llm as llm_mod

    monkeypatch.setattr(llm_mod, "_llm", FakeLLM(passed=True))

    first = _run_inspection("ST-90-C1-M")
    assert first["status"] == "ok", first.get("error")

    # saver 每次用完即关（相当于进程重启后重新打开 SQLite 文件），线程状态仍在
    result = _run_followup("ST-90-C1-M", "那优先调拨还是补货？")
    assert result["status"] == "ok"
    assert result["thread_id"] == "sku:ST-90-C1-M"
    assert result["final_state"].get("plan")
    nodes = [t.get("node") for t in result["final_state"].get("trace") or []]
    assert "refine" in nodes


def test_thread_registry_touch_and_prune(seeded, monkeypatch):
    """线程上限：超过 max_threads 时删除最旧线程（不再只写在文档里）。"""
    from app.agents import checkpoint as cp
    from app.db.engine import session_scope
    from app.db.models import CheckpointThread

    now = dt.datetime.now()
    with session_scope() as session:
        for i in range(6):
            session.add(CheckpointThread(
                thread_id=f"sku:OLD-{i}", updated_at=now - dt.timedelta(minutes=60 - i),
            ))

    calls: list[str] = []

    class FakeSaver:
        async def adelete_thread(self, thread_id):
            calls.append(thread_id)

    import contextlib

    @contextlib.asynccontextmanager
    async def fake_saver_cm():
        yield FakeSaver()

    monkeypatch.setattr(cp, "_saver_cm", fake_saver_cm)
    dropped = asyncio.run(cp.prune_threads(max_threads=3))
    assert dropped >= 3
    assert all(t.startswith("sku:OLD-") for t in calls)
    with session_scope() as session:
        remaining = {r.thread_id for r in session.query(CheckpointThread)}
    assert not any(t.endswith(("OLD-0", "OLD-1", "OLD-2")) for t in remaining)


def test_checkpoint_saver_kind_is_sqlite(seeded):
    from app.agents import checkpoint as cp

    health = cp.checkpoint_health()
    assert health["backend"] in ("sqlite", "init", "redis")
    asyncio.run(cp.startup())
    assert cp.saver_kind() in ("sqlite", "redis")


def test_revoked_token_survives_restart(seeded):
    """注销 token 落 SQLite：清空进程内黑名单后仍拒绝。"""
    from fastapi import HTTPException

    from app.auth import _REVOKED, AuthUser, decode_token, is_revoked, issue_token, revoke_token

    token = issue_token(AuthUser(username="admin", role="admin", department=None))
    revoke_token(token)
    assert is_revoked(token)
    # 模拟重启：清空进程内快路径
    _REVOKED.clear()
    assert is_revoked(token) is True
    with pytest.raises(HTTPException) as exc:
        decode_token(token)
    assert exc.value.status_code == 401
