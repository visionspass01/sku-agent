"""图片存储插件包：导入即注册 local（COS 在生产里程碑加入）。"""

from __future__ import annotations

from app.agents_image.storage import image_storage as _contract
from app.agents_image.storage.local_store import LocalStore

_contract.register("local", LocalStore)

__all__ = ["LocalStore"]
