"""最小编排（graph）与重启恢复（dispatch）：节点顺序、失败落库、续跑。"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.config import reset_image_settings
from app.agents_image.dispatch.local_generator import LocalGenerator
from app.agents_image.graph import execute_task
from app.agents_image.ids import new_task_id
from app.agents_image.registry import reset_registry
from app.agents_image.store import assets as asset_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store.schema import ensure_image_schema
from app.core.errors import ImageError, raise_image
from tests.test_image_storage import make_png


@pytest.fixture()
def image_db(tmp_path, monkeypatch, _db):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    reset_image_settings()
    reset_registry()
    image_metrics.reset()
    ensure_image_schema()
    yield
    reset_image_settings()
    reset_registry()
    image_metrics.reset()


def _seed_task(ref_count: int = 1, *, n: int = 1, status: str = "queued",
               created_at: dt.datetime | None = None) -> tuple[str, list[str]]:
    from app.agents_image.sources.fake_catalog import _PRODUCTS
    from app.db.engine import session_scope

    with session_scope() as session:
        ref_ids = []
        for index in range(ref_count):
            asset = asset_store.save_bytes(session, make_png(64, 64), kind="ref",
                                           product_id="P-1001")
            ref_ids.append(asset.asset_id)
        task = task_store.create_task(
            session, task_id=new_task_id(), username="anonymous", department=None,
            prompt="白底主图，正面全身像上装突出", platform="淘系", aspect="1:1", n=n,
            ref_asset_ids=ref_ids, product_id="P-1001",
            attrs=dict(_PRODUCTS["P-1001"]["attrs"]),
        )
        task.status = status
        task.created_at = created_at or dt.datetime.now()
        return task.task_id, ref_ids


def test_execute_task_runs_nodes_in_order(image_db):
    task_id, _refs = _seed_task(n=2)
    state = asyncio.run(execute_task(task_id))
    stages = [item["stage"] for item in state.timeline]
    assert stages[:5] == ["understand", "compose_prompt", "retrieve", "validate", "generate"]
    assert stages[-2:] == ["aggregate", "backfill"]      # 回写中台在交付之后
    assert state.delivered_images == 2
    assert len(state.prompt_pack) == 2
    assert "must_not_change" in state.prompt_pack[0].preserve or state.prompt_pack[0].preserve

    from app.db.engine import session_scope

    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        assert task.status == "done"
        assert task.delivered_images == 2
        assert task.backfill_status in {"sent", "failed"}   # fake catalog 回写成功 → sent
        outputs = asset_store.list_outputs(session, task_id)
        assert len(outputs) == 2


def test_provider_failure_marks_task_failed(image_db, monkeypatch):
    task_id, _refs = _seed_task()

    async def boom(self, req):
        raise_image("IMG_PROVIDER_UNAVAILABLE", "provider 挂了")

    monkeypatch.setattr("app.agents_image.providers.fake.FakeProvider.generate", boom)

    async def run_and_expect_failure():
        with pytest.raises(ImageError):
            await execute_task(task_id)

    asyncio.run(run_and_expect_failure())

    from app.db.engine import session_scope

    generator = LocalGenerator()

    async def run_until_settled():
        # 必须**在同一个事件循环里**等它落库：`asyncio.run(submit(...))` 返回后会立刻关闭
        # 循环，后台任务被取消——只是以前这条路没有 await 点才"看起来"跑完了。
        # 上一行直接跑了 execute_task（失败），租约仍在这条任务上；这里 force 模拟"调度器接管"
        # （真实链路里重试会经 reset_for_retry 清掉租约，不走 force）。
        await generator.submit(task_id, force=True)
        for _ in range(400):
            with session_scope() as session:
                task = task_store.get_task(session, task_id)
                if task is not None and task.status in {"failed", "done"}:
                    return task.status
            await asyncio.sleep(0.05)
        return None

    assert asyncio.run(run_until_settled()) == "failed"


def test_planned_image_count_is_single_source_of_truth(image_db):
    """P2-7（第九轮）：执行期的"计划张数"以任务台账 `image_count` 为准，不是 `n`。

    场景：图位 6 个（n=6）但计划只出 2 张（iterate 差额/用户减量）——执行体必须只出 2 张，
    并且部分成功判定以 2 为"预期"，不能按 6 判成部分成功（旧实现用 task.n 各算一遍）。
    """
    task_id, refs = _seed_task(n=6)
    # image_count 单独改成 2（模拟建任务期算出的差额）
    from app.db.engine import session_scope

    with session_scope() as session:
        task_store.update_task(session, task_id, image_count=2)

    state = asyncio.run(execute_task(task_id))
    assert state.count == 2, "执行体的计划张数必须是 image_count"
    assert state.attempted_images == 2, "只按计划张数出图"
    assert state.delivered_images == 2
    assert state.partial is False, "按 2 张的预期不算部分成功"
    assert "IMG_SLOT_MISSING" not in " ".join(state.warnings)


def test_plan_policy_uses_task_category(image_db, monkeypatch):
    """P1-2（第十轮）：执行期取图位策略必须带任务 attrs 的 category（与建任务期同参）。"""
    from app.agents_image.sources.fake_catalog import FakeCatalog

    seen: dict = {}
    real = FakeCatalog.get_plan_policy

    async def spy(self, **kwargs):
        seen.update(kwargs)
        return await real(self, **kwargs)

    monkeypatch.setattr(FakeCatalog, "get_plan_policy", spy)
    task_id, _refs = _seed_task(n=1)
    asyncio.run(execute_task(task_id))
    assert seen.get("platform") == "淘系"
    assert seen.get("category") == "女装", "执行期必须带上任务类目（否则策略口径与创建期不一致）"


def test_recovery_requeues_stale_task(image_db):
    """看门狗重投 stale queued 任务，并且任务能真正跑完。

    注意：`resume_pending()` 只是把任务排进当前事件循环；一旦 `asyncio.run` 返回、循环关闭，
    后台任务会被取消。所以必须**在同一个循环里**等它落终态（P2-4 把参考图读取改成线程池后，
    执行体会真正让出事件循环，旧的"靠循环关闭前同步跑完"的巧合不再成立）。
    """
    stale_created = dt.datetime.now() - dt.timedelta(hours=1)
    task_id, _refs = _seed_task(status="queued", created_at=stale_created)

    generator = LocalGenerator()

    async def run_until_settled():
        requeued = await generator.resume_pending()
        assert task_id in requeued
        for _ in range(400):
            from app.db.engine import session_scope

            with session_scope() as session:
                status = task_store.get_task(session, task_id).status
            if status in {"done", "failed"}:
                return status
            await asyncio.sleep(0.05)
        return None

    assert asyncio.run(run_until_settled()) == "done"
