"""生图 Agent 包：对外只暴露 router / 运行期钩子 / 配置（不 import SKU 业务包）。

**惰性导入**（PEP 562）：`app.agents_image.worker.*` 这类子模块在"只跑 worker"时
不应该把 FastAPI / SQLAlchemy / 后台任务一起拉起来（SCF 包只装第三方调用那一层）。
真实 Web 入口（`app.main`）都是直接 `from app.agents_image.api.routes import router`，
不依赖本文件的导入副作用。
"""

from __future__ import annotations

import importlib

_LAZY: dict[str, tuple[str, str]] = {
    "router": ("app.agents_image.api.routes", "router"),
    "ImageSettings": ("app.agents_image.config", "ImageSettings"),
    "get_image_settings": ("app.agents_image.config", "get_image_settings"),
    "reset_image_settings": ("app.agents_image.config", "reset_image_settings"),
    "ensure_image_schema": ("app.agents_image.store.schema", "ensure_image_schema"),
    "health_block": ("app.agents_image.runtime", "health_block"),
    "startup": ("app.agents_image.runtime", "startup"),
    "shutdown": ("app.agents_image.runtime", "shutdown"),
}


def __getattr__(name: str):  # noqa: ANN202 —— 惰性导出
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    value = getattr(importlib.import_module(module_name), attr)
    globals()[name] = value          # 缓存，后续访问不再走 __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(_LAZY)


__all__ = list(_LAZY)
