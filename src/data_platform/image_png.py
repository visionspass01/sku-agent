"""中台侧占位图生成（纯标准库，不依赖 Agent 侧代码）。

演示要**离线可跑**：中台没有真实商品图库，就用确定性占位图（同 id 同字节）；
生产接 OSS/COS 时只需要把 `image_mock.image_bytes` 换成回源。
"""

from __future__ import annotations

import hashlib
import struct
import zlib


def encode_png(width: int, height: int, rgb_rows: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    stride = width * 3
    raw = b"".join(b"\x00" + rgb_rows[y * stride:(y + 1) * stride] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def placeholder_png(seed: str, width: int = 640, height: int = 640) -> bytes:
    """商品图占位：斜向渐变 + 左上角棋盘标记 + 右下角边框（肉眼可辨，不冒充真实商品图）。"""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    base = (digest[0] % 96 + 80, digest[1] % 96 + 80, digest[2] % 96 + 80)
    accent = (255 - base[0] // 2, 255 - base[1] // 2, 255 - base[2] // 2)
    rows = bytearray()
    for y in range(height):
        for x in range(width):
            v = (x + y + digest[3]) % 64
            r = min(255, base[0] + v)
            g = min(255, base[1] + (v // 2))
            b = min(255, base[2] + (v // 3))
            if (x < 96 and y < 96 and ((x // 12 + y // 12) % 2 == 0)) \
                    or x > width - 24 or y > height - 24:
                r, g, b = accent
            rows += bytes((r, g, b))
    return encode_png(width, height, bytes(rows))


__all__ = ["encode_png", "placeholder_png"]
