"""第九轮 P1-5：RAG 召回与修复经验检索必须在线程池里跑。

`rag_index.retrieve` 内部是**同步** redis 客户端（共享 TTL 缓存）+ BM25 打分；
在 async 执行体里直接调用时，真实 Redis 下每次召回都是两次阻塞往返，
网络一抖就把事件循环（SSE 心跳、取消检查、其它任务）一起卡住。
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid

import pytest

from app.agents_image import graph as graph_module
from app.agents_image.graph import execute_task
from app.agents_image.ids import new_task_id
from app.agents_image.nodes import plan_slots
from app.agents_image.png import placeholder_png
from app.agents_image.rag import index as rag_index
from app.agents_image.sources.fake_catalog import _PRODUCTS, FakeCatalog
from app.agents_image.state import ImageTaskState, SlotPlan
from app.agents_image.store import assets as asset_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    from app.agents_image.config import reset_image_settings

    reset_image_settings()
    rag_index.reset_index()
    ensure_image_schema()
    yield
    reset_image_settings()
    rag_index.reset_index()


def _seed_task(tmp_path, monkeypatch) -> str:
    from app.agents_image.config import reset_image_settings

    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    reset_image_settings()
    ensure_image_schema()
    user = f"u-rag-{uuid.uuid4().hex[:8]}"
    with session_scope() as session:
        ref = asset_store.save_bytes(session, placeholder_png("rag-ref", 64, 64),
                                     kind="ref", product_id="P-1001", username=user)
        state = ImageTaskState(prompt="白底主图", aspect="1:1", count=1, refs=[])
        state.slots = [SlotPlan(**item) for item in plan_slots.build_slots(
            state, policy=asyncio.run(FakeCatalog().get_plan_policy()))]
        task = task_store.create_task(
            session, task_id=new_task_id(), username=user, department="女装",
            prompt="白底主图", platform="淘系", aspect="1:1", n=1,
            ref_asset_ids=[ref.asset_id], product_id="P-1001",
            attrs=dict(_PRODUCTS["P-1001"]["attrs"]),
            slots=[item.model_dump() for item in state.slots], image_count=1,
        )
        asset_store.bind_refs_to_task(session, [ref.asset_id], task.task_id)
        return task.task_id


def test_rag_recall_runs_off_the_event_loop(env, tmp_path, monkeypatch):
    real_retrieve = rag_index.retrieve
    seen = {"threads": [], "main": threading.get_ident()}

    def slow_retrieve(*args, **kwargs):
        seen["threads"].append(threading.get_ident())
        time.sleep(0.15)                     # 模拟真实 Redis 往返 + BM25 打分
        return real_retrieve(*args, **kwargs)

    monkeypatch.setattr(rag_index, "retrieve", slow_retrieve)
    task_id = _seed_task(tmp_path, monkeypatch)

    async def scenario() -> int:
        ticks = {"n": 0}

        async def ticker():
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        try:
            await execute_task(task_id)
        finally:
            task.cancel()
        return ticks["n"]

    ticks = asyncio.run(scenario())
    assert seen["threads"], "召回必须被调用"
    assert all(tid != seen["main"] for tid in seen["threads"]), \
        "召回不得在主事件循环线程里执行（旧实现会卡住 loop）"
    assert ticks > 3, f"召回阻塞了事件循环（ticker 只跑了 {ticks} 次）"
    assert graph_module is not None
