"""图片存储插件位：本地（测试环境，24h 保留）｜腾讯云 COS（生产，后期启用）。"""

from __future__ import annotations

import struct
from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class AssetRef(BaseModel):
    key: str
    sha256: str
    mime: str
    width: int = 0
    height: int = 0
    bytes: int = 0
    storage: str = "local"


def sniff_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def image_size(data: bytes) -> tuple[int, int]:
    """纯标准库读宽高（PNG/JPEG/WebP）；读不到返回 (0, 0)。"""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data.startswith(b"\xff\xd8\xff"):
            idx = 2
            while idx + 9 < len(data):
                if data[idx] != 0xFF:
                    idx += 1
                    continue
                marker = data[idx + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
                    return struct.unpack(">H", data[idx + 7:idx + 9])[0], \
                        struct.unpack(">H", data[idx + 5:idx + 7])[0]
                seg_len = struct.unpack(">H", data[idx + 2:idx + 4])[0]
                idx += 2 + seg_len
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
            chunk = data[12:16]
            if chunk == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
            if chunk == b"VP8 ":
                w = int.from_bytes(data[26:28], "little") & 0x3FFF
                h = int.from_bytes(data[28:30], "little") & 0x3FFF
                return w, h
    except Exception:  # noqa: BLE001 —— 头部损坏时按未知尺寸处理
        return 0, 0
    return 0, 0


@runtime_checkable
class ImageStorage(Protocol):
    name: str

    def put(self, data: bytes, *, kind: str, mime: str) -> AssetRef: ...
    def get(self, key: str) -> bytes: ...
    def signed_url(self, key: str, *, asset_id: str = "", ttl_seconds: int = 3600) -> str: ...
    def delete(self, key: str) -> bool: ...
    def sweep_expired(self, hours: float) -> int: ...
    def health(self) -> dict: ...


_REGISTRY: dict = {}


def register(name: str, factory) -> None:
    _REGISTRY[name.strip().lower()] = factory


def storage_names() -> list[str]:
    return sorted(_REGISTRY)


def get_storage(name: str | None = None) -> ImageStorage:
    from app.agents_image.config import get_image_settings
    from app.core.errors import raise_image

    settings = get_image_settings()
    key = (name or settings.storage or "local").strip().lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        raise_image("IMG_PROVIDER_UNAVAILABLE", f"未注册的存储后端：{key}（可用：{storage_names()}）")
    return factory()
