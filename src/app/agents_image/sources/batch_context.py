"""商品上下文批量取数（W2）：同一 3 秒窗口内的多次取数**合并成一次 MCP 调用**。

为什么需要：
- 一次批量建任务要点「N 个商品 × 颜色」，且要先做部门校验再下载参考图；
- 单个商品逐个回中台 = N 次 MCP 往返（前端还会并发发起多个请求）；
- 所以按 `IMAGE_CONTEXT_BATCH_WINDOW_SECONDS`（默认 3s）开一个窗口，把窗口内所有请求的
  商品 id **去重合并**，一次调用 `get_products_context`（`image_get_products_context`），
  结果进 TTL 缓存（`IMAGE_CONTEXT_CACHE_TTL_SECONDS`，默认 60s）。

口径不变：数据仍然**只来自中台**，这里只做"请求合并 + 短时缓存"，不兜底、不造数据。
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.agents_image.config import get_image_settings
from app.agents_image.sources.product_source import ProductData
from app.core.errors import ImageError

logger = logging.getLogger("sku-agent.image.catalog.batch")


async def fetch_products_context(catalog, product_ids: list[str]) -> dict[str, ProductData]:
    """取一批商品上下文：catalog 支持批量就用批量，否则退回逐个取（语义一致）。"""
    ids = _dedupe(product_ids)
    if not ids:
        return {}
    batch = getattr(catalog, "get_products_context", None)
    if callable(batch):
        return await batch(ids)
    out: dict[str, ProductData] = {}
    for product_id in ids:
        out[product_id] = await catalog.get_product(product_id)
    return out


def _dedupe(values) -> list[str]:
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


class ProductContextBatcher:
    """合并窗口 + TTL 缓存（进程内，单事件循环安全：状态只在无 await 的临界区改动）。"""

    def __init__(self, catalog) -> None:
        self.catalog = catalog
        self._cache: dict[str, tuple[float, ProductData]] = {}
        self._waiters: list[tuple[list[str], asyncio.Future]] = []
        self._flushing = False
        self._flush_task: asyncio.Task | None = None
        self.batches = 0        # 真正发起的合并请求数
        self.requested = 0      # 被合并的商品 id 次数
        self.cache_hits = 0

    # ------------------------------------------------------------ 观测
    def stats(self) -> dict:
        return {"batches": self.batches, "requested": self.requested,
                "cache_hits": self.cache_hits, "cached": len(self._cache)}

    def reset(self) -> None:
        self._cache.clear()
        self._waiters.clear()
        self._flushing = False
        self._flush_task = None
        self.batches = 0
        self.requested = 0
        self.cache_hits = 0

    # ------------------------------------------------------------ 取数
    async def get_many(self, product_ids) -> dict[str, ProductData]:
        ids = _dedupe(product_ids)
        if not ids:
            return {}
        moment = time.monotonic()
        found: dict[str, ProductData] = {}
        missing: list[str] = []
        for product_id in ids:
            cached = self._cache.get(product_id)
            if cached is not None and cached[0] > moment:
                found[product_id] = cached[1]
                self.cache_hits += 1
            else:
                missing.append(product_id)
        if not missing:
            return found

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.append((missing, future))
        self.requested += len(missing)
        if not self._flushing:
            self._flushing = True
            self._flush_task = asyncio.create_task(self._flush())
        found.update(await future)
        return found

    # ------------------------------------------------------------ 合并
    async def _flush(self) -> None:
        settings = get_image_settings()
        window = max(0.0, float(getattr(settings, "context_batch_window_seconds", 0.0) or 0.0))
        try:
            while True:
                if window > 0:
                    await asyncio.sleep(window)      # 攒够一个窗口，再发一次合并请求
                pending, self._waiters = self._waiters, []
                if not pending:
                    return
                await self._run_batch(pending, settings)
                if window > 0:
                    continue
                # P2-7（第八轮）：window=0 时旧实现处理完这一批就 return，
                # 批次执行期间新到的 waiter 永远没人唤醒 → 请求永久挂起。
                # 现在只要还有 waiter 就继续处理；空了才退出（退出时 finally 复位 flushing）。
                if not self._waiters:
                    return
        finally:
            self._flushing = False
            self._flush_task = None

    async def _run_batch(self, pending: list[tuple[list[str], asyncio.Future]],
                         settings) -> None:
        chunk_size = max(1, int(getattr(settings, "context_batch_max_ids", 50) or 50))
        union: list[str] = []
        seen: set[str] = set()
        for ids, _ in pending:
            for product_id in ids:
                if product_id not in seen:
                    seen.add(product_id)
                    union.append(product_id)

        result: dict[str, ProductData] = {}
        error: BaseException | None = None
        for offset in range(0, len(union), chunk_size):
            chunk = union[offset:offset + chunk_size]
            self.batches += 1
            try:
                result.update(await fetch_products_context(self.catalog, chunk))
            except Exception as exc:  # noqa: BLE001 —— 一个分片失败不影响其它分片
                error = exc
                logger.warning("商品上下文合并取数失败（%d 个 id）：%s", len(chunk), exc)

        ttl = float(getattr(settings, "context_cache_ttl_seconds", 60.0) or 0.0)
        expire = time.monotonic() + ttl
        if ttl > 0:
            for product_id, data in result.items():
                self._cache[product_id] = (expire, data)

        for ids, future in pending:
            if future.done():
                continue
            got = {product_id: result[product_id] for product_id in ids if product_id in result}
            missing = [product_id for product_id in ids if product_id not in result]
            if not missing:
                future.set_result(got)
            elif error is not None:
                future.set_exception(error)
            else:
                future.set_exception(ImageError("IMG_PRODUCT_NOT_FOUND",
                                                f"商品不存在：{missing[0]}"))


_BATCHERS: dict[str, ProductContextBatcher] = {}


def get_product_batcher(catalog=None) -> ProductContextBatcher:
    """按 catalog 复用同一个 batcher（窗口/缓存跨请求共享，合并才有意义）。"""
    from app.agents_image.sources.product_source import get_catalog

    target = catalog or get_catalog()
    key = getattr(target, "name", "") or "default"
    batcher = _BATCHERS.get(key)
    if batcher is None or batcher.catalog is not target:
        batcher = ProductContextBatcher(target)
        _BATCHERS[key] = batcher
    return batcher


def reset_product_batcher() -> None:
    """测试/热重载用：清空窗口与缓存（避免跨用例复用别的库里的商品）。"""
    _BATCHERS.clear()


__all__ = ["ProductContextBatcher", "fetch_products_context", "get_product_batcher",
           "reset_product_batcher"]
