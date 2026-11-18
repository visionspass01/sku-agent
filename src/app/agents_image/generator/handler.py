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
            result = await provider.generate(request)
        results.append({
            "slot": int(slot.get("slot") or 1),
            "images": result.images,
            "mime": result.mime,
            "provider": result.provider,
            "model": result.model,
            "cost_cny": result.cost_cny,
            "latency_ms": result.latency_ms,
            "meta": result.meta,
        })
    return {"slots": results, "provider": provider.name,
            "model": results[0]["model"] if results else ""}
