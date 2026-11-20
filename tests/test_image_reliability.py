"""M4：P1 可靠性（执行期 fail-fast / 清理一致 / 恢复看门狗 / SSE 3s / provider 并发 10 / 死配置 / 指标口径）。"""

from __future__ import annotations

import asyncio
import datetime as dt
import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import get_image_settings, reset_image_settings
from app.agents_image.generator import handler as generator_handler
from app.agents_image.providers.fake import FakeProvider
from app.agents_image.providers.image_provider import ImageResult
from app.agents_image.sources import product_source as ps
from app.agents_image.store import assets as asset_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.core.errors import ImageError
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
    image_metrics.reset()
    generator_handler.reset_provider_semaphore()
    ensure_image_schema()
    with session_scope() as session:
        user_store.ensure_seed_users(session)
    yield app_client
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    generator_handler.reset_provider_semaphore()


def login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


# ---------------------------------------------------------------- P1-1 执行期 fail-fast
def test_platform_unavailable_at_execution_fails_task(env, monkeypatch):
    """执行期取平台规则失败 → 任务失败（与创建期 503 同口径，不再 fail-open）。"""
    from app.agents_image import graph

    catalog = ps.get_catalog()

    async def boom(*, platform=""):
        raise ImageError("IMG_UPSTREAM_MCP_UNAVAILABLE", "中台不可用（执行期注入）", retryable=True)

    monkeypatch.setattr(catalog, "get_platform_rules", boom)
    task_id = "20261118000000cccc"
    with session_scope() as session:
        ref = asset_store.save_bytes(session, b"\x89PNG\r\n\x1a\n" + b"0" * 60, kind="ref",
                                     product_id="P-1001")
        task_store.create_task(session, task_id=task_id, username="admin", department=None,
                               prompt="白底主图", platform="淘系", aspect="1:1", n=1,
                               ref_asset_ids=[ref.asset_id], product_id="P-1001",
                               image_count=1)
    with pytest.raises(ImageError) as exc:
        asyncio.run(graph.execute_task(task_id))
    assert exc.value.code == "IMG_UPSTREAM_MCP_UNAVAILABLE"


# ---------------------------------------------------------------- P1-4 清理一致
def test_sweep_marks_purged_assets(env, tmp_path):
    from app.agents_image.runtime import sweep_local_files

    created = env.post("/api/image/tasks", headers=login(env),
                       json={"product_ids": ["P-3002"], "prompt": "白底主图", "image_count": 1})
    task_id = created.json()["tasks"][0]["task_id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}",
                         headers=login(env)).json()
        if detail["status"] in {"done", "failed"}:
            break
        time.sleep(0.05)
    with session_scope() as session:
        outputs = asset_store.list_outputs(session, task_id)
        assert outputs
        keys = [asset.key for asset in outputs]

    # 把所有输出文件改成"25 小时前"，再跑清理
    import os

    for key in keys:
        path = get_image_settings().images_dir / key
        old = dt.datetime.now() - dt.timedelta(hours=25)
        os.utime(path, (old.timestamp(), old.timestamp()))
    result = sweep_local_files()
    assert result["removed"] >= 1
    assert result["purged"] >= 1
    with session_scope() as session:
        purged = [asset for asset in asset_store.list_outputs(session, task_id)
                  if asset.purged_at is not None]
    assert purged, "清理文件后资产行必须标记 purged_at（避免详情里有图但打不开）"


def test_token_blacklist_is_swept(env):
    from app.agents_image.runtime import sweep_tokens
    from app.agents_image.store import tokens as token_store

    with session_scope() as session:
        token_store.revoke(session, "jti-expired-1", time.time() - 10)
    assert sweep_tokens() >= 1


# ---------------------------------------------------------------- P1-5 看门狗
def test_recovery_loop_is_wired(env):
    """恢复扫描是**周期任务**（不再只在启动时跑一次）。"""
    source = (__import__("pathlib").Path(__file__).resolve().parents[1]
              / "src/app/agents_image/runtime.py").read_text(encoding="utf-8")
    assert "_recovery_loop" in source
    assert "recovery_interval_seconds" in source


def test_resume_picks_up_lease_expired_running(env, monkeypatch):
    from app.agents_image.dispatch.local_generator import LocalGenerator

    stale = dt.datetime.now() - dt.timedelta(hours=1)
    with session_scope() as session:
        task_store.create_task(session, task_id="20261118000000dddd", username="admin",
                               department=None, prompt="白底主图", platform="淘系",
                               aspect="1:1", n=1, ref_asset_ids=[], product_id="P-1001",
                               image_count=1)
        task_store.update_task(session, "20261118000000dddd", status="running",
                               started_at=stale)
    generator = LocalGenerator()
    resumed = asyncio.run(generator.resume_pending())
    assert "20261118000000dddd" in resumed


def test_lease_expired_running_is_resumed_with_recoveries(env, monkeypatch):
    """W1：租约过期的 running 会被重投，并累加 `recoveries`。"""
    from app.agents_image.dispatch.local_generator import LocalGenerator

    with session_scope() as session:
        task_store.create_task(session, task_id="20261118000000aa11", username="admin",
                               department=None, prompt="白底主图", platform="淘系",
                               aspect="1:1", n=1, ref_asset_ids=[], product_id="P-1001",
                               image_count=1)
        task_store.update_task(session, "20261118000000aa11", status="running",
                               started_at=dt.datetime.now() - dt.timedelta(hours=1),
                               lease_until=dt.datetime.now() - dt.timedelta(seconds=30))
    generator = LocalGenerator()
    resumed = asyncio.run(generator.resume_pending())
    assert "20261118000000aa11" in resumed
    with session_scope() as session:
        task = task_store.get_task(session, "20261118000000aa11")
        assert task.recoveries == 1


def test_live_lease_is_not_recovered(env):
    """W1：租约未过期的在途任务**不能被**看门狗重投（否则同一任务跑两遍、成本翻倍）。"""
    from app.agents_image.dispatch.local_generator import LocalGenerator

    with session_scope() as session:
        task_store.create_task(session, task_id="20261118000000aa22", username="admin",
                               department=None, prompt="白底主图", platform="淘系",
                               aspect="1:1", n=1, ref_asset_ids=[], product_id="P-1001",
                               image_count=1)
        # 启动时间很旧，但租约刚续过 → 仍在途
        task_store.update_task(session, "20261118000000aa22", status="running",
                               started_at=dt.datetime.now() - dt.timedelta(hours=2),
                               lease_until=dt.datetime.now() + dt.timedelta(seconds=120))
    generator = LocalGenerator()
    resumed = asyncio.run(generator.resume_pending())
    assert "20261118000000aa22" not in resumed
    with session_scope() as session:
        task = task_store.get_task(session, "20261118000000aa22")
        assert task.recoveries == 0


def test_recoveries_exhausted_marks_failed_and_releases_quota(env):
    """W1：重投次数超限 → 判失败（IMG_RECOVERY_EXHAUSTED）并释放预留额度，不再无限重投。"""
    from app.agents_image.dispatch.local_generator import LocalGenerator
    from app.agents_image.store import quota as quota_store

    with session_scope() as session:
        quota_store.grant(session, "worker1", period_type="day", period_limit=50)
        # 基线：别的用例可能给 worker1 留过预留（测试库 session 级），不断言绝对 0
        baseline = {row["username"]: row
                    for row in quota_store.list_quotas(session)}["worker1"]["reserved"]
        assert quota_store.reserve(session, "worker1", 2) is True
        task_store.create_task(session, task_id="20261118000000aa33", username="worker1",
                               department="男装", prompt="白底主图", platform="淘系",
                               aspect="1:1", n=2, ref_asset_ids=[], product_id="P-1002",
                               image_count=2)
        task_store.update_task(session, "20261118000000aa33", status="running",
                               started_at=dt.datetime.now() - dt.timedelta(hours=1),
                               lease_until=dt.datetime.now() - dt.timedelta(seconds=30),
                               recoveries=int(get_image_settings().max_recoveries))

    generator = LocalGenerator()
    assert asyncio.run(generator.resume_pending()) == []
    with session_scope() as session:
        task = task_store.get_task(session, "20261118000000aa33")
        status, code = task.status, task.error_code
        rows = {row["username"]: row for row in quota_store.list_quotas(session)}
    assert status == "failed"
    assert code == "IMG_RECOVERY_EXHAUSTED"
    assert rows["worker1"]["reserved"] == baseline    # 本任务预留的 2 张必须还回去


# ---------------------------------------------------------------- P1-7 SSE / 并发
def test_sse_poll_interval_is_three_seconds():
    from app.agents_image.api import stream_routes

    assert stream_routes._POLL_SECONDS == 3.0


def test_provider_concurrency_cap_is_ten(env, monkeypatch):
    """20 个并发调用同时压 provider：在途峰值必须 ≤ 10。"""
    from app.agents_image.png import placeholder_png

    peak = {"value": 0}
    current = {"value": 0}
    lock = asyncio.Lock()

    async def slow(self, req):
        async with lock:
            current["value"] += 1
            peak["value"] = max(peak["value"], current["value"])
        await asyncio.sleep(0.05)
        async with lock:
            current["value"] -= 1
        return ImageResult(images=[placeholder_png("x", 64, 64)], mime="image/png",
                           provider="fake", model="slow", cost_cny=0.0, latency_ms=1)

    monkeypatch.setattr(FakeProvider, "generate", slow)
    assert get_image_settings().provider_concurrency == 10

    ref = {"data": placeholder_png("r", 64, 64), "mime": "image/png", "sha256": "r"}

    async def run_many():
        tasks = []
        for index in range(20):
            tasks.append(generator_handler.run({
                "provider": "fake", "refs": [ref],
                "slots": [{"slot": 1, "prompt": f"p{index}", "n": 1}],
                "width": 64, "height": 64, "preserve": [],
            }))
        return await asyncio.gather(*tasks)

    asyncio.run(run_many())
    assert peak["value"] <= 10, peak
    assert peak["value"] >= 2, "并发没跑起来（信号量配置或 gather 有问题）"


# ---------------------------------------------------------------- P1-8 死配置
def test_dead_configs_are_gone_or_wired(env):
    settings = get_image_settings()
    assert not hasattr(settings, "vision_provider")          # 从没被实现过，已删除
    health = env.get("/api/image/health").json()
    assert "vision_provider" not in health
    assert health["provider_concurrency"] == 10
    assert "provider_in_flight" in health


def test_cost_budget_gate(env, monkeypatch):
    monkeypatch.setenv("IMAGE_COST_PER_IMAGE_CNY", "1.0")
    monkeypatch.setenv("IMAGE_COST_BUDGET_CNY", "0.5")
    reset_image_settings()
    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1002"], "prompt": "白底主图", "image_count": 2,
    })
    assert resp.status_code == 429
    assert resp.json()["error_code"] == "IMG_COST_BUDGET_EXCEEDED"


def test_critic_threshold_comes_from_manifest(env):
    from app.agents_image.nodes import critic as critic_mod
    from app.agents_image.state import ImageTaskState

    manifest = {"critic": {"pass_score": 101}}               # 故意设成不可能通过
    state = ImageTaskState(prompt="p", aspect="1:1", count=1)
    result = critic_mod.critique(state, [], [], manifest=manifest)
    assert result.passed is False


# ---------------------------------------------------------------- P1-9 指标口径
def test_metrics_cards_are_db_based(env):
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-1002"], "prompt": "白底主图", "image_count": 1})
    task_id = created.json()["tasks"][0]["task_id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail["status"] in {"done", "failed"}:
            break
        time.sleep(0.05)
    body = env.get("/api/image/admin/metrics?granularity=day&count=3", headers=headers).json()
    cards = body["cards"]
    assert cards["tasks_done"] >= 1
    assert cards["images_delivered"] >= 1
    # 在途以 DB 为准（第三轮 P2-7）；本实例在途单独给，不叠加
    assert cards["in_flight_scope"] == "db"
    assert cards["in_flight"] == cards["tasks_running_db"]
    assert "in_flight_instance" in cards
    assert "tasks_running_db" in cards
    assert body["runtime"]["scope"] == "instance"
    assert body["runtime"]["provider_in_flight"] >= 0


def test_metrics_cards_follow_account_and_department_filter(env):
    """P2-3：卡片要跟 `?username=` / `?department=` 一致，不能是全局数。"""
    admin = login(env)
    worker1 = login(env, "worker1", "hhw111")
    env.post("/api/image/tasks", headers=worker1,
             json={"product_ids": ["P-1002"], "prompt": "白底主图", "image_count": 1})
    scoped = env.get("/api/image/admin/metrics?granularity=day&count=3&username=worker1",
                     headers=admin).json()
    assert scoped["username"] == "worker1"
    assert scoped["cards"]["usage_images"] >= 0
    by_dept = env.get("/api/image/admin/metrics?granularity=day&count=3&department=%E7%94%B7%E8%A3%85",
                      headers=admin).json()
    assert by_dept["department"] == "男装"
    assert by_dept["cards"]["tasks_done"] >= 0


def test_health_no_generator_dependency(env):
    """health 不该因为生成器未初始化而报错（provider_in_flight 兜底 0）。"""
    assert env.get("/api/image/health").status_code == 200
