"""纯标准库的 PNG 编码 + 占位图生成（`providers/fake.py` 与 `sources/fake_catalog.py` 共用）。

不引 Pillow：开发/测试/无 Key 演示要能零依赖出图；像素级校验（D17）才用可选 Pillow。
"""

from __future__ import annotations

import hashlib
import struct
import zlib


def encode_png(width: int, height: int, rgb_rows: bytes) -> bytes:
    """把 RGB 行数据（每行 `width*3` 字节，无 filter 前缀）编码成 PNG。"""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    stride = width * 3
    raw = b"".join(b"\x00" + rgb_rows[y * stride:(y + 1) * stride] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def placeholder_png(seed: str, width: int = 512, height: int = 512, *,
                    margin: int = 24, mark: int = 96) -> bytes:
    """由 `seed` 派生确定性占位图：斜向渐变 + 左上角棋盘标记 + 右下角边框。

    同 seed 必然同字节，便于测试断言；肉眼看得出是占位图（不冒充真实商品图）。
    """
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
            if (x < mark and y < mark and ((x // 12 + y // 12) % 2 == 0)) \
                    or x > width - margin or y > height - margin:
                r, g, b = accent
            rows += bytes((r, g, b))
    return encode_png(width, height, bytes(rows))


def placeholder_compliant_png(seed: str, width: int = 1024, height: int = 1024) -> bytes:
    """**合规的**占位图：纯白背景 + 居中主体矩形（占画面 ~60%）+ 四周留白。

    FakeProvider 用它出图，这样「交付前像素门禁」也能在离线环境被真实走到：
    白底纯度、主体占比、留白都在平台阈值内；真实 provider 出图后再由门禁判真假。
    """
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    subject = (digest[0] % 96 + 60, digest[1] % 96 + 60, digest[2] % 96 + 60)
    left, right = int(width * 0.2), int(width * 0.8)
    top, bottom = int(height * 0.2), int(height * 0.8)
    stripe = digest[3] % 32
    rows = bytearray()
    for y in range(height):
        for x in range(width):
            if left <= x < right and top <= y < bottom:
                v = (x + y + stripe) % 48
                rows += bytes((min(255, subject[0] + v), min(255, subject[1] + (v // 2)),
                               min(255, subject[2] + (v // 3))))
            else:
                rows += bytes((255, 255, 255))
    return encode_png(width, height, bytes(rows))


__all__ = ["encode_png", "placeholder_png", "placeholder_compliant_png"]
