"""健康段注册表：让各模块把自己的健康信息挂到 `/api/health`，而不让路由反向 import 业务包。

SKU 的 `/api/health` 只调 `collect()`；Image Agent 在启动时 `register("image", health_block)`。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger("sku-agent.health")

_REGISTRY: dict[str, Callable[[], dict]] = {}


def register(name: str, provider: Callable[[], dict]) -> None:
    _REGISTRY[name] = provider


def unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


def registered() -> list[str]:
    return sorted(_REGISTRY)


def collect() -> dict[str, dict]:
    """逐个调用；单个模块崩了只记日志，不影响整体健康响应。"""
    out: dict[str, dict] = {}
    for name, provider in list(_REGISTRY.items()):
        try:
            out[name] = provider()
        except Exception as exc:  # noqa: BLE001 —— 健康检查自己不能把服务搞挂
            logger.warning("健康段 %s 采集失败：%s", name, exc)
            out[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out


def reset() -> None:
    _REGISTRY.clear()
