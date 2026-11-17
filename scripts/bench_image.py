"""生图链路压测（离线）：并发 N 路同时跑「Prompt 组装 + FakeProvider 出图 + Critic」，
输出吞吐与 P50/P95，用来验证并发设计与成本口径（默认零成本）。

用法：
    PYTHONPATH=src python scripts/bench_image.py --tasks 50 --concurrency 8
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app.agents_image.config import ImageSettings  # noqa: E402
from app.agents_image.generator import handler as generator_handler  # noqa: E402
from app.agents_image.nodes import critic as critic_mod  # noqa: E402
from app.agents_image.nodes import plan_slots  # noqa: E402
from app.agents_image.registry import get_registry  # noqa: E402
from app.agents_image.sources.fake_catalog import FakeCatalog  # noqa: E402
from app.agents_image.state import ImageTaskState, RefImage, SlotPlan  # noqa: E402


async def _build_state(catalog: FakeCatalog, index: int) -> ImageTaskState:
    product_id = ["P-1001", "P-1002", "P-2001", "P-3001"][index % 4]
    product = await catalog.get_product(product_id)
    policy = await catalog.get_plan_policy()
    state = ImageTaskState(
        task_id=f"bench-{index:04d}", prompt="白底主图，正面全身像上装突出",
        aspect="1:1", count=3, platform="淘系",
        refs=[RefImage(asset_id=f"ref-{product_id}", product_id=product_id)],
        attrs=dict(product.attrs),
    )
    state.slots = [SlotPlan(**item) for item in plan_slots.build_slots(state, policy=policy)]
    return state


async def _one(catalog: FakeCatalog, registry, rules, state: ImageTaskState, semaphore,
               image_size: int = 128) -> float:
    async with semaphore:
        started = time.perf_counter()
        drafts = [draft for draft in plan_slots.compose(state, registry, rules=rules)
                  if draft.type == "generate"]
        result = await generator_handler.run({
            "provider": "fake",
            "refs": [{"asset_id": "r", "mime": "image/png", "sha256": "x",
                      "data": _tiny_png()}],
            "slots": [{"slot": draft.slot, "prompt": draft.prompt,
                       "negative_prompt": draft.negative_prompt, "n": 1} for draft in drafts],
            # 占位图是纯 Python 生成：压测用小尺寸（真实 provider 才用 1024），否则 CPU 打满看不出并发效果
            "width": image_size,
            "height": image_size,
            "preserve": drafts[0].preserve if drafts else [],
        })
        images = [critic_mod.CheckedImage(slot=slot.get("slot", 1), data=data)
                  for slot in result.get("slots") or [] for data in slot.get("images") or []]
        critic_mod.critique(state, drafts, images, rules=rules)
        return (time.perf_counter() - started) * 1000


def _tiny_png() -> bytes:
    from app.agents_image.png import encode_png

    return encode_png(32, 32, bytes((240, 240, 240) * 32 * 32))


async def _run(tasks: int, concurrency: int, image_size: int = 128) -> dict:
    catalog = FakeCatalog()
    registry = get_registry()
    rules = await catalog.get_platform_rules(platform="淘系")
    semaphore = asyncio.Semaphore(concurrency)
    states = [await _build_state(catalog, index) for index in range(tasks)]
    started = time.perf_counter()
    latencies = await asyncio.gather(*[
        _one(catalog, registry, rules, state, semaphore, image_size) for state in states])
    total = time.perf_counter() - started
    ordered = sorted(latencies)
    return {
        "tasks": tasks, "concurrency": concurrency, "image_size": image_size,
        "wall_seconds": round(total, 3),
        "throughput_per_second": round(tasks / total, 2) if total else 0.0,
        "latency_ms_p50": int(statistics.median(ordered)),
        "latency_ms_p95": int(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]),
        "cost_cny": round(tasks * ImageSettings().cost_per_image_cny, 4),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生图链路离线压测")
    parser.add_argument("--tasks", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--image-size", type=int, default=128,
                        help="占位图边长（纯 Python 生成，默认 128 便于压并发；真实 provider 用 1024）")
    args = parser.parse_args(argv)
    result = asyncio.run(_run(max(1, args.tasks), max(1, args.concurrency),
                              max(64, args.image_size)))
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
