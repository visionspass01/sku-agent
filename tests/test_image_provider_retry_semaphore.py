"""第九轮 P2-5/P2-6：重试不占信号量 + 回写 payload 必须确定（重放安全）。

P2-5：`async with _provider_semaphore()` 过去套在整段重试外，退避睡眠期间也占着全局并发
槽位（一次限流重试把 IMAGE_PROVIDER_CONCURRENCY 个名额一起占住）。现在只包住单次调用。

P2-6：回写 `save_result` 网络超时会重试，重发必须是**同一份 payload**（中台按
`(task_id, slot)` upsert 才幂等）；如果 payload 里混入时间戳/随机数，重试会覆盖成
"新内容"。这里锁住"同 state 生成同 payload"。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.config import reset_image_settings
from app.agents_image.generator import handler as generator_handler
from app.agents_image.graph import _backfill
from app.agents_image.ids import new_task_id
from app.agents_image.nodes import plan_slots
from app.agents_image.png import placeholder_png
from app.agents_image.providers.image_provider import ImageRequest, ImageResult
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.sources.fake_catalog import _PRODUCTS, FakeCatalog
from app.agents_image.state import ImageTaskState, SlotPlan
from app.agents_image.store import assets as asset_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store.schema import ensure_image_schema
from app.core.errors import ImageError
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    monkeypatch.delenv("IMAGE_PRODUCT_SOURCE", raising=False)
    reset_image_settings()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()
    image_metrics.reset()
    ensure_image_schema()
    yield
    reset_image_settings()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()


def _result() -> ImageResult:
    return ImageResult(images=[b"img"], mime="image/png", provider="fake",
                       model="m", cost_cny=0.0, latency_ms=1)


# ---------------------------------------------------------------- P2-5
def test_backoff_does_not_hold_provider_semaphore(env, monkeypatch):
    """provider_concurrency=1 时：A 在退避睡眠，B 必须能立刻拿到槽位。

    退避时长特意设成 1s（默认 0.2s）：这是**时间差**断言，落在共享 runner 上时
    0.2s 的窗口会被调度抖动吃掉（历史上就出现过 B 被误判成"被挡住"）。设成 1s 后
    两边差距拉到 5 倍以上，断言依然只在"信号量被退避占住"时才失败。
    """
    monkeypatch.setenv("IMAGE_PROVIDER_CONCURRENCY", "1")
    monkeypatch.setenv("IMAGE_PROVIDER_RETRIES", "1")
    monkeypatch.setenv("IMAGE_PROVIDER_RETRY_BACKOFF_SECONDS", "1.0")
    reset_image_settings()
    generator_handler.reset_provider_semaphore()

    class _Flaky:
        name = "flaky"

        def __init__(self) -> None:
            self.failed = False

        async def generate(self, req: ImageRequest) -> ImageResult:
            if req.prompt == "A" and not self.failed:
                self.failed = True
                raise ImageError("IMG_PROVIDER_RATE_LIMITED", "限流", retryable=True)
            return _result()

    provider = _Flaky()

    async def run(prompt: str) -> float:
        started = time.monotonic()
        await generator_handler._generate_with_retry(
            provider, ImageRequest(prompt=prompt, n=1))
        return time.monotonic() - started

    async def scenario():
        a = asyncio.create_task(run("A"))
        await asyncio.sleep(0.02)                 # 让 A 先进入退避睡眠
        b_elapsed = await run("B")
        a_elapsed = await a
        return a_elapsed, b_elapsed

    a_elapsed, b_elapsed = asyncio.run(scenario())
    # B 没有退避，正常应是"接近 0"；门槛留到 0.5s 是给共享 runner 的调度抖动留余量，
    # 只要 B 真的被 A 的 1s 退避挡住（历史 bug），这里必然 > 1s 而失败。
    assert b_elapsed < 0.5, f"B 被 A 的退避睡眠挡住了（{b_elapsed:.3f}s）"
    assert a_elapsed >= 0.9, a_elapsed             # A 确实经历了一次 1s 退避
    generator_handler.reset_provider_semaphore()


# ---------------------------------------------------------------- P2-6
def _seed_state(tmp_path, monkeypatch) -> ImageTaskState:
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    reset_image_settings()
    ensure_image_schema()
    user = f"u-det-{uuid.uuid4().hex[:8]}"
    state = ImageTaskState(prompt="白底主图", aspect="1:1", count=1, refs=[])
    slots = [SlotPlan(**item) for item in plan_slots.build_slots(
        state, policy=asyncio.run(FakeCatalog().get_plan_policy()))]
    with session_scope() as session:
        ref = asset_store.save_bytes(session, placeholder_png("det-ref", 64, 64),
                                     kind="ref", product_id="P-1001", username=user)
        task = task_store.create_task(
            session, task_id=new_task_id(), username=user, department="女装",
            prompt="白底主图", platform="淘系", aspect="1:1", n=1,
            ref_asset_ids=[ref.asset_id], product_id="P-1001",
            attrs=dict(_PRODUCTS["P-1001"]["attrs"]),
            slots=[item.model_dump() for item in slots], image_count=1,
        )
        asset_store.bind_refs_to_task(session, [ref.asset_id], task.task_id)
        task_id = task.task_id
    delivered = ImageTaskState(task_id=task_id, username=user, department="女装",
                               prompt="白底主图", platform="淘系", aspect="1:1", count=1,
                               attrs=dict(_PRODUCTS["P-1001"]["attrs"]))
    delivered.slots = slots
    delivered.prompt_final = "白底主图，商品占画面 70%"
    return delivered


def _capture_payload(monkeypatch) -> dict:
    captured: dict = {}
    real_save = FakeCatalog.save_result

    async def spy(self, payload):
        captured["payload"] = json.loads(json.dumps(payload, default=str, sort_keys=True))
        return await real_save(self, payload)

    monkeypatch.setattr(FakeCatalog, "save_result", spy)
    return captured


def test_backfill_payload_is_deterministic_for_replay(env, tmp_path, monkeypatch):
    """同一 state + 同一 outputs 两次回写必须产出**同一份 payload**（重试重放安全）。"""
    state = _seed_state(tmp_path, monkeypatch)
    outputs = [{"slot": 1, "mime": "image/png", "sha256": "abc", "width": 1024,
                "height": 1024, "bytes": 100, "storage": "local", "key": "outputs/abc.png"}]
    captured = _capture_payload(monkeypatch)

    asyncio.run(_backfill(state, outputs_by_slot=outputs))
    first = captured["payload"]
    asyncio.run(_backfill(state, outputs_by_slot=outputs))
    second = captured["payload"]
    assert first == second, "回写 payload 不得含时间戳/随机值（否则重试会覆盖中台结果）"
    assert first["results"][0]["slot"] == 1
