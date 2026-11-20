"""D11：Critic 规则评审、Reflexion 补丁、image_memory 归档，以及编排里的重试闭环。"""

from __future__ import annotations

import asyncio

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.config import ImageSettings, reset_image_settings
from app.agents_image.graph import execute_task
from app.agents_image.ids import new_task_id
from app.agents_image.nodes import critic as critic_mod
from app.agents_image.nodes import plan_slots, reflexion
from app.agents_image.png import placeholder_png
from app.agents_image.providers.image_provider import ImageResult
from app.agents_image.registry import reset_registry
from app.agents_image.sources.fake_catalog import _PRODUCTS, FakeCatalog
from app.agents_image.state import ImageTaskState, PromptDraft, RefImage, SlotPlan
from app.agents_image.store import assets as asset_store
from app.agents_image.store import memory as memory_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    # 关掉看门狗：本文件直接建 queued 任务再手动 execute_task，
    # 看门狗若同时重投同一任务会让 provider 调用次数翻倍（跨用例/跨线程串味）
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    reset_registry()
    reset_image_settings()
    image_metrics.reset()
    yield
    reset_registry()
    reset_image_settings()


def _state(**overrides) -> ImageTaskState:
    base = {"prompt": "白底主图", "aspect": "1:1", "count": 1,
            "refs": [RefImage(asset_id="a" * 8)],
            "attrs": dict(_PRODUCTS["P-1001"]["attrs"])}
    base.update(overrides)
    return ImageTaskState(**base)


_LOCKED = list(_PRODUCTS["P-1001"]["attrs"]["must_not_change"])


def _draft(slot: int = 1, **overrides) -> PromptDraft:
    base = {"slot": slot, "role": "主图", "prompt": "白底主图，商品占画面 70%",
            "negative_prompt": "文字水印", "preserve": list(_LOCKED), "type": "generate"}
    base.update(overrides)
    return PromptDraft(**base)


def _image(width: int = 640, height: int = 640, slot: int = 1) -> critic_mod.CheckedImage:
    return critic_mod.CheckedImage(slot=slot, data=placeholder_png(f"{slot}:{width}", width, height),
                                   mime="image/png", width=width, height=height)


# ---------------------------------------------------------------- Critic
def test_critique_passes_for_consistent_output():
    result = critic_mod.critique(_state(), [_draft()], [_image()],
                                 settings=ImageSettings())
    assert result.passed is True and result.score == 100
    assert result.issues == []


def test_critique_blocks_identity_loss():
    draft = _draft(preserve=["颜色"])
    result = critic_mod.critique(_state(), [draft], [_image()], settings=ImageSettings())
    assert result.passed is False
    assert "identity" in result.consistency["blocking"]
    assert any("版型" in issue for issue in result.issues)


def test_critique_blocks_wrong_aspect_image():
    result = critic_mod.critique(_state(aspect="3:4"), [_draft()], [_image(640, 360)],
                                 settings=ImageSettings())
    assert result.passed is False
    assert "aspect" in result.consistency["blocking"]


def test_critique_blocks_banned_words():
    draft = _draft(prompt="白底主图 官方认证")
    result = critic_mod.critique(_state(), [draft], [_image()], settings=ImageSettings())
    assert result.passed is False
    assert "banned_words" in result.consistency["blocking"]


def test_critique_blocks_text_when_platform_forbids():
    from app.agents_image.sources.product_source import PlatformRules

    rules = PlatformRules(platform="淘系", text_allowed=False)
    draft = _draft(text_policy="headline")
    result = critic_mod.critique(_state(), [draft], [_image()], settings=ImageSettings(),
                                 rules=rules)
    assert result.passed is False
    assert "text_policy" in result.consistency["blocking"]


def test_critique_blocks_empty_output_and_bad_mime():
    empty = critic_mod.critique(_state(), [_draft()], [], settings=ImageSettings())
    assert empty.passed is False and "empty" in empty.consistency["blocking"]

    weird = critic_mod.CheckedImage(slot=1, data=b"xx", mime="text/plain")
    bad = critic_mod.critique(_state(), [_draft()], [weird], settings=ImageSettings())
    assert bad.passed is False and "mime" in bad.consistency["blocking"]


def test_critique_score_floor():
    drafts = [_draft(preserve=[]), _draft(2, prompt="")]
    result = critic_mod.critique(_state(), drafts, [], settings=ImageSettings())
    assert result.score == 0 and result.passed is False


def test_failure_signature_groups_blocking_kinds():
    result = critic_mod.critique(_state(), [_draft(preserve=[])], [], settings=ImageSettings())
    signature = critic_mod.failure_signature(_state(), result)
    assert "identity" in signature and "针织开衫" in signature


# ---------------------------------------------------------------- Reflexion
def test_reflect_targets_only_problem_slots():
    critic = critic_mod.critique(_state(), [_draft(1), _draft(2, preserve=[])],
                                 [_image(slot=1), _image(slot=2)], settings=ImageSettings())
    reflection = reflexion.reflect(_state(), critic, [_draft(1), _draft(2, preserve=[])])
    assert reflection.slots() == [2]
    assert "严格保持参考图中的颜色" in reflection.patches[2][0]
    assert state_reflections_contains(reflection.summary)


def state_reflections_contains(lines: list[str]) -> bool:
    return bool(lines) and all("图位" in line for line in lines)


def test_apply_patches_appends_fix_and_negative_terms():
    reflection = reflexion.Reflection(patches={1: ["严格按请求画幅比例生成，主体居中"]},
                                      summary=["图位 1：比例"])
    patched = reflexion.apply_patches([_draft()], reflection)
    assert "[修复要求]" in patched[0].prompt
    assert "严格按请求画幅比例生成" in patched[0].prompt
    assert "严格按请求画幅比例生成" in patched[0].negative_prompt
    assert patched[0] is not _draft()          # 不改原对象


def test_reflect_prefers_memory_patch():
    critic = critic_mod.critique(_state(), [_draft(preserve=[])], [], settings=ImageSettings())
    reflection = reflexion.reflect(_state(), critic, [_draft()],
                                   memory={"identity": "历史修复：改回影棚正式风"})
    assert "历史修复：改回影棚正式风" in reflection.patches[1][0]


# ---------------------------------------------------------------- memory 表
def test_memory_records_and_increments(_db):
    ensure_image_schema()
    with session_scope() as session:
        first_id = memory_store.remember(session, signature="identity|开衫",
                                         fix_summary="补身份锁定",
                                         prompt_patch="补身份锁定").memory_id
    with session_scope() as session:
        second_id = memory_store.remember(session, signature="identity|开衫",
                                          fix_summary="再补").memory_id
    assert first_id == second_id
    with session_scope() as session:
        row = memory_store.lookup(session, "identity|开衫")
        assert row.hits >= 2
        assert memory_store.patches_for(session, "identity|开衫") == {"identity": "补身份锁定"}
        listed = memory_store.list_memories(session)
    assert any(item["failure_signature"] == "identity|开衫" for item in listed)


def test_memory_ignores_blank_signature(_db):
    ensure_image_schema()
    with session_scope() as session:
        assert memory_store.remember(session, signature="  ", fix_summary="x") is None


# ---------------------------------------------------------------- 编排闭环
def _seed_task(tmp_path, monkeypatch, *, aspect="1:1", prompt="白底主图") -> str:
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    reset_image_settings()
    ensure_image_schema()
    with session_scope() as session:
        ref = asset_store.save_bytes(session, placeholder_png("r", 64, 64), kind="ref",
                                     product_id="P-1001")
        state = _state(aspect=aspect, prompt=prompt)
        state.slots = [SlotPlan(**item) for item in plan_slots.build_slots(
            state, policy=asyncio.run(FakeCatalog().get_plan_policy()))]
        task = task_store.create_task(
            session, task_id=new_task_id(), username="worker1", department="女装",
            prompt=state.prompt, platform="淘系", aspect=aspect, n=1,
            ref_asset_ids=[ref.asset_id], product_id="P-1001",
            attrs=dict(_PRODUCTS["P-1001"]["attrs"]),
            slots=[item.model_dump() for item in state.slots],
        )
        asset_store.bind_refs_to_task(session, [ref.asset_id], task.task_id)
        return task.task_id


def test_graph_retries_after_critic_failure(tmp_path, monkeypatch, _db):
    """第一轮出图比例错误 → Critic 判失败 → Reflexion 改 Prompt → 第二轮通过。"""
    marker = "critic-retry-marker-9f2a"
    task_id = _seed_task(tmp_path, monkeypatch, prompt=f"白底主图 {marker}")
    from app.agents_image.providers.fake import FakeProvider

    original = FakeProvider.generate          # 先取未绑定的原函数，避免被自己的补丁递归
    state = {"calls": 0}

    async def generate(self, req):
        # 只统计**本用例任务**的调用：别的用例遗留的后台任务也会走 provider（prompt 里没有这个标记）
        if marker not in (req.prompt or ""):
            return await original(self, req)
        state["calls"] += 1
        if state["calls"] == 1:
            bad = placeholder_png("bad", 640, 360)         # 16:9，与 1:1 不符
            return ImageResult(images=[bad], mime="image/png", provider="fake",
                               model="bad-aspect", cost_cny=0.0, latency_ms=1)
        return await original(self, req)

    monkeypatch.setattr(FakeProvider, "generate", generate)

    result = asyncio.run(execute_task(task_id))
    assert result.attempt == 2
    assert state["calls"] == 2
    assert result.critic.passed is True
    stages = [item["stage"] for item in result.timeline]
    assert "reflexion" in stages
    assert stages.count("generate") == 2
    with session_scope() as session:
        stored = task_store.get_task(session, task_id)
        assert stored.status == "done"
        assert stored.delivered_images == 1            # 只算最终通过那轮
        assert stored.attempted_images == 2            # 两轮都记尝试
        assert stored.attempts == 2


def test_graph_fails_after_max_rounds_without_polluting_memory(tmp_path, monkeypatch, _db):
    """第五轮 P0-4：Critic 多轮仍失败 → 任务失败，但**失败不进记忆**（避免污染默认生成）。"""
    task_id = _seed_task(tmp_path, monkeypatch, aspect="3:4")

    async def always_bad(self, req):
        return ImageResult(images=[placeholder_png("bad", 640, 360)], mime="image/png",
                           provider="fake", model="bad", cost_cny=0.0, latency_ms=1)

    monkeypatch.setattr("app.agents_image.providers.fake.FakeProvider.generate", always_bad)

    # 内存写入打点：失败路径不该调用任何"归档"入口（不看残留行，避免被别的用例干扰）
    writes = {"remember": 0, "remember_repair": 0}
    real_remember = memory_store.remember
    real_remember_repair = memory_store.remember_repair

    def spy_remember(*args, **kwargs):
        writes["remember"] += 1
        return real_remember(*args, **kwargs)

    def spy_remember_repair(*args, **kwargs):
        writes["remember_repair"] += 1
        return real_remember_repair(*args, **kwargs)

    monkeypatch.setattr(memory_store, "remember", spy_remember)
    monkeypatch.setattr(memory_store, "remember_repair", spy_remember_repair)

    from app.agents_image.dispatch.local_generator import LocalGenerator

    generator = LocalGenerator()

    async def run_until_settled():
        # 同一个事件循环里等它落库：asyncio.run(submit(...)) 返回会关闭循环、取消后台任务
        await generator.submit(task_id)
        for _ in range(400):
            with session_scope() as session:
                task = task_store.get_task(session, task_id)
                if task is not None and task.status in {"failed", "done"}:
                    return task.status
            await asyncio.sleep(0.05)
        return None

    assert asyncio.run(run_until_settled()) == "failed"
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        assert task.error_code == "IMG_CRITIC_FAILED"
        assert task.attempts >= 2
    assert writes == {"remember": 0, "remember_repair": 0}, writes
