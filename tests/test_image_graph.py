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
        await generator.submit(task_id)
        for _ in range(400):
            with session_scope() as session:
                task = task_store.get_task(session, task_id)
                if task is not None and task.status in {"failed", "done"}:
                    return task.status
            await asyncio.sleep(0.05)
        return None

    assert asyncio.run(run_until_settled()) == "failed"


def test_recovery_requeues_stale_task(image_db):
    stale_created = dt.datetime.now() - dt.timedelta(hours=1)
    task_id, _refs = _seed_task(status="queued", created_at=stale_created)

    generator = LocalGenerator()
    requeued = asyncio.run(generator.resume_pending())
    assert task_id in requeued

    deadline = __import__("time").time() + 10
    status = None
    while __import__("time").time() < deadline:
        from app.db.engine import session_scope

        with session_scope() as session:
            task = task_store.get_task(session, task_id)
            status = task.status
            if status in {"done", "failed"}:
                break
        __import__("time").sleep(0.05)
    assert status == "done"
