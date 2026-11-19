"""生图 Provider 插件位：**换供应商只加一个文件**。

契约要点：
- 图生图（`init_images` 必须非空，1..caps.max_refs）；
- 参数差异（参考图字段、size 档位、response_format、watermark、n/seed 支持度）
  全部收在各 provider 文件里映射，图/节点层只见 `ImageRequest`；
- 失败一律抛 `ImageError`（错误码在 `app.core.errors.IMAGE_CODES` 登记）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import raise_image


class ImageInput(BaseModel):
    """一张参考图（内存里只留 bytes + sha256；落盘由 storage 负责）。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    data: bytes
    mime: str = "image/png"
    sha256: str = ""


class ImageRequest(BaseModel):
    prompt: str
    negative: str = ""
    init_images: list[ImageInput] = Field(default_factory=list)
    reference_mode: str = "subject"     # subject（保主体）| style（保风格）| composition
    strength: float = 0.45
    width: int = 1024
    height: int = 1024
    n: int = 1
    seed: int | None = None
    preserve: list[str] = Field(default_factory=list)
    watermark: bool = False


class ImageResult(BaseModel):
    images: list[bytes] = Field(default_factory=list)
    mime: str = "image/png"
    provider: str = ""
    model: str = ""
    cost_cny: float = 0.0
    latency_ms: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)


class ProviderCaps(BaseModel):
    supports_reference: bool = True
    max_refs: int = 6
    strength_range: tuple[float, float] = (0.0, 1.0)
    supports_seed: bool = True
    max_n: int = 4
    supports_negative: bool = True


@runtime_checkable
class ImageProvider(Protocol):
    name: str
    caps: ProviderCaps

    async def generate(self, req: ImageRequest) -> ImageResult: ...
    async def health(self) -> dict: ...


_REGISTRY: dict[str, Any] = {}


def register(name: str, factory: Any) -> None:
    """注册 provider 工厂（无参可调用 → provider 实例）。"""
    _REGISTRY[name.strip().lower()] = factory


def providers() -> list[str]:
    return sorted(_REGISTRY)


def get_provider(name: str | None = None) -> ImageProvider:
    from app.agents_image.config import get_image_settings

    settings = get_image_settings()
    key = (name or settings.provider or "fake").strip().lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        raise_image("IMG_PROVIDER_UNAVAILABLE", f"未注册的 provider：{key}（可用：{providers()}）")
    return factory()


def effective_max_refs(settings=None, provider=None) -> int:
    """参考图上限 = `min(IMAGE_MAX_REFS, provider.caps.max_refs)`（第四轮裁决 ③）。

    配置上限留弹性（可配到 12），但真正生效的上限必须与当前 provider 的能力对齐：
    否则 `IMAGE_MAX_REFS=12` + provider `caps.max_refs=6` 时，创建期放行、执行期才在
    `validate_request` 里炸（用户等到出图才看到 IMG_TOO_MANY_REFS）。现在创建期就报
    `IMG_REFS_BUDGET`，错误提前且可解释；`/health` 也会暴露这个生效值。
    """
    from app.agents_image.config import get_image_settings

    settings = settings or get_image_settings()
    cap = int(getattr(settings, "max_refs", 6) or 6)
    provider_cap = 0
    try:
        inst = provider if provider is not None else get_provider(None)
        caps = getattr(inst, "caps", None)
        provider_cap = int(getattr(caps, "max_refs", 0) or 0)
    except Exception:  # noqa: BLE001 —— provider 不可用时退回配置值（health 另有告警）
        provider_cap = 0
    if provider_cap > 0:
        cap = min(cap, provider_cap)
    return max(1, cap)


def validate_request(req: ImageRequest, caps: ProviderCaps) -> None:
    """与 provider 能力对齐的通用校验（供应商无关）。"""
    if not caps.supports_reference:
        raise_image("IMG_PROVIDER_UNAVAILABLE", "provider 不支持图生图（caps.supports_reference=false）")
    refs = len(req.init_images)
    if refs == 0:
        raise_image("IMG_REFERENCE_REQUIRED", "图生图必须提供参考图（本 Agent 不做文生图）")
    if refs > caps.max_refs:
        raise_image("IMG_TOO_MANY_REFS", f"参考图最多 {caps.max_refs} 张，收到 {refs} 张")
    if req.n < 1 or req.n > caps.max_n:
        raise_image("IMG_INPUT_INVALID", f"n 需在 1..{caps.max_n}，收到 {req.n}")
    lo, hi = caps.strength_range
    if not lo <= req.strength <= hi:
        raise_image("IMG_INPUT_INVALID", f"strength 需在 {lo}..{hi}，收到 {req.strength}")
    if req.width < 64 or req.height < 64 or req.width > 4096 or req.height > 4096:
        raise_image("IMG_INPUT_INVALID", f"尺寸不合法：{req.width}x{req.height}")
