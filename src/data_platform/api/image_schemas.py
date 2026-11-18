"""中台 image 模块的请求/响应模型（`/api/v1/image/*`）。"""

from __future__ import annotations

from pydantic import Field

from data_platform.api.schemas import Envelope


class ProductSummaryOut(Envelope):
    product_id: str
    title: str = ""
    category: str = ""
    main_image_id: str = ""
    updated_at: str = ""
    sku_ids: list[str] = Field(default_factory=list)


class ProductsOut(Envelope):
    products: list[ProductSummaryOut] = Field(default_factory=list)
    count: int = 0


class ProductImageOut(Envelope):
    image_id: str
    url: str = ""
    mime: str = "image/png"
    is_main: bool = False
    width: int = 0
    height: int = 0
    color: str = ""
    color_hex: str = ""


class ProductSkuOut(Envelope):
    sku_id: str = ""
    color: str = ""
    color_hex: str = ""
    size: str = ""


class ProductContextOut(Envelope):
    product_id: str
    title: str = ""
    category: str = ""
    attrs: dict = Field(default_factory=dict)
    images: list[ProductImageOut] = Field(default_factory=list)
    skus: list[ProductSkuOut] = Field(default_factory=list)
    sku_ids: list[str] = Field(default_factory=list)


class PresetOut(Envelope):
    preset_id: str
    kind: str = "composition"
    code: str = ""
    label: str = ""
    desc: str = ""
    prompt_hint: str = ""


class PresetsOut(Envelope):
    kind: str
    platform: str = ""
    category: str = ""
    items: list[PresetOut] = Field(default_factory=list)
    count: int = 0


class SlotOut(Envelope):
    slot: int
    role: str = ""
    type: str = "generate"
    composition_id: str = ""
    scene_style_id: str = ""


class PlanPolicyOut(Envelope):
    platform: str = "淘系"
    category: str = ""
    default_count: int = 6
    max_count: int = 9
    default_composition_id: str = ""
    default_scene_style_id: str = ""
    slots: list[SlotOut] = Field(default_factory=list)


class PlatformRulesOut(Envelope):
    platform: str = "淘系"
    aspect: str = "1:1"
    min_px: int = 800
    max_mb: float = 3.0
    background: str = "纯白"
    text_allowed: bool = True
    forbidden_words: list[str] = Field(default_factory=list)
    notes: str = ""


class ImageResultIn(Envelope):
    slot: int
    role: str = ""
    prompt: str = ""
    negative_prompt: str = ""
    model: str = ""
    sha256: str = ""
    mime: str = "image/png"
    width: int = 0
    height: int = 0
    bytes: int = 0
    storage: str = "local"
    storage_key: str = ""
    critic_score: int = 0
    critic_issues: list[str] = Field(default_factory=list)
    cost_cny: float = 0.0
    elapsed_ms: int = 0
    status: str = "done"


class ResultsIn(Envelope):
    """一次任务的结果回写（含需求快照 + 每个图位一行）。"""

    request_id: str = ""
    batch_id: str = ""
    product_id: str = ""
    task_id: str
    platform: str = "淘系"
    aspect: str = "1:1"
    count: int = 1
    compositions: list[str] = Field(default_factory=list)
    scene_styles: list[str] = Field(default_factory=list)
    prompt_final: str = ""
    prompt_pack: list[dict] = Field(default_factory=list)
    params: dict = Field(default_factory=dict)
    requester: str = ""
    trace_id: str = ""
    results: list[ImageResultIn] = Field(default_factory=list)


class ResultsOut(Envelope):
    saved: int = 0
    deduped: int = 0
    request_id: str = ""
    task_id: str = ""


class ResultRowOut(Envelope):
    request_id: str = ""
    product_id: str = ""
    task_id: str = ""
    slot: int = 0
    role: str = ""
    prompt: str = ""
    sha256: str = ""
    mime: str = "image/png"
    width: int = 0
    height: int = 0
    bytes: int = 0
    storage: str = "local"
    storage_key: str = ""
    critic_score: int = 0
    cost_cny: float = 0.0
    status: str = "done"
    created_at: str = ""


class ResultListOut(Envelope):
    product_id: str = ""
    items: list[ResultRowOut] = Field(default_factory=list)
    count: int = 0
