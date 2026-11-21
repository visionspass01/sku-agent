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
    # 因部门作用域被过滤掉的条数（第四轮 P1-8：清单过去完全不按部门过滤）
    filtered: int = 0


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


class ProductContextListOut(Envelope):
    """批量商品上下文（W2）：`products` 命中，`missing` 由 Agent 翻成 404，
    `forbidden` 是调用方部门看不到的商品（第三轮 P1-13 的中台兜底）。"""

    products: list[ProductContextOut] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    count: int = 0


class SimilarIn(Envelope):
    """拍立淘入参（W3）：`image_b64` 是待搜图（Agent 侧把素材库图/商品主图编码后传）。"""

    image_b64: str = ""
    min_sales: int = 100
    top_n: int = 5
    exclude: str = ""


class SimilarItemOut(Envelope):
    """相似款：`sales` 是销量口径（用于 > 门槛过滤与降序），`image` 是商品主图。"""

    product_id: str = ""
    title: str = ""
    sales: int = 0
    image: ProductImageOut | None = None


class SimilarOut(Envelope):
    items: list[SimilarItemOut] = Field(default_factory=list)
    count: int = 0
    min_sales: int = 100
    filtered: int = 0     # 因部门作用域被过滤掉的条数（第三轮 P1-13）


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
    reused: bool = False
    copy_json: dict = Field(default_factory=dict)
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
    copy_json: dict = Field(default_factory=dict)
    ref_source: str = "mcp"
    variant_id: str = ""
    variant_direction: str = ""
    expected_images: int = 0
    partial: bool = False
    warnings: list[str] = Field(default_factory=list)
    blocked: list[dict] = Field(default_factory=list)
    params: dict = Field(default_factory=dict)
    requester: str = ""
    trace_id: str = ""
    results: list[ImageResultIn] = Field(default_factory=list)


class ResultsOut(Envelope):
    saved: int = 0
    deduped: int = 0
    # 同 (task_id, slot) 但内容变了 → 覆盖（第四轮 P2-14：重试后重发不能丢新结果）
    updated: int = 0
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
    reused: bool = False
    copy_json: dict = Field(default_factory=dict)
    variant_id: str = ""
    ref_source: str = "mcp"
    partial: bool = False
    expected_images: int = 0
    blocked: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: str = ""


class ResultListOut(Envelope):
    product_id: str = ""
    items: list[ResultRowOut] = Field(default_factory=list)
    count: int = 0


class ResultBinaryOut(Envelope):
    """结果图片二进制落库的回执（第十二轮：worker → 中台文件夹 → 路径交回 Agent）。"""

    storage: str = "platform"
    storage_key: str = ""
    sha256: str = ""
    mime: str = "image/png"
    bytes: int = 0
    width: int = 0
    height: int = 0
    url: str = ""
    expire_at: str = ""
