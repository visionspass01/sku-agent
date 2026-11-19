"""第三轮 P0-2：看门狗只拾取"未被认领的 queued / 租约过期的 running"。

背景（评审证据）：旧实现把**所有 queued** 都当恢复候选，每次扫描都 +recoveries；
批量任务排队超过 90–120 秒就被判 `IMG_RECOVERY_EXHAUSTED`（任务根本没跑过）。
这里用 `claim()` / `claim_running()` 的 CAS 认领 + 排队宽限 + 只在认领成功时计数。
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import get_image_settings, reset_image_settings
from app.agents_image.dispatch.local_generator import LocalGenerator
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import quota as quota_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    monkeypatch.delenv("IMAGE_PRODUCT_SOURCE", raising=False)
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()
    image_metrics.reset()
    ensure_image_schema()
    with session_scope() as session:
        user_store.ensure_seed_users(session)
    yield app_client
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()


@pytest.fixture(autouse=True)
def _clean_probe_tasks():
    """会话级库里会残留上一条探针任务：把它们标成 done，避免干扰"候选集合"断言。"""
    _purge()

    yield
    _purge()


def _purge() -> None:
    """会话级库里可能残留别的用例的 queued/running 任务：先标 done，保证候选集合干净。"""
    with session_scope() as session:
        for row in task_store.list_tasks(session, limit=500):
            if row.status in {"queued", "running"}:
                task_store.update_task(session, row.task_id, status="done")


PREFIX = "202611191000000a"


def _seed(task_id: str, *, status: str = "queued", created_ago: float = 0,
          lease_delta: float | None = None, recoveries: int = 0,
          username: str = "admin") -> None:
    with session_scope() as session:
        task_store.create_task(session, task_id=task_id, username=username,
                               department=None, batch_id="",
                               prompt="看门狗探针", platform="淘系", aspect="1:1",
                               n=1, ref_asset_ids=[], product_id="P-3001", image_count=1)
        fields: dict = {"status": status, "recoveries": recoveries}
        if created_ago:
            fields["created_at"] = dt.datetime.now() - dt.timedelta(seconds=created_ago)
        if lease_delta is not None:
            fields["lease_until"] = dt.datetime.now() + dt.timedelta(seconds=lease_delta)
        if status == "running":
            fields["started_at"] = dt.datetime.now() - dt.timedelta(seconds=created_ago or 0)
        task_store.update_task(session, task_id, **fields)


def _state(task_id: str) -> dict:
    with session_scope() as session:
        row = task_store.get_task(session, task_id)
        return {"status": row.status, "recoveries": row.recoveries,
                "lease": row.lease_until, "error": row.error_code,
                "settled": row.settled_at}


def _stub_submit(generator) -> list[str]:
    submitted: list[str] = []

    async def fake(task_id: str, *, force: bool = False) -> str:
        submitted.append(task_id)
        return task_id

    generator.submit = fake  # type: ignore[method-assign]
    return submitted


def test_fresh_queued_task_is_not_bumped_within_grace(env, monkeypatch):
    """刚建出来的 queued 任务在宽限期内不被看门狗碰（不会被 +recoveries，更不会被误杀）。"""
    monkeypatch.setenv("IMAGE_CLAIM_GRACE_SECONDS", "30")
    reset_image_settings()
    _seed("202611191000000a01", status="queued", created_ago=1)
    generator = LocalGenerator()
    _stub_submit(generator)
    assert asyncio.run(generator.resume_pending()) == []
    state = _state("202611191000000a01")
    assert state["status"] == "queued"
    assert state["recoveries"] == 0


def test_orphan_queued_task_is_claimed_after_grace(env, monkeypatch):
    """超过宽限仍未被认领 = 孤儿（创建请求崩了/派发失败），看门狗认领并只计一次。"""
    monkeypatch.setenv("IMAGE_CLAIM_GRACE_SECONDS", "0")
    reset_image_settings()
    _seed("202611191000000a02", status="queued", created_ago=120)
    generator = LocalGenerator()
    submitted = _stub_submit(generator)
    resumed = asyncio.run(generator.resume_pending())
    assert resumed == ["202611191000000a02"]
    assert submitted == ["202611191000000a02"]
    state = _state("202611191000000a02")
    assert state["recoveries"] == 1
    assert state["lease"] is not None           # 已被认领，后续扫描不会再抢


def test_claimed_queued_task_is_not_counted_again(env, monkeypatch):
    """重复扫描：已认领（lease 有效）的 queued 任务不再被计数——旧实现每次扫描都 +1。"""
    monkeypatch.setenv("IMAGE_CLAIM_GRACE_SECONDS", "0")
    reset_image_settings()
    _seed("202611191000000a03", status="queued", created_ago=120)
    generator = LocalGenerator()
    _stub_submit(generator)
    for _ in range(4):
        asyncio.run(generator.resume_pending())
    state = _state("202611191000000a03")
    assert state["status"] == "queued"            # 不会被判失败
    assert state["recoveries"] == 1               # 只有第一次认领时计数


def test_lease_expired_running_is_claimed_once(env, monkeypatch):
    """租约过期的 running：认领一次、计数一次；第二次扫描时 lease 已被刷新，不再重投。"""
    monkeypatch.setenv("IMAGE_CLAIM_GRACE_SECONDS", "0")
    reset_image_settings()
    _seed("202611191000000a04", status="running", created_ago=600, lease_delta=-30)
    generator = LocalGenerator()
    _stub_submit(generator)
    first = asyncio.run(generator.resume_pending())
    second = asyncio.run(generator.resume_pending())
    assert first == ["202611191000000a04"]
    assert second == []
    assert _state("202611191000000a04")["recoveries"] == 1


def test_live_lease_running_is_skipped(env, monkeypatch):
    monkeypatch.setenv("IMAGE_CLAIM_GRACE_SECONDS", "0")
    reset_image_settings()
    _seed("202611191000000a05", status="running", created_ago=600, lease_delta=120)
    generator = LocalGenerator()
    _stub_submit(generator)
    assert asyncio.run(generator.resume_pending()) == []
    assert _state("202611191000000a05")["recoveries"] == 0


def test_recoveries_exhausted_marks_failed_and_releases_quota(env, monkeypatch):
    """重投到上限：判失败 + 退回预留（走统一结算，避免二次释放）。"""
    monkeypatch.setenv("IMAGE_CLAIM_GRACE_SECONDS", "0")
    reset_image_settings()
    max_recoveries = int(get_image_settings().max_recoveries)
    with session_scope() as session:
        quota_store.grant(session, "worker1", period_type="week", period_limit=10)
        row = quota_store.get_or_create(session, "worker1")
        row.used = 0
        row.reserved = 0
        assert quota_store.reserve(session, "worker1", 1) is True
    _seed("202611191000000a06", status="running", created_ago=600, lease_delta=-30,
          recoveries=max_recoveries, username="worker1")
    generator = LocalGenerator()
    _stub_submit(generator)
    assert asyncio.run(generator.resume_pending()) == []
    state = _state("202611191000000a06")
    assert state["status"] == "failed"
    assert state["error"] == "IMG_RECOVERY_EXHAUSTED"
    assert state["settled"] is not None
    with session_scope() as session:
        rows = {row["username"]: row for row in quota_store.list_quotas(session)}
    assert rows["worker1"]["reserved"] == 0


def test_claim_is_a_cas(env):
    """并发认领：同一个 queued 任务只可能被认领一次（第二个返回 False）。"""
    _seed("202611191000000a07", status="queued", created_ago=60)
    with session_scope() as session:
        assert task_store.claim(session, "202611191000000a07", lease_seconds=300) is True
    with session_scope() as session:
        assert task_store.claim(session, "202611191000000a07", lease_seconds=300) is False


def test_claim_refuses_terminal_tasks(env):
    _seed("202611191000000a08", status="done")
    with session_scope() as session:
        assert task_store.claim(session, "202611191000000a08", lease_seconds=300) is False
