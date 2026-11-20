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

# P1-4（第十二轮）：一次合并取数的总等待上限（窗口 + 上游往返 + 重试）。
# 取数卡住时必须**报错**，不能让建任务请求永久挂起。
_DEFAULT_BUDGET_SECONDS = 20.0


def _budget_seconds() -> float:
    try:
        settings = get_image_settings()
        budget = float(getattr(settings, "context_batch_timeout_seconds",
                               _DEFAULT_BUDGET_SECONDS) or _DEFAULT_BUDGET_SECONDS)
    except Exception:  # noqa: BLE001 —— 配置读不到就按默认预算
        budget = _DEFAULT_BUDGET_SECONDS
    return max(1.0, budget)


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
        # P1-4（第十二轮）：等待必须有上限。上一版裸 `await future`：合并任务若在把结果
        # 分发给 waiter 之前就退出（被取消/配置读取失败/异常路径），future 永远不会被 set，
        # 调用方的 HTTP 请求就**永久挂起**（连接与 worker 槽位一起泄漏，还没有任何错误码）。
        budget = _budget_seconds()
        try:
            found.update(await asyncio.wait_for(future, timeout=budget))
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ImageError("IMG_UPSTREAM_MCP_UNAVAILABLE",
                             f"商品上下文取数超时（合并窗口 {budget:g}s），请稍后重试",
                             retryable=True) from exc
        return found

    # ------------------------------------------------------------ 合并
    async def _flush(self) -> None:
        """合并循环：**首个请求立即发车**，后续到达的请求并入下一批（P1-9）。

        上一版是"先 `sleep(window)` 再取数"：即使窗口内只有它自己一个请求，也要先静置
        满 3s 才发车——建任务与商品列表两个首屏接口因此固定 +3s（测试里窗口配 0，
        所以基线延迟把这条掩盖了）。现在的顺序是：先取走当前 waiter 发一批，若这批
        执行期间又来了新 waiter，再攒一个窗口合并下一批。
        """
        settings = get_image_settings()
        window = max(0.0, float(getattr(settings, "context_batch_window_seconds", 0.0) or 0.0))
        try:
            while True:
                pending, self._waiters = self._waiters, []
                if not pending:
                    return
                await self._run_batch(pending, settings)
                if not self._waiters:
                    return
                if window > 0:
                    await asyncio.sleep(window)   # 只为"下一批"攒窗口，首屏不再等待
        finally:
            self._flushing = False
            self._flush_task = None
            self._fail_pending("商品上下文合并取数中断（服务重启或任务取消），请重试")

    def _fail_pending(self, message: str) -> None:
        """把没人认领的 waiter 全部唤起为失败（P1-4）：绝不让请求挂在 future 上。

        典型场景：`_flush` 在 batch 之间被取消（关机/热重载），或 `_run_batch` 抛出
        未预期异常——上一版这种情况下 waiter 永远等不到结果。
        """
        leftover, self._waiters = self._waiters, []
        for _ids, future in leftover:
            if not future.done():
                future.set_exception(
                    ImageError("IMG_UPSTREAM_MCP_UNAVAILABLE", message, retryable=True))

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
                future.set_exception(_as_image_error(error))
            else:
                future.set_exception(ImageError("IMG_PRODUCT_NOT_FOUND",
                                                f"商品不存在：{missing[0]}"))


def _as_image_error(exc: BaseException) -> ImageError:
    """上游取数异常统一收敛成 503（P1-4）：原样抛 RuntimeError 会变成 500，
    而这类失败（中台不可用/超时）对调用方是**可重试**的 503。"""
    if isinstance(exc, ImageError):
        return exc
    return ImageError("IMG_UPSTREAM_MCP_UNAVAILABLE",
                      f"商品上下文取数失败：{type(exc).__name__}", retryable=True)


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
