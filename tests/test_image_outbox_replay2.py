"""第四轮 W4：outbox 三段式重放（P1-5 网络 IO 出会话 / P2-7 清理与 GROUP BY 统计）。

口径：
- 重放的读（pending_payloads）、占位（claim）、网络（save_result）、收尾（mark_*_by_id）
  各自独立短会话，**网络阶段不允许有任何会话/连接被占住**（SQLite 下就是 `database is locked` 的根因）；
- `claim()` 把 next_retry_at 推后，避免并发重放把同一批发两遍；
- `prune()` 只清理已 sent 且超过保留期的行；`stats()` 用 GROUP BY 聚合。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
from pathlib import Path

import pytest

from app.agents_image.store import outbox as outbox_store
from app.agents_image.store.models import ImageOutbox
from app.db.engine import get_engine, session_scope

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _schema(_db):
    from app.agents_image.store.schema import ensure_image_schema

    ensure_image_schema()
    yield


def _enqueue_due(task_id: str) -> int:
    with session_scope() as session:
        row = outbox_store.enqueue(session, task_id=task_id, payload={"task_id": task_id})
        row.next_retry_at = dt.datetime.now() - dt.timedelta(seconds=1)
        session.flush()
        return int(row.id)


class _OkCatalog:
    def __init__(self, observed: list | None = None):
        self.observed = observed if observed is not None else []

    async def save_result(self, payload):
        self.observed.append(get_engine().pool.checkedout())
        return {"ok": True}


class _BadCatalog:
    async def save_result(self, payload):
        raise RuntimeError("platform still down")


# ---------------------------------------------------------------- P1-5：网络 IO 出会话
def test_replay_keeps_network_io_outside_db_session():
    """`save_result` 执行期间不能占着数据库连接（否则写锁被网络拖住 → database is locked）。"""
    _enqueue_due("t-io")
    observed: list = []
    result = asyncio.run(outbox_store.replay(catalog=_OkCatalog(observed), limit=20))
    assert result["sent"] >= 1
    assert observed, "save_result 没被调用"
    assert all(count == 0 for count in observed), f"网络阶段仍占着连接：{observed}"


def test_replay_has_no_session_parameter():
    signature = inspect.signature(outbox_store.replay)
    assert "session" not in signature.parameters
    for name in ("pending_payloads", "claim", "mark_sent_by_id", "mark_failed_by_id",
                 "prune", "stats"):
        assert hasattr(outbox_store, name), name


def test_admin_replay_route_does_not_wrap_network_in_session():
    source = (ROOT / "src/app/agents_image/api/admin_routes.py").read_text(encoding="utf-8")
    start = source.index("async def admin_replay_outbox")
    end = source.index("@router.post(\"/admin/reload\")")
    block = source[start:end]
    assert "await outbox_store.replay(limit=50)" in block
    # 会话里只允许读统计/列表，不允许包住 replay
    assert "with session_scope() as session:\n            result = await" not in block


# ---------------------------------------------------------------- claim：防并发重发
def test_claim_pushes_retry_window_so_it_is_not_sent_twice():
    row_id = _enqueue_due("t-claim")
    before = {item["id"] for item in outbox_store.pending_payloads(limit=50)}
    assert row_id in before

    claimed = outbox_store.claim([row_id], seconds=600)
    assert claimed == [row_id]
    after = {item["id"] for item in outbox_store.pending_payloads(limit=50)}
    assert row_id not in after, "占位后不该再被下一轮重放捞到"

    later = dt.datetime.now() + dt.timedelta(seconds=700)
    assert row_id in {item["id"] for item in outbox_store.pending_payloads(limit=50, now=later)}


def test_claim_ignores_unknown_ids():
    assert outbox_store.claim([]) == []
    assert outbox_store.claim([987654321]) == []


# ---------------------------------------------------------------- 失败与退避
def test_mark_failed_by_id_backs_off_then_exhausts():
    with session_scope() as session:
        row = outbox_store.enqueue(session, task_id="t-fail", payload={})
        row.next_retry_at = dt.datetime.now() - dt.timedelta(seconds=1)
        session.flush()
        row_id = int(row.id)

    assert outbox_store.mark_failed_by_id(row_id, "still down") is True
    with session_scope() as session:
        row = session.get(ImageOutbox, row_id)
        assert row.status == "pending" and row.attempts == 1 and row.last_error
        # 退避：下一次到期时间被推后，不会立刻重放
        assert row.next_retry_at > dt.datetime.now()

    with session_scope() as session:
        row = session.get(ImageOutbox, row_id)
        row.attempts = outbox_store.MAX_ATTEMPTS - 1
        session.flush()
    assert outbox_store.mark_failed_by_id(row_id, "still down") is True
    with session_scope() as session:
        row = session.get(ImageOutbox, row_id)
        assert row.status == "failed" and row.next_retry_at is None


def test_replay_marks_failure_and_keeps_row_pending():
    _enqueue_due("t-bad-replay")
    stats_before = outbox_store.stats()
    result = asyncio.run(outbox_store.replay(catalog=_BadCatalog(), limit=20))
    assert result["failed"] >= 1
    stats_after = outbox_store.stats()
    assert stats_after["pending"] + stats_after["failed"] >= stats_before["pending"]


# ---------------------------------------------------------------- P2-7：清理与统计
def test_prune_removes_only_old_sent_rows():
    now = dt.datetime.now()
    with session_scope() as session:
        old = outbox_store.enqueue(session, task_id="t-old-sent", payload={})
        old.status = "sent"
        old.created_at = now - dt.timedelta(hours=100)
        fresh = outbox_store.enqueue(session, task_id="t-fresh-sent", payload={})
        fresh.status = "sent"
        pending = outbox_store.enqueue(session, task_id="t-old-pending", payload={})
        pending.created_at = now - dt.timedelta(hours=100)
        session.flush()
        old_id, fresh_id, pending_id = int(old.id), int(fresh.id), int(pending.id)

    removed = outbox_store.prune(keep_hours=72, now=now)
    assert removed >= 1
    with session_scope() as session:
        assert session.get(ImageOutbox, old_id) is None, "过期 sent 行应被清理"
        assert session.get(ImageOutbox, fresh_id) is not None, "未过期的 sent 行要保留"
        assert session.get(ImageOutbox, pending_id) is not None, "pending 行不能删"


def test_prune_uses_configured_keep_hours():
    from app.agents_image.config import get_image_settings

    settings = get_image_settings()
    assert hasattr(settings, "outbox_keep_hours")
    assert hasattr(settings, "outbox_claim_seconds")


def test_stats_groups_by_status():
    with session_scope() as session:
        pending = outbox_store.enqueue(session, task_id="t-stats-pending", payload={})
        sent = outbox_store.enqueue(session, task_id="t-stats-sent", payload={})
        sent.status = "sent"
        session.flush()
        assert pending.id and sent.id
    stats = outbox_store.stats()
    assert stats["pending"] >= 1 and stats["sent"] >= 1
    assert set(stats) >= {"pending", "sent", "failed"}


def test_cleanup_loop_prunes_outbox():
    source = (ROOT / "src/app/agents_image/runtime.py").read_text(encoding="utf-8")
    assert "_prune_outbox" in source and "prune()" in source
