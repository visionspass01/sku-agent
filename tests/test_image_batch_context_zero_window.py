"""第八轮 P2-7：`context_batch_window=0` 时后续请求不能挂起。

旧实现 `_flush` 在 window≤0 时处理完**当前**一批就 return；批次执行期间新到的 waiter
留在 `_waiters` 里没人唤醒，`await future` 永久挂起（请求永远不回）。
"""

from __future__ import annotations

import asyncio

from app.agents_image.config import reset_image_settings
from app.agents_image.sources.batch_context import ProductContextBatcher
from app.agents_image.sources.product_source import ProductData


class _Catalog:
    name = "stub"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def get_products_context(self, ids: list[str]) -> dict[str, ProductData]:
        self.calls.append(list(ids))
        await asyncio.sleep(0.01)          # 模拟网络：让第二个请求在批次执行期间到达
        return {pid: ProductData(product_id=pid, title="t") for pid in ids}


def test_zero_window_serves_late_waiters(tmp_path, monkeypatch, _db):
    monkeypatch.setenv("IMAGE_CONTEXT_BATCH_WINDOW_SECONDS", "0")
    reset_image_settings()
    catalog = _Catalog()
    batcher = ProductContextBatcher(catalog)

    async def scenario():
        first = asyncio.create_task(batcher.get_many(["P-1001"]))
        await asyncio.sleep(0)                       # first 进入在途批次
        second = asyncio.create_task(batcher.get_many(["P-2001"]))
        return await asyncio.gather(first, second)

    results = asyncio.run(asyncio.wait_for(scenario(), timeout=2.0))
    assert results[0]["P-1001"].product_id == "P-1001"
    assert results[1]["P-2001"].product_id == "P-2001", "window=0 时第二个请求不能挂起"
    assert len(catalog.calls) == 2, catalog.calls


def test_batcher_stats_counts_batches(tmp_path, monkeypatch, _db):
    monkeypatch.setenv("IMAGE_CONTEXT_BATCH_WINDOW_SECONDS", "0")
    reset_image_settings()
    catalog = _Catalog()
    batcher = ProductContextBatcher(catalog)
    asyncio.run(batcher.get_many(["P-1001"]))
    stats = batcher.stats()
    assert stats["batches"] == 1
    assert stats["requested"] == 1
    reset_image_settings()
