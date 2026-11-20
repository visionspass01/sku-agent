"""第五轮 W4：记忆语义重写（P0-4 / P1-3 / P1-4）。

口径（按产品决策）：
- **失败不归档**（只留任务台账）；
- **失败→修复成功才归档修法**（存的是实际生效的修复指令，不是报错文本）；
- **默认生成不检索**，只有 Critic 不通过、准备再来一轮时才检索
  （同签名+同部门 → KB fixes → 内置规则）；
- 修复经验按 TTL 清理，人工采纳/驳回样本永不自动删除。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.nodes import plan_slots
from app.agents_image.png import placeholder_png
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.state import ImageTaskState, SlotPlan
from app.agents_image.store import assets as asset_store
from app.agents_image.store import memory as memory_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("IMAGE_PROVIDER_RETRY_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("IMAGE_RAG_ENABLED", "false")
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


def _seed_task(monkeypatch, tmp_path, *, department: str = "女装",
               product_type: str = "", marker: str = "") -> str:
    """建一个待执行任务；`product_type` 决定失败签名，便于用例之间互不干扰。"""
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    reset_image_settings()
    ensure_image_schema()
    state = ImageTaskState(prompt=f"白底主图 {marker}".strip(), aspect="1:1", count=1, refs=[])
    slots = [SlotPlan(**item) if isinstance(item, dict) else item
             for item in plan_slots.build_slots(
                 state, policy=asyncio.run(ps.get_catalog().get_plan_policy()))]
    with session_scope() as session:
        ref = asset_store.save_bytes(session, placeholder_png("r", 64, 64), kind="ref",
                                     product_id="P-3001")
        task_id = f"2026112{uuid.uuid4().hex[:9]}"
        task_store.create_task(
            session, task_id=task_id, username="admin", department=department,
            prompt=state.prompt, platform="淘系", aspect="1:1", n=1,
            ref_asset_ids=[ref.asset_id], product_id="P-3001",
            attrs={"product_type": product_type or "服装商品", "color_hex": "#111111",
                   "category": "女装"},
            slots=[item.model_dump() for item in slots], image_count=1,
            slot_count=len(slots),
        )
        asset_store.bind_refs_to_task(session, [ref.asset_id], task_id)
    return task_id


# ---------------------------------------------------------------- store 层
def test_remember_repair_upserts_and_counts_hits(_db):
    ensure_image_schema()
    now = dt.datetime.now()
    with session_scope() as session:
        memory_store.remember_repair(session, signature="aspect|upsert-品类",
                                     patch="修法 A", department="女装", now=now)
        memory_store.remember_repair(session, signature="aspect|upsert-品类",
                                     patch="修法 B", department="女装", now=now)
        rows = memory_store.list_memories(session, kind=memory_store.REPAIR_KIND, limit=50)
    mine = [row for row in rows if row["failure_signature"] == "aspect|upsert-品类"]
    assert len(mine) == 1, "同签名+同部门必须 upsert 成一行"
    assert mine[0]["hits"] == 2
    assert mine[0]["prompt_patch"] == "修法 B"


def test_repair_patches_prefers_same_department(_db):
    ensure_image_schema()
    with session_scope() as session:
        memory_store.remember_repair(session, signature="identity|部门-品类",
                                     patch="通用修法", department=None)
        memory_store.remember_repair(session, signature="identity|部门-品类",
                                     patch="男装修法", department="男装")
        male = memory_store.repair_patches(session, "identity|部门-品类", department="男装")
        female = memory_store.repair_patches(session, "identity|部门-品类", department="女装")
    assert male == {"identity": "男装修法"}, "同部门必须优先"
    assert female == {"identity": "通用修法"}, "没有本部门经验时退回通用"


def test_repair_patches_without_experience_is_empty(_db):
    ensure_image_schema()
    with session_scope() as session:
        assert memory_store.repair_patches(session, "mime|不存在的品类-uuid",
                                           department="男装") == {}


def test_prune_keeps_human_samples(_db):
    ensure_image_schema()
    old = dt.datetime.now() - dt.timedelta(days=400)
    with session_scope() as session:
        stale = memory_store.remember_repair(session, signature="aspect|旧经验",
                                             patch="旧修法", department="女装", now=old)
        stale_id = stale.memory_id
        human = memory_store.remember(session, signature="adopted|P-1001|slot9",
                                      fix_summary="人工采纳")
        human.sample_kind = "positive"
        human.updated_at = old
        human_id = human.memory_id
        session.flush()
        removed = memory_store.prune(session, keep_days=90)
        remaining = {row["memory_id"] for row in memory_store.list_memories(session, limit=200)}
    assert removed >= 1
    assert stale_id not in remaining, "过期修复经验应被清理"
    assert human_id in remaining, "人工样本永不被自动清理"


# ---------------------------------------------------------------- 图谱：检索时机
def test_first_pass_success_does_not_read_memory(env, monkeypatch, tmp_path):
    """首轮就通过的正常任务：不该发生任何"记忆检索"（防污染的关键一侧）。"""
    from app.agents_image import graph

    calls = {"repair": 0}
    real = memory_store.repair_patches

    def spy(*args, **kwargs):
        calls["repair"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(memory_store, "repair_patches", spy)
    task_id = _seed_task(monkeypatch, tmp_path, product_type="首轮通过品类")
    asyncio.run(graph.execute_task(task_id))
    with session_scope() as session:
        status = task_store.get_task(session, task_id).status
    assert status == "done"
    assert calls["repair"] == 0, "首轮成功不得检索修复经验"


def test_failure_does_not_archive_memory(env, monkeypatch, tmp_path):
    from app.agents_image import graph
    from app.agents_image.providers.fake import FakeProvider
    from app.agents_image.providers.image_provider import ImageResult

    task_id = _seed_task(monkeypatch, tmp_path, product_type="失败不归档品类")

    async def always_bad(self, req):
        return ImageResult(images=[placeholder_png("bad", 640, 360)], mime="image/png",
                           provider="fake", model="bad", cost_cny=0.0, latency_ms=1)

    monkeypatch.setattr(FakeProvider, "generate", always_bad)
    with pytest.raises(Exception):
        asyncio.run(graph.execute_task(task_id))
    with session_scope() as session:
        rows = memory_store.list_memories(session, limit=200)
    assert not any("失败不归档品类" in row["failure_signature"]
                   and row["sample_kind"] in {memory_store.REPAIR_KIND, "auto"}
                   for row in rows), rows


def test_failure_to_success_archives_the_repair(env, monkeypatch, tmp_path):
    """第一轮 Critic 失败、修一轮后通过 → 归档「修法」，且存的是修法不是报错文本。"""
    from app.agents_image import graph
    from app.agents_image.providers.fake import FakeProvider
    from app.agents_image.providers.image_provider import ImageResult

    product_type = "归档品类-q1"
    marker = "mem-repair-marker"
    task_id = _seed_task(monkeypatch, tmp_path, product_type=product_type, marker=marker)
    original = FakeProvider.generate
    state = {"calls": 0}

    async def flaky(self, req):
        if marker not in (req.prompt or ""):
            return await original(self, req)
        state["calls"] += 1
        if state["calls"] == 1:
            return ImageResult(images=[placeholder_png("bad", 640, 360)], mime="image/png",
                               provider="fake", model="bad-aspect", cost_cny=0.0, latency_ms=1)
        return await original(self, req)

    monkeypatch.setattr(FakeProvider, "generate", flaky)
    result = asyncio.run(graph.execute_task(task_id))
    assert result.attempt == 2

    with session_scope() as session:
        rows = memory_store.list_memories(session, kind=memory_store.REPAIR_KIND, limit=200)
    repairs = [row for row in rows if row["failure_signature"] == f"aspect|{product_type}"]
    assert repairs, rows
    row = repairs[0]
    assert row["department"] == "女装"
    # 存的是"修法"（按图位组织的修复指令：可能来自历史修法 / KB fixes / 内置规则），
    # 不是 Critic 的报错原文
    assert "图位" in row["prompt_patch"], row["prompt_patch"]
    assert not row["prompt_patch"].startswith("aspect:")
    assert row["hits"] >= 1


def test_repair_is_reused_on_next_same_signature_failure(env, monkeypatch, tmp_path):
    """同签名再次失败时：检索到已归档的修法，并把修法注入下一轮 Prompt。"""
    from app.agents_image import graph
    from app.agents_image.providers.fake import FakeProvider
    from app.agents_image.providers.image_provider import ImageResult

    product_type = "复用品类-q2"
    signature = f"aspect|{product_type}"
    with session_scope() as session:
        memory_store.remember_repair(session, signature=signature,
                                     patch="历史修法：按请求比例重新裁切并居中",
                                     department="女装")

    marker = "mem-reuse-marker"
    task_id = _seed_task(monkeypatch, tmp_path, product_type=product_type, marker=marker)
    original = FakeProvider.generate
    state = {"calls": 0, "prompts": []}

    async def flaky(self, req):
        if marker not in (req.prompt or ""):
            return await original(self, req)
        state["calls"] += 1
        state["prompts"].append(req.prompt or "")
        if state["calls"] == 1:
            return ImageResult(images=[placeholder_png("bad", 640, 360)], mime="image/png",
                               provider="fake", model="bad", cost_cny=0.0, latency_ms=1)
        return await original(self, req)

    monkeypatch.setattr(FakeProvider, "generate", flaky)
    asyncio.run(graph.execute_task(task_id))
    assert state["calls"] == 2
    assert any("历史修法：按请求比例重新裁切并居中" in prompt for prompt in state["prompts"][1:]), \
        state["prompts"]


def test_kb_fix_is_used_when_memory_misses(env, monkeypatch):
    """没有历史修法时退回 KB 的 `fixes/` 语料（P1-3）。

    本文件的 env 为了测记忆默认关了 RAG（P1-3 第七轮起开关对记忆检索之外的 KB 检索
    同样生效），这个用例要验证 KB 兜底，所以显式把开关打开。
    """
    from app.agents_image import graph

    monkeypatch.setenv("IMAGE_RAG_ENABLED", "true")
    reset_image_settings()
    state = ImageTaskState(task_id="t-kb", department="女装",
                           attrs={"product_type": "KB品类-q3"})
    result = graph._repair_experience(state, "aspect|KB品类-q3")
    assert result["source"] == "kb", result
    assert result["patches"], result
    assert any("IMG_REPAIR_KB" in warning for warning in state.warnings), state.warnings
