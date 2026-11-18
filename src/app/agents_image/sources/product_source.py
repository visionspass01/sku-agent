"""`PlatformCatalog` 协议 + 数据模型 + 注册表。

Agent 只认这套接口；`IMAGE_PRODUCT_SOURCE=fake|mcp` 决定实现。
失败语义（关键）：
- 商品不存在 → `IMG_PRODUCT_NOT_FOUND`(404)
- 中台不可用/超时/协议错 → `IMG_UPSTREAM_MCP_UNAVAILABLE`(503)
- 中台返回字段不合 schema → `IMG_UPSTREAM_INVALID`(502)
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.agents_image.config import get_image_settings


class ProductSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    product_id: str
    title: str = ""
    category: str = "通用"
    main_image_id: str = ""
    updated_at: str = ""


class ProductImage(BaseModel):
    """商品参考图：`url`（生产中台给可下载地址）或 `bytes_b64`（离线/测试直给字节）。

    `color` 是**颜色维度**（M3）：图生图按「商品+颜色」出图，参考图必须取对应颜色的图，
    否则米色商品会被画成黑色——这是服装电商最常见的翻车点。
    """

    model_config = ConfigDict(extra="ignore")

    image_id: str = ""
    url: str = ""
    bytes_b64: str = ""
    mime: str = "image/png"
    is_main: bool = False
    width: int = 0
    height: int = 0
    color: str = ""
    color_hex: str = ""

    @property
    def has_payload(self) -> bool:
        return bool(self.bytes_b64 or self.url)


class ProductSku(BaseModel):
    """商品的一个颜色/尺码组合（出图的最小业务单位 = 商品 + 颜色）。"""

    model_config = ConfigDict(extra="ignore")

    sku_id: str = ""
    color: str = ""
    color_hex: str = ""
    size: str = ""


class ProductData(BaseModel):
    """商品完整数据：身份锁定字段 + 参考图 + 颜色维度（用户不可上传/替换）。"""

    model_config = ConfigDict(extra="ignore")

    product_id: str
    title: str = ""
    category: str = "通用"
    attrs: dict[str, Any] = Field(default_factory=dict)
    images: list[ProductImage] = Field(default_factory=list)
    skus: list[ProductSku] = Field(default_factory=list)
    sku_ids: list[str] = Field(default_factory=list)

    def colors(self) -> list[dict]:
        """可用颜色列表（去重，保留顺序）：`[{color, color_hex, image_ids: [...]}]`。"""
        out: list[dict] = []
        seen: set[str] = set()
        for sku in self.skus:
            key = (sku.color or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({"color": key, "color_hex": sku.color_hex,
                        "sku_ids": [sku.sku_id] if sku.sku_id else []})
        if not out:
            # 老商品没有颜色维度：退化为"商品主图"一个颜色
            out.append({"color": "", "color_hex": "", "sku_ids": list(self.sku_ids)})
        return out


class SimilarProduct(BaseModel):
    """拍立淘相似款（W3）：`sales` 用于按销量门禁过滤，`image` 是该商品主图。"""

    model_config = ConfigDict(extra="ignore")

    product_id: str = ""
    title: str = ""
    sales: int = 0
    image: ProductImage | None = None


class Preset(BaseModel):
    """构图 / 场景风格候选（`preset_id` + 展示文案 + 提示词线索）。"""

    model_config = ConfigDict(extra="ignore")

    preset_id: str
    kind: str = "composition"      # composition | scene_style | platform_rule
    label: str = ""
    desc: str = ""
    prompt_hint: str = ""


class SlotSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    slot: int
    role: str = ""
    type: str = "generate"         # generate | layout | photo_required
    composition_id: str = ""
    scene_style_id: str = ""


class PlanPolicy(BaseModel):
    """9 图位框架 + 默认张数 + 默认构图/场景风格 + 差异化方向（全部来自中台）。"""

    model_config = ConfigDict(extra="ignore")

    default_count: int = 6
    max_count: int = 9
    slots: list[SlotSpec] = Field(default_factory=list)
    default_composition_id: str = ""
    default_scene_style_id: str = ""
    default_composition_label: str = ""
    default_scene_style_label: str = ""
    # M5：variant 差异化方向（同款多店铺/多链接测试用），A/B 两套主图方向不同
    variant_directions: list[dict] = Field(default_factory=list)
    # M5：图内文案表（按图位/角色给字数上限；平台是否允许文字由 PlatformRules.copy_policy 决定）
    copy_table: dict = Field(default_factory=dict)


class PlatformRules(BaseModel):
    """平台规则卡：尺寸/背景/禁用词，以及 **copy_policy（图内文字）** 与质量阈值。"""

    model_config = ConfigDict(extra="ignore")

    platform: str = "淘系"
    aspect: str = "1:1"
    min_px: int = 800
    max_mb: float = 3.0
    background: str = "纯白"
    text_allowed: bool = True
    forbidden_words: list[str] = Field(default_factory=list)
    notes: str = ""
    # 图内文案策略（M5）：淘系允许主图叠字，抖系主图禁营销文字——这是硬冲突，不是偏好
    copy_policy: dict = Field(default_factory=dict)
    # 像素门禁阈值（M5）：与 scripts/check_image.py 的平台阈值保持同源
    compliance: dict = Field(default_factory=dict)

    @property
    def headline_allowed(self) -> bool:
        return bool((self.copy_policy or {}).get("headline_allowed", self.text_allowed))

    @property
    def max_headline_words(self) -> int:
        return int((self.copy_policy or {}).get("max_headline_words", 4) or 4)

    @property
    def max_subhead_words(self) -> int:
        return int((self.copy_policy or {}).get("max_subhead_words", 7) or 7)

    @property
    def badge_allowed(self) -> bool:
        """平台是否允许纯数字角标（A2：抖系允许角标、禁止营销文字）。"""
        return bool((self.copy_policy or {}).get("badge_allowed", True))

    @property
    def brand_only(self) -> bool:
        """平台是否只允许品牌标识（A2：抖系 True）。"""
        return bool((self.copy_policy or {}).get("brand_only", False))

    @property
    def max_text_area_pct(self) -> int:
        return int((self.copy_policy or {}).get("max_area_pct", 20) or 20)


@runtime_checkable
class PlatformCatalog(Protocol):
    """取数接缝：所有生图参数与商品数据的唯一来源。"""

    name: str

    async def list_products(self, *, keyword: str = "", limit: int = 20) -> list[ProductSummary]: ...

    async def get_product(self, product_id: str) -> ProductData: ...

    async def list_compositions(self, *, category: str = "", platform: str = "") -> list[Preset]: ...

    async def list_scene_styles(self, *, category: str = "", platform: str = "") -> list[Preset]: ...

    async def get_plan_policy(self, *, platform: str = "", category: str = "") -> PlanPolicy: ...

    async def get_platform_rules(self, *, platform: str = "") -> PlatformRules: ...

    async def save_result(self, payload: dict) -> dict: ...


_INSTANCES: dict[str, PlatformCatalog] = {}


def register_catalog(name: str, catalog: PlatformCatalog) -> None:
    _INSTANCES[name.strip().lower()] = catalog


def get_catalog(name: str | None = None) -> PlatformCatalog:
    """按 `IMAGE_PRODUCT_SOURCE`（默认 fake）取实现；未知实现直接报配置错。"""
    from app.core.errors import raise_image

    key = (name or get_image_settings().product_source or "fake").strip().lower()
    if key not in _INSTANCES:
        raise_image("IMG_INTERNAL", f"未知 IMAGE_PRODUCT_SOURCE={key}（可选 {sorted(_INSTANCES)}）")
    return _INSTANCES[key]


def registered_catalogs() -> list[str]:
    return sorted(_INSTANCES)


def reset_catalogs() -> None:
    _INSTANCES.clear()


def _ensure_builtin() -> None:
    """首次取用时注册内置实现（fake 永远可用；mcp 由 D6 注册，缺依赖时静默跳过）。"""
    from app.agents_image.sources.fake_catalog import FakeCatalog

    if "fake" not in _INSTANCES:
        _INSTANCES["fake"] = FakeCatalog()
    if "mcp" not in _INSTANCES:
        try:
            from app.agents_image.sources.mcp_catalog import McpCatalog

            _INSTANCES["mcp"] = McpCatalog()
        except Exception:  # noqa: BLE001 —— D6 之前 mcp 实现还不存在
            pass


_ensure_builtin()

__all__ = [
    "PlatformCatalog", "ProductSummary", "ProductImage", "ProductSku", "ProductData", "Preset",
    "SlotSpec", "PlanPolicy", "PlatformRules", "register_catalog", "get_catalog",
    "registered_catalogs", "reset_catalogs",
]
