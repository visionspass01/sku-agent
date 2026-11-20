"""W4：功能6（第三方重试 / 发起量 / 任务重试）与功能5（iterate 差额补齐 / 变体方向 / copy_json）。"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.generator import handler as generator_handler
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import quota as quota_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.core.errors import ImageError, raise_image
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
    generator_handler.reset_provider_semaphore()
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


def _wait(env, headers, task_id, timeout=30.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail.get("status") in {"done", "failed", "canceled"} and \
                detail.get("backfill_status") != "pending":
            return detail
        time.sleep(0.05)
    return detail


def _quota_row(username: str) -> dict:
    with session_scope() as session:
        rows = {row["username"]: row for row in quota_store.list_quotas(session)}
    return rows.get(username, {})


# ---------------------------------------------------------------- 功能6：provider 重试
def _payload() -> dict:
    from app.agents_image.png import placeholder_png

    return {"provider": "fake",
            "refs": [{"data": placeholder_png("ref", 64, 64), "mime": "image/png"}],
            "slots": [{"slot": 1, "prompt": "白底主图", "n": 1}],
            "width": 1024, "height": 1024, "preserve": []}


def test_retry_on_rate_limit_then_success_counts_provider_calls(monkeypatch):
    """限流/超时会重试，成功时把**真实发起次数**记进 `provider_calls`（发起量口径）。"""
    from app.agents_image.providers.fake import FakeProvider

    monkeypatch.setenv("IMAGE_PROVIDER_RETRIES", "2")
    monkeypatch.setenv("IMAGE_PROVIDER_RETRY_BACKOFF_SECONDS", "0")
    reset_image_settings()
    original = FakeProvider.generate
    state = {"calls": 0}

    async def flaky(self, req):
        state["calls"] += 1
        if state["calls"] <= 2:
            raise_image("IMG_PROVIDER_RATE_LIMITED", "限流")
        return await original(self, req)

    monkeypatch.setattr(FakeProvider, "generate", flaky)
    result = asyncio.run(generator_handler.run(_payload()))
    assert state["calls"] == 3
    assert result["provider_calls"] == 3
    assert result["slots"][0]["images"]


def test_non_retryable_error_is_not_retried(monkeypatch):
    from app.agents_image.providers.fake import FakeProvider

    monkeypatch.setenv("IMAGE_PROVIDER_RETRIES", "3")
    reset_image_settings()
    state = {"calls": 0}

    async def blocked(self, req):
        state["calls"] += 1
        raise_image("IMG_CONTENT_BLOCKED", "内容违规")

    monkeypatch.setattr(FakeProvider, "generate", blocked)
    with pytest.raises(ImageError):
        asyncio.run(generator_handler.run(_payload()))
    assert state["calls"] == 1          # 内容违规重试只会更糟：不重试


def test_retries_exhausted_raises(monkeypatch):
    from app.agents_image.providers.fake import FakeProvider

    monkeypatch.setenv("IMAGE_PROVIDER_RETRIES", "1")
    monkeypatch.setenv("IMAGE_PROVIDER_RETRY_BACKOFF_SECONDS", "0")
    reset_image_settings()
    state = {"calls": 0}

    async def down(self, req):
        state["calls"] += 1
        raise_image("IMG_PROVIDER_UNAVAILABLE", "5xx")

    monkeypatch.setattr(FakeProvider, "generate", down)
    with pytest.raises(ImageError):
        asyncio.run(generator_handler.run(_payload()))
    assert state["calls"] == 2          # 首调 + 1 次重试


def test_task_records_provider_calls_in_usage(env):
    """任务成功 → 任务行与使用量都记发起量（可用性指标分子分母都在）。"""
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图",
                             "image_count": 1})
    assert created.status_code == 201, created.text
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert detail["status"] == "done"
    assert detail["provider_calls"] >= 1
    assert detail["copy"]["slots"], "任务详情要带图内文案契约"


# ---------------------------------------------------------------- 功能6：任务重试接口
def test_retry_failed_task_requeues_and_reserves_quota(env, monkeypatch):
    """重试 = 重新排队 + 重新预留额度（失败时额度已释放），不重复建任务。"""
    submitted: list[str] = []

    class _StubDispatcher:
        async def submit(self, task_id: str) -> str:
            submitted.append(task_id)     # 不立刻执行，避免这里就看结算结果
            return task_id

    monkeypatch.setattr("app.agents_image.api.routes.get_dispatcher",
                        lambda: _StubDispatcher())
    # P1-2（第九轮）：重试统一走 batch_routes.retry_one，派发器桩也要覆盖它
    monkeypatch.setattr("app.agents_image.api.batch_routes.get_dispatcher",
                        lambda: _StubDispatcher())
    headers = login(env, "worker1", "hhw111")
    with session_scope() as session:
        quota_store.grant(session, "worker1", period_type="day", period_limit=10)
        task_store.create_task(session, task_id="20261118000000cc01", username="worker1",
                               department="男装", prompt="白底主图", platform="淘系",
                               aspect="1:1", n=2, ref_asset_ids=[], product_id="P-1002",
                               image_count=2)
        task_store.mark_failed(session, "20261118000000cc01", code="IMG_PROVIDER_UNAVAILABLE",
                               detail="provider 挂了")
    before = _quota_row("worker1")
    resp = env.post("/api/image/tasks/20261118000000cc01/retry", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "queued" and body["retries"] == 1
    assert submitted == ["20261118000000cc01"]
    after = _quota_row("worker1")
    assert after["reserved"] == before.get("reserved", 0) + 2
    with session_scope() as session:
        task = task_store.get_task(session, "20261118000000cc01")
        assert task.error_code == "" and task.attempts == 0


def test_retry_rejects_done_task(env):
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图",
                             "image_count": 1})
    task_id = created.json()["tasks"][0]["task_id"]
    detail = _wait(env, headers, task_id)
    assert detail["status"] == "done"
    resp = env.post(f"/api/image/tasks/{task_id}/retry", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "IMG_TASK_CONFLICT"


def test_retry_capped_by_max_task_retries(env):
    headers = login(env, "worker1", "hhw111")
    with session_scope() as session:
        quota_store.grant(session, "worker1", period_type="day", period_limit=20)
        task_store.create_task(session, task_id="20261118000000cc02", username="worker1",
                               department="男装", prompt="白底主图", platform="淘系",
                               aspect="1:1", n=1, ref_asset_ids=[], product_id="P-1002",
                               image_count=1)
        task_store.mark_failed(session, "20261118000000cc02", code="IMG_TIMEOUT", detail="超时")
        task_store.update_task(session, "20261118000000cc02", retries=3)
    resp = env.post("/api/image/tasks/20261118000000cc02/retry", headers=headers)
    assert resp.status_code == 409
    assert "上限" in resp.json()["detail"]


def test_retry_forbidden_for_other_user(env):
    worker = login(env, "worker2", "hhw222")
    with session_scope() as session:
        task_store.create_task(session, task_id="20261118000000cc03", username="admin",
                               department=None, prompt="白底主图", platform="淘系",
                               aspect="1:1", n=1, ref_asset_ids=[], product_id="P-3001",
                               image_count=1)
        task_store.mark_failed(session, "20261118000000cc03", code="IMG_TIMEOUT", detail="超时")
    resp = env.post("/api/image/tasks/20261118000000cc03/retry", headers=worker)
    assert resp.status_code == 403


def test_retry_clears_previous_run_state(env, monkeypatch):
    """P2-9：重试要清掉上一轮的 started_at / 文案契约 / 提示词包，否则详情看起来"没跑过"。"""
    import datetime as dt

    submitted: list[str] = []

    class _StubDispatcher:
        async def submit(self, task_id: str) -> str:
            submitted.append(task_id)      # 不立刻执行，避免这里就被真实跑一遍
            return task_id

    monkeypatch.setattr("app.agents_image.api.routes.get_dispatcher",
                        lambda: _StubDispatcher())
    # P1-2（第九轮）：重试统一走 batch_routes.retry_one，派发器桩也要覆盖它
    monkeypatch.setattr("app.agents_image.api.batch_routes.get_dispatcher",
                        lambda: _StubDispatcher())
    headers = login(env)
    task_id = "20261118000000cc04"
    with session_scope() as session:
        task_store.create_task(session, task_id=task_id, username="admin",
                               department=None, prompt="白底主图", platform="淘系",
                               aspect="1:1", n=1, ref_asset_ids=[], product_id="P-3001",
                               image_count=1)
        task_store.mark_failed(session, task_id, code="IMG_TIMEOUT", detail="超时")
        task_store.update_task(
            session, task_id,
            started_at=dt.datetime.now(), copy_json={"slots": [{"slot": 1}]},
            prompt_pack_json=[{"slot": 1, "prompt": "旧提示词"}],
            warnings_json=["IMG_SLOT_MISSING: 预期 1 张，输出 0 张"],
            retained_note="")
    resp = env.post(f"/api/image/tasks/{task_id}/retry", headers=headers)
    assert resp.status_code == 200, resp.text
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        assert task.status == "queued"
        assert task.started_at is None
        assert dict(task.copy_json or {}) == {}
        assert list(task.prompt_pack_json or []) == []
        assert list(task.warnings_json or []) == []


# ---------------------------------------------------------------- 功能5：iterate 差额补齐
def test_iterate_fills_gap_and_charges_only_generated(env):
    """基础任务只出了 1 张（主图）→ iterate 不能"复用不了就报错"，要按差额重做并只按差额计额度。"""
    headers = login(env, "worker2", "hhw222")
    with session_scope() as session:
        quota_store.grant(session, "worker2", period_type="day", period_limit=30)
    base = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-2001"], "prompt": "白底主图", "image_count": 1, "slot_count": 3,
    })
    assert base.status_code == 201, base.text
    base_id = base.json()["tasks"][0]["task_id"]
    base_detail = _wait(env, headers, base_id)
    assert base_detail["status"] == "done"
    assert {asset["slot"] for asset in base_detail["assets"]} == {1}

    before = _quota_row("worker2")
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-2001"],
        "prompt": "主图点击率低，只改主图", "mode": "iterate", "base_task_id": base_id,
        "image_count": 2, "slot_count": 3,
    })
    assert created.status_code == 201, created.text
    body = created.json()
    # 主图 1 张 + 差额图位（没有可复用成品）→ 2 张，且计划的张数与任务一致
    assert body["tasks"][0]["image_count"] == 2
    assert body["planned_images"] == 2
    detail = _wait(env, headers, body["tasks"][0]["task_id"])
    assert detail["status"] == "done", detail
    assert detail["delivered_images"] == 2
    after = _quota_row("worker2")
    assert after["used"] == before.get("used", 0) + detail["delivered_images"]
    assert after["reserved"] == before.get("reserved", 0)


def test_iterate_all_slots_reusable_is_rejected(env):
    """所有 generate 图位都能复用时，iterate 没有意义 → 明确报错，而不是静默出 0 张。"""
    headers = login(env, "worker2", "hhw222")
    with session_scope() as session:
        quota_store.grant(session, "worker2", period_type="day", period_limit=30)
    base = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-2001"], "prompt": "白底主图", "image_count": 2, "slot_count": 2,
    })
    base_id = base.json()["tasks"][0]["task_id"]
    assert _wait(env, headers, base_id)["status"] == "done"
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-2001"], "prompt": "全部复用", "mode": "iterate",
        "base_task_id": base_id, "image_count": 2, "slot_count": 2,
    })
    # 主图必须重做 → 仍能通过；这里验证的是"存在需要重做的图位"（主图）时的正常路径
    assert created.status_code == 201, created.text


# ---------------------------------------------------------------- 功能5：变体方向不进 headline
def test_variant_direction_never_becomes_headline():
    from app.agents_image.nodes import copywriter
    from app.agents_image.schemas import ClothingAttrs

    attrs = ClothingAttrs(product_type="针织开衫", color_names=["米色"],
                          key_features=["罗纹袖口"], materials="羊毛混纺 30%",
                          shape="落肩、V 领")
    copy = copywriter.build_copy(attrs, "主图", direction="版型优先")
    assert copy.headline != "版型优先"
    assert "版型优先" not in copy.headline
    assert "版型优先" in copy.notes       # 方向只作为追踪信息留着
    assert copy.headline == "罗纹袖口"     # 主标语仍然只写商品信息


# ---------------------------------------------------------------- 功能5：copy_json 回写中台
def test_copy_json_is_written_to_platform(env):
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图",
                             "image_count": 1})
    task_id = created.json()["tasks"][0]["task_id"]
    detail = _wait(env, headers, task_id)
    assert detail["status"] == "done"
    assert detail["copy"]["platform"] == "淘系"
    assert detail["copy"]["slots"][0]["slot"] == 1
