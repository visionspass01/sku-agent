"""生图 Agent 的 HTTP 路由（`/api/image/*`）。

D1 阶段只有 `/api/image/health`；上传/任务/取图在 M1（D2–D3）加入，认证在 D4 加入。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.agents_image.config import get_image_settings
from app.agents_image.providers.image_provider import providers

logger = logging.getLogger("sku-agent.image.api")

router = APIRouter(prefix="/api/image", tags=["image"])


@router.get("/health")
def image_health() -> dict:
    """Image Agent 自身的健康（与 SKU 的 `/api/health` 汇总里 `image` 段同源）。"""
    settings = get_image_settings()
    return {
        "status": "ok" if settings.enabled else "disabled",
        "provider": settings.provider,
        "providers_registered": providers(),
        "vision_provider": settings.vision_provider,
        "storage": settings.storage,
        "dispatcher": settings.dispatcher,
        "concurrency": settings.concurrency,
        "max_refs": settings.max_refs,
        "secret_is_default": settings.secret_is_default,
        "degraded": False,
        "degraded_reason": "",
    }
