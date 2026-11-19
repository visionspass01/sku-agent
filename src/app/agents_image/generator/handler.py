"""生图执行器（generator）：**本地函数与 SCF 共用同一份代码**。

`run(payload)` 只做三件事：按图位组装 provider 请求 → 调 provider 出图 → 返回每图结果。
不碰数据库、不碰 HTTP，因此可以单独打成 SCF 函数包。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.agents_image.providers.image_provider import ImageInput, ImageRequest, get_provider
from app.core.errors import ImageError

logger = logging.getLogger("sku-agent.image.generator")

# 第三方生图 API 的**全局并发上限**（默认 10）：保护对方限流与自身成本，
# 与任务并发（IMAGE_CONCURRENCY）分开——任务并发高不代表要同时压 10+ 个外部请求。
_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: Any = None


def _provider_semaphore() -> asyncio.Semaphore:
    global _semaphore, _semaphore_loop
    from app.agents_image.config import get_image_settings

    try:
        loop: Any = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover
        loop = None
    if _semaphore is None or _semaphore_loop is not loop:
        limit = max(1, int(getattr(get_image_settings(), "provider_concurrency", 10) or 10))
        _semaphore = asyncio.Semaphore(limit)
        _semaphore_loop = loop
    return _semaphore


def provider_in_flight() -> int:
    """当前在途的第三方调用数（health / 面板用）。"""
    from app.agents_image.config import get_image_settings

    sem = _semaphore
    if sem is None:
        return 0
    limit = getattr(sem, "_value", 0)
    total = int(getattr(get_image_settings(), "provider_concurrency", 10) or 10)
    return max(0, total - int(limit))


def reset_provider_semaphore() -> None:
    """测试用：清掉信号量（换事件循环时必须重建）。"""
    global _semaphore, _semaphore_loop
    _semaphore = None
    _semaphore_loop = None


_RETRYABLE_CODES = {"IMG_PROVIDER_RATE_LIMITED", "IMG_PROVIDER_UNAVAILABLE",
                    "IMG_TIMEOUT", "IMG_UPSTREAM_MCP_UNAVAILABLE"}


def _retryable(exc: BaseException) -> bool:
    """只有限流/超时/上游不可用值得重试；内容违规、参数错重试只会更糟。"""
    if isinstance(exc, ImageError):
        return bool(getattr(exc, "retryable", False)) or exc.code in _RETRYABLE_CODES
    return False


def _set_provider_calls(exc: BaseException, calls: int) -> None:
    """把"真实发起的第三方调用次数"挂到异常上（P1-6：失败路径也要能记发起量）。

    发起量是 API 可用性的分母（使用量/发起量）。provider 抛错时过去只丢一句"失败"，
    重试次数全部蒸发，指标里的可用率就会虚高（显示 100% 实际打了 3 次）。
    """
    extra = getattr(exc, "extra", None)
    if isinstance(extra, dict):
        extra["provider_calls"] = max(0, int(calls))


def _provider_calls_of(exc: BaseException) -> int:
    extra = getattr(exc, "extra", None)
    if isinstance(extra, dict):
        try:
            return max(0, int(extra.get("provider_calls") or 0))
        except (TypeError, ValueError):
            return 0
    return 0


async def _generate_with_retry(provider, request) -> tuple[Any, int]:
    """调一次 provider；限流/超时/5xx 按 `IMAGE_PROVIDER_RETRIES` 退避重试。

    返回 `(result, provider_calls)`：`provider_calls` 是**真实发起次数**（含重试），
    对应「发起量」口径；使用量（成功张数）仍只看最终结果。

    失败时把本次已发起的次数挂到异常的 `extra["provider_calls"]` 上（P1-6），
    由 `run()` 聚合成整轮的真实发起量再往外抛。
    """
    from app.agents_image.config import get_image_settings

    settings = get_image_settings()
    attempts = max(1, int(getattr(settings, "provider_retries", 2) or 0) + 1)
    backoff = max(0.0, float(getattr(settings, "provider_retry_backoff_seconds", 0.2) or 0.0))
    last_exc: BaseException | None = None
    attempt = 0
    try:
        for attempt in range(1, attempts + 1):
            try:
                return await provider.generate(request), attempt
            except ImageError as exc:
                last_exc = exc
                if attempt >= attempts or not _retryable(exc):
                    raise
                logger.warning("生图调用失败（第 %d/%d 次，可重试）：%s", attempt, attempts,
                               exc.code)
            except Exception as exc:  # noqa: BLE001 —— 未分类异常一律不重试（避免放大故障）
                raise ImageError("IMG_PROVIDER_UNAVAILABLE",
                                 f"生图调用异常：{type(exc).__name__}: {exc}") from exc
            if backoff > 0:
                await asyncio.sleep(backoff * attempt)
        raise last_exc if last_exc is not None else ImageError("IMG_PROVIDER_UNAVAILABLE",
                                                              "生图调用失败")
    except BaseException as exc:  # noqa: BLE001 —— 失败也要带上真实发起次数
        _set_provider_calls(exc, max(1, attempt))
        raise


async def run(payload: dict[str, Any]) -> dict[str, Any]:
    """payload 见 `local_generator._payload_for_task()`；返回 {slots: [...], provider, model}。"""
    provider = get_provider(payload.get("provider") or None)
    refs: list[ImageInput] = [
        ImageInput(data=item["data"], mime=item.get("mime") or "image/png",
                   sha256=item.get("sha256") or "")
        for item in payload.get("refs") or []
    ]
    if not refs:
        raise ImageError("IMG_REFERENCE_REQUIRED", "图生图必须提供参考图（本 Agent 不做文生图）")

    slots = payload.get("slots") or [{"slot": 1, "prompt": payload.get("prompt", "")}]
    results: list[dict[str, Any]] = []
    provider_calls = 0
    try:
        for slot in slots:
            request = ImageRequest(
                prompt=slot.get("prompt") or payload.get("prompt", ""),
                negative=slot.get("negative_prompt", ""),
                init_images=refs,
                width=int(payload.get("width") or 1024),
                height=int(payload.get("height") or 1024),
                n=int(slot.get("n") or 1),
                seed=payload.get("seed"),
                preserve=list(payload.get("preserve") or []),
            )
            async with _provider_semaphore():
                result, calls = await _generate_with_retry(provider, request)
            provider_calls += int(calls)
            results.append({
                "slot": int(slot.get("slot") or 1),
                "provider_calls": int(calls),
                "images": result.images,
                "mime": result.mime,
                "provider": result.provider,
                "model": result.model,
                "cost_cny": result.cost_cny,
                "latency_ms": result.latency_ms,
                "meta": result.meta,
            })
    except BaseException as exc:  # noqa: BLE001 —— P1-6：失败也要把发起量带出去
        # 已经成功跑完的图位 + 失败图位自己的发起次数 = 整轮真实发起量
        _set_provider_calls(exc, provider_calls + _provider_calls_of(exc))
        raise
    return {"slots": results, "provider": provider.name,
            "provider_calls": sum(int(item.get("provider_calls") or 0) for item in results),
            "model": results[0]["model"] if results else ""}
