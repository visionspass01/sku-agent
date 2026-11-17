"""D12：配额（自然周期/条件更新不超发）、使用量（小时流水 + 多粒度聚合）、outbox 补偿。"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.sources import product_source as ps
from app.agents_image.store import outbox as outbox_store
from app.agents_image.store import quota as quota_store
from app.agents_image.store import usage as usage_store
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch, _db):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    image_metrics.reset()
    ensure_image_schema()
    with session_scope() as session:
        user_store.ensure_seed_users(session)
    yield
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()


# ---------------------------------------------------------------- 周期键
def test_period_keys_are_natural_cycles_asia_shanghai():
    moment = dt.datetime(2026, 11, 18, 23, 30)
    assert quota_store.period_key("day", moment) == "2026-11-19"     # +8h 跨天
    assert quota_store.period_key("month", moment) == "2026-11"
    assert quota_store.period_key("week", moment).startswith("2026-W")
    assert quota_store.period_key("unlimited") == "unlimited"


# ---------------------------------------------------------------- 配额
def test_default_quota_created_and_consumed():
    with session_scope() as session:
        quota_store.grant(session, "u-quota-1", period_type="day", period_limit=2)
        assert quota_store.check(session, "u-quota-1", 2)["allowed"] is True
        assert quota_store.consume(session, "u-quota-1", 2) is True
        state = quota_store.check(session, "u-quota-1", 1)
        assert state["allowed"] is False and state["remaining"] == 0
        assert quota_store.consume(session, "u-quota-1", 1) is False


def test_quota_rollover_resets_used():
    with session_scope() as session:
        row = quota_store.grant(session, "u-quota-2", period_type="day", period_limit=1)
        assert quota_store.consume(session, "u-quota-2", 1) is True
        row.period_key = "2020-01-01"          # 假装上一周期
        session.flush()
        assert quota_store.check(session, "u-quota-2", 1)["allowed"] is True
        assert quota_store.check(session, "u-quota-2", 1)["remaining"] == 1


def test_admin_is_unlimited():
    with session_scope() as session:
        assert quota_store.check(session, "admin", 10 ** 6, is_admin=True)["allowed"] is True
        quota_store.grant(session, "admin", period_type="unlimited", period_limit=0,
                          granted_by="system")
        assert quota_store.consume(session, "admin", 10 ** 6) is True  # unlimited 不计数


def test_concurrent_consume_never_oversells():
    """8 个线程抢 5 张额度：成功次数必须 ≤ 5（条件更新，不超发）。"""
    with session_scope() as session:
        quota_store.grant(session, "u-quota-3", period_type="day", period_limit=5)

    results: list[bool] = []
    lock = threading.Lock()

    def worker():
        with session_scope() as session:
            ok = quota_store.consume(session, "u-quota-3", 1)
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(1 for ok in results if ok) <= 5
    with session_scope() as session:
        assert quota_store.check(session, "u-quota-3", 0)["remaining"] >= 0


def test_list_quotas_shape():
    with session_scope() as session:
        quota_store.grant(session, "u-quota-4", period_type="week", period_limit=20,
                          granted_by="admin")
        rows = {item["username"]: item for item in quota_store.list_quotas(session)}
    assert rows["u-quota-4"]["period_type"] == "week"
    assert rows["u-quota-4"]["period_limit"] == 20
    assert rows["u-quota-4"]["granted_by"] == "admin"


# ---------------------------------------------------------------- 使用量
def test_usage_records_accumulate_per_hour():
    moment = dt.datetime(2026, 11, 18, 10, 5)
    with session_scope() as session:
        usage_store.record(session, username="u-usage-1", delivered=2, attempted=3, tasks=1,
                           cost=0.2, now=moment)
        usage_store.record(session, username="u-usage-1", delivered=1, attempted=1, tasks=1,
                           now=moment)
        totals = usage_store.totals(session, username="u-usage-1")
    assert totals["images"] == 3 and totals["images_attempted"] == 4
    assert totals["tasks"] == 2 and round(totals["cost_cny"], 4) == 0.2


def test_usage_buckets_support_four_granularities():
    moment = dt.datetime(2026, 11, 18, 10, 5)
    with session_scope() as session:
        usage_store.record(session, username="u-usage-1", delivered=2, tasks=1, now=moment)
        for granularity in ("hour", "day", "week", "month"):
            rows = usage_store.buckets(session, granularity=granularity, count=30,
                                          username="u-usage-1", now=moment)
            assert len(rows) == 30
            assert rows[-1]["images"] >= 2                       # 当前桶含本次记账
            assert rows[-1]["key"]
            if granularity == "hour":
                assert "T18" in rows[-1]["key"]                # 10:05 + 8h = Asia/Shanghai 18 点
                assert rows[-1]["by_user"]["u-usage-1"]["images"] >= 2


def test_usage_buckets_filter_by_user():
    moment = dt.datetime(2026, 11, 18, 10, 5)
    with session_scope() as session:
        usage_store.record(session, username="u-usage-1", delivered=2, tasks=1, now=moment)
        usage_store.record(session, username="u-usage-2", delivered=5, tasks=1, now=moment)
        mine = usage_store.buckets(session, granularity="day", count=3,
                                   username="u-usage-2", now=moment)
    assert mine[-1]["images"] == 5
    assert set(mine[-1]["by_user"]) == {"u-usage-2"}


# ---------------------------------------------------------------- outbox
def test_outbox_enqueue_and_replay(monkeypatch):
    with session_scope() as session:
        outbox_store.enqueue(session, task_id="t-1", payload={"task_id": "t-1"},
                             error="boom")

    class OkCatalog:
        name = "ok"

        async def save_result(self, payload):
            return {"saved": 1, "deduped": 0}

    with session_scope() as session:
        before = outbox_store.stats(session).get("pending", 0)
        due = dt.datetime.now() + dt.timedelta(minutes=5)
        assert len(outbox_store.pending(session, now=due)) >= 1
        result = asyncio.run(outbox_store.replay(session, catalog=OkCatalog(), now=due))
        assert result["sent"] >= 1
        assert outbox_store.stats(session).get("pending", 0) <= before
        assert outbox_store.pending(session) == []


def test_outbox_marks_failure_and_backoff():
    class BadCatalog:
        name = "bad"

        async def save_result(self, payload):
            raise RuntimeError("still down")

    with session_scope() as session:
        outbox_store.enqueue(session, task_id="t-2", payload={})
    with session_scope() as session:
        due = dt.datetime.now() + dt.timedelta(minutes=5)
        result = asyncio.run(outbox_store.replay(session, catalog=BadCatalog(),
                                                 limit=5, now=due))
        assert result["failed"] >= 1
        stats = outbox_store.stats(session)
        assert stats["pending"] + stats["failed"] >= 1


def test_outbox_payload_is_stored_as_data():
    with session_scope() as session:
        outbox_store.enqueue(session, task_id="t-3",
                             payload={"task_id": "t-3", "results": [{"slot": 1}]})
        rows = outbox_store.list_outbox(session)
    assert rows[0]["task_id"] == "t-3"
    assert rows[0]["status"] in {"pending", "sent", "failed"}
