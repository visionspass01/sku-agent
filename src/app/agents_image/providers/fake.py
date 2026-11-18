"""Fake 生图 Provider：**纯标准库**生成确定性 PNG（开发/测试/无 Key 演示）。

诚实性约定：输出是**占位图**，不是真实图生图结果——
- 内容由「参考图 sha256 + prompt + seed」派生，同输入必然同输出（便于断言）；
- 画面是"纯白背景 + 居中主体色块"，**刻意满足平台像素门禁**（白底纯度/主体占比/留白），
  这样离线测试也能真实走到"交付前合规门禁"这一步；它是占位图，不冒充真图；
- `meta={"placeholder": True}`，任务详情里也标出来。
"""

from __future__ import annotations

import time

from app.agents_image.png import placeholder_compliant_png
from app.agents_image.providers.image_provider import (
    ImageRequest,
    ImageResult,
    ProviderCaps,
    validate_request,
)


def _make_image(req: ImageRequest) -> bytes:
    seed_src = (req.init_images[0].sha256 if req.init_images else "") + req.prompt + \
               str(req.seed if req.seed is not None else 0)
    return placeholder_compliant_png(seed_src, int(req.width), int(req.height))


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
