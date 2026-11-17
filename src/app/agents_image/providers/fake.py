"""Fake 生图 Provider：**纯标准库**生成确定性 PNG（开发/测试/无 Key 演示）。

诚实性约定：输出是**占位图**，不是真实图生图结果——
- 内容由「参考图 sha256 + prompt + seed」派生，同输入必然同输出（便于断言）；
- 左上角画一块棋盘标记（代表 "FAKE"），右下角画边框，肉眼一眼能看出不是真图；
- `meta={"placeholder": True}`，任务详情里也标出来，绝不冒充真图。
"""

from __future__ import annotations

import hashlib
import struct
import time
import zlib

from app.agents_image.providers.image_provider import (
    ImageRequest,
    ImageResult,
    ProviderCaps,
    validate_request,
)


def _png(width: int, height: int, rgb_rows: bytes) -> bytes:
    """把 RGB 行数据（每行前加 filter=0）编码成 PNG。"""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    stride = width * 3
    raw = b"".join(b"\x00" + rgb_rows[y * stride:(y + 1) * stride] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _make_image(req: ImageRequest) -> bytes:
    seed_src = (req.init_images[0].sha256 if req.init_images else "") + req.prompt + \
               str(req.seed if req.seed is not None else 0)
    digest = hashlib.sha256(seed_src.encode("utf-8")).digest()
    base = (digest[0] % 96 + 80, digest[1] % 96 + 80, digest[2] % 96 + 80)
    accent = (255 - base[0] // 2, 255 - base[1] // 2, 255 - base[2] // 2)

    width, height = int(req.width), int(req.height)
    rows = bytearray()
    for y in range(height):
        for x in range(width):
            # 斜向条纹渐变：确定性、肉眼可辨
            v = (x + y + digest[3]) % 64
            r = min(255, base[0] + v)
            g = min(255, base[1] + (v // 2))
            b = min(255, base[2] + (v // 3))
            # 左上角标记块（"FAKE" 的替身）：16x16 棋盘
            mark = x < 96 and y < 96 and ((x // 12 + y // 12) % 2 == 0)
            # 右下角边框
            border = x > width - 24 or y > height - 24
            if mark or border:
                r, g, b = accent
            rows += bytes((r, g, b))
    return _png(width, height, bytes(rows))


class FakeProvider:
    """离线占位图 Provider（零外部依赖、零成本）。"""

    name = "fake"
    caps = ProviderCaps(supports_reference=True, max_refs=6, max_n=4)

    async def generate(self, req: ImageRequest) -> ImageResult:
        validate_request(req, self.caps)
        started = time.perf_counter()
        images = [_make_image(req) for _ in range(req.n)]
        latency = int((time.perf_counter() - started) * 1000)
        return ImageResult(
            images=images, mime="image/png", provider=self.name, model="fake-placeholder",
            cost_cny=0.0, latency_ms=latency,
            meta={"placeholder": True,
                  "note": "占位图（非真实图生图）；配置真实 provider 后才是实图"},
        )

    async def health(self) -> dict:
        return {"ok": True, "provider": self.name, "mode": "placeholder",
                "degraded": False, "degraded_reason": ""}
