"""生图 Agent 包：对外只暴露 router / 运行期钩子 / 配置（不 import SKU 业务包）。"""

from __future__ import annotations

from app.agents_image.api.routes import router
from app.agents_image.config import ImageSettings, get_image_settings, reset_image_settings
from app.agents_image.runtime import health_block, shutdown, startup
from app.agents_image.store.schema import ensure_image_schema

__all__ = [
    "router",
    "ImageSettings",
    "get_image_settings",
    "reset_image_settings",
    "ensure_image_schema",
    "health_block",
    "startup",
    "shutdown",
]
