"""生图 Provider 插件包：导入即注册内置实现（换供应商只加文件 + 一行 register）。"""

from __future__ import annotations

from app.agents_image.providers import image_provider as _contract
from app.agents_image.providers.fake import FakeProvider

_contract.register("fake", FakeProvider)

__all__ = ["FakeProvider"]
