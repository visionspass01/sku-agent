"""第九轮 P1-2：重试必须 CAS 抢占，不能并发重复预留额度。

上一版 retry 是「读 status → reserve → reset」：两个并发重试都能 reserve 一份，
只有一次 claim 会成功，多占的额度没有任何路径释放（settle_done 只释放
`planned - charged`），直到周期 rollover——表现为"配额莫名被占住"。
现在先用 `begin_retry` 条件 UPDATE 抢重试权，抢不到直接 409。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.agents_image.auth import AuthUser
from app.agents_image.store import quota as quota_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store.models import ImageQuota
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    from app.agents_image.config import reset_image_settings

    reset_image_settings()
    ensure_image_schema()
    yield
    reset_image_settings()


def _failed_task(username: str, *, planned: int = 2) -> str:
    task_id = f"t-rcas-{uuid.uuid4().hex[:10]}"
    with session_scope() as session:
        quota_store.grant(session, username, period_type="day", period_limit=10)
        task_store.create_task(
            session, task_id=task_id, username=username, department="女装",
            prompt="白底主图", platform="淘系", aspect="1:1", n=planned,
            ref_asset_ids=[], product_id="P-3001", image_count=planned,
        )
        task_store.mark_failed(session, task_id, code="IMG_PROVIDER_UNAVAILABLE",
                               detail="x")
    return task_id


def test_begin_retry_is_cas_once(env):
    username = f"u-rcas-{uuid.uuid4().hex[:8]}"
    task_id = _failed_task(username)

    with session_scope() as session:
        assert task_store.begin_retry(session, task_id, retries=0) is True
        assert quota_store.reserve(session, username, 2) is True
    with session_scope() as session:
        # 第二个并发重试：状态已变/重试次数已变 → CAS 失败，绝不能再次预留
        assert task_store.begin_retry(session, task_id, retries=0) is False
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        quota = session.get(ImageQuota, username)
        status, retries = task.status, task.retries
        reserved = int(quota.reserved or 0)
    assert status == "queued"
    assert retries == 1
    assert reserved == 2, "CAS 只放行一次，所以额度只预留一次"


def _stub_dispatcher(monkeypatch):
    class _Stub:
        async def submit(self, task_id: str) -> str:
            return task_id

    monkeypatch.setattr("app.agents_image.api.batch_routes.get_dispatcher", lambda: _Stub())


def test_retry_one_does_not_double_reserve_under_concurrency(env, monkeypatch):
    """两个线程同时重试同一任务：只能有一个抢到重试权（额度只预留一次）。"""
    from app.agents_image.api import batch_routes

    _stub_dispatcher(monkeypatch)
    username = f"u-rcas2-{uuid.uuid4().hex[:8]}"
    task_id = _failed_task(username)
    user = AuthUser(username=username, role="user", department="女装")

    results: list[str] = []

    def worker():
        try:
            results.append(asyncio.run(batch_routes.retry_one(user, task_id))["status"])
        except Exception as exc:  # noqa: BLE001 —— 抢不到重试权的那一个应该是 409 冲突
            results.append(getattr(exc, "code", type(exc).__name__))

    import threading

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["IMG_TASK_CONFLICT", "queued"], results
    with session_scope() as session:
        quota = session.get(ImageQuota, username)
        task = task_store.get_task(session, task_id)
        reserved = int(quota.reserved or 0)
        retries = task.retries
    assert reserved == 2, "并发重试只允许预留一次"
    assert retries == 1


def test_retry_one_rolls_back_cas_when_quota_insufficient(env, monkeypatch):
    """预留失败时整个事务回滚：任务必须还是 failed、retries 不变（不留半账）。"""
    from app.agents_image.api import batch_routes

    _stub_dispatcher(monkeypatch)
    username = f"u-rcas3-{uuid.uuid4().hex[:8]}"
    task_id = _failed_task(username, planned=2)
    with session_scope() as session:
        quota_store.grant(session, username, period_type="day", period_limit=1)
    user = AuthUser(username=username, role="user", department="女装")

    with pytest.raises(Exception) as caught:
        asyncio.run(batch_routes.retry_one(user, task_id))
    assert getattr(caught.value, "code", "") == "IMG_QUOTA_EXCEEDED"

    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        quota = session.get(ImageQuota, username)
        status, retries = task.status, task.retries
        reserved = int(quota.reserved or 0)
    assert status == "failed", "CAS 必须随事务一起回滚"
    assert retries == 0
    assert reserved == 0
