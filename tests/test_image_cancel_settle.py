"""第三轮 P0-1：取消/失败必须把预留额度退回去，且只结算一次。

背景（评审证据）：旧实现里"取消一个还没调模型的任务"只把状态改成 canceled，
`image_quota.reserved` 永远不退，用户额度被永久占住；同一笔预留还可能被取消与
失败两条路径各释放一次（第二次会吃掉别人的预留）。这里用 `settled_at` 做结算权。
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import get_image_settings, reset_image_settings
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import quota as quota_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store import usage as usage_store
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


def login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _reset_quota(username: str, limit: int = 50) -> None:
    with session_scope() as session:
        row = quota_store.grant(session, username, period_type="week", period_limit=limit)
        row.used = 0
        row.reserved = 0
        session.flush()


def _quota(username: str) -> dict:
    with session_scope() as session:
        rows = {row["username"]: row for row in quota_store.list_quotas(session)}
    return rows.get(username, {})


def _usage(username: str) -> dict:
    with session_scope() as session:
        return usage_store.totals(session, username=username)


def _stub_dispatcher(monkeypatch, submitted: list[str]) -> None:
    """把派发器换成"只记录不执行"，让任务停在 queued（模拟用户取消的典型时序）。"""

    class _Stub:
        name = "stub"

        async def submit(self, task_id: str) -> str:
            submitted.append(task_id)
            return task_id

        async def cancel(self, task_id: str) -> bool:      # pragma: no cover —— 走真实 dispatcher
            return True

        async def resume_pending(self) -> list[str]:       # pragma: no cover
            return []

        async def health(self) -> dict:                    # pragma: no cover
            return {"ok": True}

    monkeypatch.setattr("app.agents_image.api.routes.get_dispatcher", lambda: _Stub())


def test_cancel_queued_task_releases_reservation(env, monkeypatch):
    """P0-1 主证据：取消未调模型的任务后 reserved 归零，任务记 IMG_TASK_CANCELED。"""
    headers = login(env, "worker2", "hhw222")
    _reset_quota("worker2")
    _stub_dispatcher(monkeypatch, [])
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1001"], "prompt": "白底主图", "image_count": 2, "slot_count": 3,
    })
    assert created.status_code == 201, created.text
    task_id = created.json()["tasks"][0]["task_id"]
    with session_scope() as session:
        assert _quota("worker2")["reserved"] == 2
    before_failed = _usage("worker2").get("failed", 0)

    resp = env.post(f"/api/image/tasks/{task_id}/cancel", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["canceled"] is True

    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        status, code, settled = task.status, task.error_code, task.settled_at
    assert status == "canceled"
    assert code == "IMG_TASK_CANCELED"
    assert settled is not None
    quota = _quota("worker2")
    assert quota["reserved"] == 0            # 额度必须退回
    assert quota["used"] == 0                # 未调模型 → 使用量 0
    assert _usage("worker2")["failed"] == before_failed + 1


def test_cancel_twice_releases_only_once(env, monkeypatch):
    """结算权（settled_at）保证同一笔预留只被释放一次。"""
    from app.agents_image.dispatch.local_generator import LocalGenerator

    headers = login(env, "worker2", "hhw222")
    _reset_quota("worker2")
    _stub_dispatcher(monkeypatch, [])
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-2001"], "prompt": "白底主图", "image_count": 3, "slot_count": 3,
    })
    task_id = created.json()["tasks"][0]["task_id"]

    async def cancel_twice():
        generator = LocalGenerator()
        first = await generator.cancel(task_id)
        second = await generator.cancel(task_id)
        return first, second

    first, second = asyncio.run(cancel_twice())
    assert first is True
    assert second is True                       # 幂等：不再改状态也不报错
    quota = _quota("worker2")
    assert quota["reserved"] == 0
    assert quota["used"] == 0


def test_cancel_after_provider_started_discards_results(env, monkeypatch):
    """已调模型：取消 → 409，执行体丢弃结果并把预留在结算里退掉。"""
    from app.agents_image import graph
    from app.agents_image.providers.fake import FakeProvider

    headers = login(env, "worker2", "hhw222")
    _reset_quota("worker2")
    _stub_dispatcher(monkeypatch, [])
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3003"], "prompt": "白底主图", "image_count": 1,
    })
    task_id = created.json()["tasks"][0]["task_id"]
    with session_scope() as session:
        task_store.update_task(session, task_id, status="running",
                               provider_started_at=dt.datetime.now())
    resp = env.post(f"/api/image/tasks/{task_id}/cancel", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["provider_in_flight"] is True
    with session_scope() as session:
        assert task_store.get_task(session, task_id).cancel_requested is True

    original = FakeProvider.generate

    async def slow(self, req):
        await asyncio.sleep(0.01)
        return await original(self, req)

    monkeypatch.setattr(FakeProvider, "generate", slow)
    asyncio.run(graph.execute_task(task_id))
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        status, attempted, settled = task.status, task.attempted_images, task.settled_at
    assert status == "canceled"
    assert attempted >= 1                      # provider 已经跑过
    assert settled is not None
    quota = _quota("worker2")
    assert quota["reserved"] == 0              # 预留退回
    assert quota["used"] == 0                  # 交付 0 张 → 不占额度


def test_failed_task_settles_once_and_records_usage(env, monkeypatch):
    """失败路径：额度退回 + 使用量只记一次（不再由 graph 与调度器各记一次）。"""
    from app.agents_image.dispatch.local_generator import LocalGenerator

    headers = login(env, "worker2", "hhw222")
    _reset_quota("worker2")
    _stub_dispatcher(monkeypatch, [])
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-2001"], "prompt": "白底主图", "image_count": 2, "slot_count": 2,
    })
    task_id = created.json()["tasks"][0]["task_id"]
    before = _usage("worker2")

    async def boom(task_id: str):
        raise RuntimeError("provider 挂了")

    monkeypatch.setattr("app.agents_image.graph.execute_task", boom)
    generator = LocalGenerator()

    async def run_until_settled():
        await generator.submit(task_id)
        for _ in range(200):
            with session_scope() as session:
                task = task_store.get_task(session, task_id)
                if task is not None and task.status in {"failed", "done"}:
                    return task.status
            await asyncio.sleep(0.02)
        return None

    assert asyncio.run(run_until_settled()) == "failed"
    after = _usage("worker2")
    assert after["failed"] == before.get("failed", 0) + 1
    assert after["tasks"] == before.get("tasks", 0) + 1
    assert _quota("worker2")["reserved"] == 0
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        assert task.settled_at is not None
        assert task.error_code == "IMG_INTERNAL"


def test_retry_resets_settlement_and_recoveries(env, monkeypatch):
    """P1-10/P2-11：重试要把 recoveries/settled_at/告警/被拦图位/上架状态清干净。"""
    headers = login(env, "worker2", "hhw222")
    _reset_quota("worker2")
    _stub_dispatcher(monkeypatch, [])
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-2001"], "prompt": "白底主图", "image_count": 1,
    })
    task_id = created.json()["tasks"][0]["task_id"]
    from app.agents_image import settlement

    with session_scope() as session:
        # 模拟"失败已结算"（额度退回）+ 上一轮残留状态
        task_store.mark_failed(session, task_id, code="IMG_TIMEOUT", detail="超时")
        settlement.settle_cancel(session, task_id)
        task_store.update_task(session, task_id, recoveries=int(get_image_settings().max_recoveries),
                               warnings_json=["旧告警"],
                               blocked_json=[{"slot": 2, "reasons": ["旧门禁"]}],
                               reused_images=3, listing_status="listed",
                               listing_url="https://shop.example.com/x")
    assert _quota("worker2")["reserved"] == 0          # 失败已把预留退回
    resp = env.post(f"/api/image/tasks/{task_id}/retry", headers=headers)
    assert resp.status_code == 200, resp.text
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        assert task.status == "queued"
        assert task.recoveries == 0
        assert task.settled_at is None
        assert task.warnings_json == []
        assert task.blocked_json == []
        assert task.reused_images == 0
        assert task.listing_status == "not_listed"
        assert task.retries == 1
    assert _quota("worker2")["reserved"] == 1     # 重试重新预留


def test_admin_update_missing_user_is_404(env):
    headers = login(env)
    resp = env.put("/api/image/admin/users/ghost-user", headers=headers,
                   json={"enabled": False})
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "IMG_USER_NOT_FOUND"
