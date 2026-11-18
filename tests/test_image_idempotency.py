"""M2：幂等（Idempotency-Key）、配额预留结算、成本取 provider 真实计费、取消两分支。"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.providers.image_provider import ImageResult
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
    monkeypatch.delenv("IMAGE_PRODUCT_SOURCE", raising=False)
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    image_metrics.reset()
    ensure_image_schema()
    with session_scope() as session:
        user_store.ensure_seed_users(session)
    yield app_client
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()


def login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _set_quota(username: str, limit: int) -> None:
    with session_scope() as session:
        quota_store.grant(session, username, period_type="day", period_limit=limit)


def _quota_row(username: str) -> dict:
    with session_scope() as session:
        rows = {row["username"]: row for row in quota_store.list_quotas(session)}
    return rows.get(username, {})


def _count_tasks(username: str) -> int:
    with session_scope() as session:
        return len(task_store.list_tasks(session, username=username, limit=200))


# ---------------------------------------------------------------- 幂等
def test_idempotency_key_replays_first_response(env):
    headers = login(env)
    body = {"product_ids": ["P-1001"], "prompt": "白底主图", "image_count": 1}
    before = _count_tasks("admin")
    first = env.post("/api/image/tasks", headers={**headers, "Idempotency-Key": "idem-1"},
                     json=body)
    assert first.status_code == 201, first.text
    after_first = _count_tasks("admin")
    second = env.post("/api/image/tasks", headers={**headers, "Idempotency-Key": "idem-1"},
                      json=body)
    assert second.status_code == 200
    assert second.json()["idempotent"] is True
    assert second.json()["batch_id"] == first.json()["batch_id"]
    assert _count_tasks("admin") == after_first == before + len(first.json()["tasks"])


def test_request_id_in_body_also_dedupes(env):
    headers = login(env)
    body = {"product_ids": ["P-1002"], "prompt": "白底主图", "request_id": "req-body-1"}
    first = env.post("/api/image/tasks", headers=headers, json=body)
    second = env.post("/api/image/tasks", headers=headers, json=body)
    assert first.status_code == 201 and second.status_code == 200
    assert second.json()["batch_id"] == first.json()["batch_id"]


def test_different_keys_create_different_batches(env):
    headers = login(env)
    body = {"product_ids": ["P-3001"], "prompt": "白底主图"}
    first = env.post("/api/image/tasks", headers={**headers, "Idempotency-Key": "idem-a"},
                     json=body)
    second = env.post("/api/image/tasks", headers={**headers, "Idempotency-Key": "idem-b"},
                      json=body)
    assert first.json()["batch_id"] != second.json()["batch_id"]


# ---------------------------------------------------------------- 配额预留
def test_reserve_then_commit_on_success(env):
    headers = login(env, "worker1", "hhw111")
    _set_quota("worker1", 5)
    before = _quota_row("worker1")
    resp = env.post("/api/image/tasks", headers=headers,
                    json={"product_ids": ["P-1002"], "prompt": "白底主图", "n": 2})
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["tasks"][0]["task_id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail["status"] in {"done", "failed"} and detail["backfill_status"] != "pending":
            break
        time.sleep(0.05)
    row = _quota_row("worker1")
    assert row["used"] >= before.get("used", 0) + detail["delivered_images"]
    assert row["remaining"] >= 0


def test_reserve_blocks_over_quota_before_creating_tasks(env):
    headers = login(env, "worker2", "hhw222")
    _set_quota("worker2", 1)
    resp = env.post("/api/image/tasks", headers=headers,
                    json={"product_ids": ["P-2001"], "prompt": "白底主图", "n": 6})
    assert resp.status_code == 429
    assert resp.json()["error_code"] == "IMG_QUOTA_EXCEEDED"
    # 失败不残留任务、不残留预留
    assert _count_tasks("worker2") == 0
    assert _quota_row("worker2")["used"] == 0


def test_concurrent_reserve_never_oversells(_db):
    """8 个请求抢 5 张：预留成功次数必须 ≤5（条件更新）。"""
    with session_scope() as session:
        user_store.ensure_seed_users(session)
        quota_store.grant(session, "u-reserve", period_type="day", period_limit=5)

    results: list[bool] = []
    lock = threading.Lock()

    def worker():
        with session_scope() as session:
            ok = quota_store.reserve(session, "u-reserve", 1)
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(1 for ok in results if ok) == 5


def test_commit_and_release_move_reserved_correctly(_db):
    with session_scope() as session:
        user_store.ensure_seed_users(session)
        quota_store.grant(session, "u-settle", period_type="day", period_limit=10)
        assert quota_store.reserve(session, "u-settle", 4) is True
        assert quota_store.commit_reserve(session, "u-settle", 3) is True
        assert quota_store.release(session, "u-settle", 1) is True
        rows = {row["username"]: row for row in quota_store.list_quotas(session)}
    assert rows["u-settle"]["used"] == 3
    assert rows["u-settle"]["remaining"] == 7


# ---------------------------------------------------------------- 成本口径
def test_cost_comes_from_provider_not_settings(env, monkeypatch):
    """provider 返回 0.25 元/张 → 任务成本必须是 0.5（配置单价已不参与计费）。"""
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3003"], "prompt": "白底主图", "n": 2})
    task_id = created.json()["tasks"][0]["task_id"]

    from app.agents_image.providers.fake import FakeProvider

    original = FakeProvider.generate

    async def paid(self, req):
        result = await original(self, req)
        return ImageResult(images=result.images, mime=result.mime, provider=result.provider,
                           model=result.model, cost_cny=0.25 * len(result.images),
                           latency_ms=result.latency_ms, meta=result.meta)

    monkeypatch.setattr(FakeProvider, "generate", paid)

    # 新建一个任务验证（避免与上面的任务混在一起）
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图", "n": 2})
    task_id = created.json()["tasks"][0]["task_id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail["status"] in {"done", "failed"}:
            break
        time.sleep(0.05)
    assert detail["status"] == "done", detail
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        assert task.external_cost_cny > 0
        assert abs(task.cost_cny - task.external_cost_cny) < 1e-6


# ---------------------------------------------------------------- 取消两分支
def test_cancel_before_provider_call(env):
    headers = login(env)
    # 直接把任务置为 queued 且不提交执行，模拟"还没调模型"
    with session_scope() as session:
        task = task_store.create_task(
            session, task_id="20261118000000aaaa", username="admin", department=None,
            prompt="白底主图", platform="淘系", aspect="1:1", n=1,
            ref_asset_ids=[], product_id="P-1001", batch_id="",
        )
        assert task.status == "queued"
    resp = env.post("/api/image/tasks/20261118000000aaaa/cancel", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["canceled"] is True and body["provider_in_flight"] is False
    assert body["message"] == "未调用生图模型的任务请求已取消。"


def test_cancel_after_provider_started_is_not_rollbackable(env):
    headers = login(env)
    with session_scope() as session:
        task_store.create_task(
            session, task_id="20261118000000bbbb", username="admin", department=None,
            prompt="白底主图", platform="淘系", aspect="1:1", n=1,
            ref_asset_ids=[], product_id="P-1001", batch_id="",
        )
        task_store.update_task(session, "20261118000000bbbb", status="running",
                               provider_started_at=dt.datetime.now())
    resp = env.post("/api/image/tasks/20261118000000bbbb/cancel", headers=headers)
    assert resp.status_code == 409
    body = resp.json()
    assert body["canceled"] is False and body["provider_in_flight"] is True
    assert "无法撤回" in body["message"]
    with session_scope() as session:
        assert task_store.get_task(session, "20261118000000bbbb").cancel_requested is True


def test_canceled_task_discards_outputs(_db, env):
    """provider 已返回但 cancel_requested=True → 不交付，任务置 canceled。"""
    from app.agents_image import graph
    from app.agents_image.ids import new_task_id
    from app.agents_image.nodes import plan_slots
    from app.agents_image.png import placeholder_png
    from app.agents_image.sources.fake_catalog import _PRODUCTS
    from app.agents_image.state import ImageTaskState, RefImage, SlotPlan
    from app.agents_image.store import assets as asset_store

    ensure_image_schema()
    task_id = new_task_id()
    with session_scope() as session:
        ref = asset_store.save_bytes(session, placeholder_png("r", 64, 64), kind="ref",
                                     product_id="P-1001")
        state = ImageTaskState(prompt="白底主图", aspect="1:1", count=1,
                               refs=[RefImage(asset_id=ref.asset_id)])
        state.slots = [SlotPlan(**item) for item in
                       plan_slots.build_slots(state, policy=asyncio.run(
                           ps.get_catalog().get_plan_policy()))]
        task_store.create_task(
            session, task_id=task_id, username="u-cancel", department=None,
            prompt=state.prompt, platform="淘系", aspect="1:1", n=1,
            ref_asset_ids=[ref.asset_id], product_id="P-1001",
            attrs=dict(_PRODUCTS["P-1001"]["attrs"]),
            slots=[item.model_dump() for item in state.slots],
            image_count=1,
        )
        asset_store.bind_refs_to_task(session, [ref.asset_id], task_id)
        task_store.update_task(session, task_id, cancel_requested=True)

    result = asyncio.run(graph.execute_task(task_id))
    assert result.delivered_images == 0
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        assert task.status == "canceled"
        assert asset_store.list_outputs(session, task_id) == []
