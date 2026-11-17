"""Fake 中台替身（开发/测试/离线演示）：确定性商品清单 + 商品图 + 预设。

设计要点：
- 商品图用 `png.placeholder_png` 直出字节（`bytes_b64`），**不需要网络**，因此 D5.5 之后
  的测试仍然离线、仍然能出图；
- 特意留了三种"坏商品"喂错误路径：`P-NOIMG`（无图）、`P-EMPTYIMG`（有图记录但取不到字节）；
- 商品 id / 图片 id 都是稳定映射，测试可直接断言。
"""

from __future__ import annotations

import base64

from app.agents_image.png import placeholder_png
from app.agents_image.sources.product_source import (
    PlanPolicy,
    PlatformRules,
    Preset,
    ProductData,
    ProductImage,
    ProductSummary,
    SlotSpec,
)
from app.core.errors import raise_image

# 商品 → 身份锁定字段（与中台 `image_catalog` mock 同构，D6 时中台返回同样的形状）
_PRODUCTS: dict[str, dict] = {
    "P-1001": {
        "title": "女装·针织开衫（短款）", "category": "女装",
        "attrs": {"product_type": "针织开衫（短款）", "category": "女装",
                  "shape": "落肩、V 领、单排 5 粒扣", "color_hex": "#C8B7A6",
                  "color_names": ["米色"], "logo_text": "胸前 LILY 刺绣",
                  "materials": "羊毛混纺 30%、表面细密针织纹",
                  "key_features": ["罗纹袖口", "双口袋"],
                  "must_not_change": ["颜色", "版型", "扣子数量", "刺绣文字", "袖口罗纹"],
                  "source": "platform", "confidence": 0.95},
        "images": 3,
    },
    "P-1002": {
        "title": "男装·工装直筒裤", "category": "男装",
        "attrs": {"product_type": "工装直筒裤", "category": "男装",
                  "shape": "直筒、高腰、双侧贴袋", "color_hex": "#4A6B8A",
                  "color_names": ["藏蓝"], "logo_text": "无",
                  "materials": "棉 98%、弹力纤维 2%",
                  "key_features": ["侧边贴袋", "可调节裤脚"],
                  "must_not_change": ["颜色", "版型", "口袋位置", "裤脚结构"],
                  "source": "platform", "confidence": 0.93},
        "images": 2,
    },
    "P-2001": {
        "title": "童装·卫衣（加绒）", "category": "童装",
        "attrs": {"product_type": "加绒卫衣", "category": "童装",
                  "shape": "宽松套头、落肩", "color_hex": "#3F7A5E",
                  "color_names": ["草绿"], "logo_text": "胸前小熊图案",
                  "materials": "棉 80%、聚酯纤维 20%",
                  "key_features": ["加绒内里", "罗纹下摆"],
                  "must_not_change": ["颜色", "版型", "图案"],
                  "source": "platform", "confidence": 0.9},
        "images": 4,
    },
    "P-3001": {
        "title": "女装·半身裙（长款）", "category": "女装",
        "attrs": {"product_type": "半身裙（长款）", "category": "女装",
                  "shape": "A 字、中长、侧隐形拉链", "color_hex": "#111111",
                  "color_names": ["黑色"], "logo_text": "无",
                  "materials": "聚酯纤维 100%",
                  "key_features": ["侧隐形拉链", "内衬"],
                  "must_not_change": ["颜色", "版型", "长度"],
                  "source": "platform", "confidence": 0.91},
        "images": 2,
    },
    "P-4001": {"title": "童装·背带裤", "category": "童装", "attrs": {}, "images": 0},   # 无图
    "P-4002": {"title": "女装·羽绒马甲", "category": "女装", "attrs": {"product_type": "羽绒马甲",
              "category": "女装", "shape": "立领、短款", "color_hex": "#C0392B"},
              "images": 0},                                                             # 无图
}

_COMPOSITIONS = (
    Preset(preset_id="C-FULL-FRONT-TOP", kind="composition",
           label="正面全身像·上装突出", desc="全身入镜，重心在上装",
           prompt_hint="正面站立全身像，上装占画面主体"),
    Preset(preset_id="C-SIDE-LOWER", kind="composition",
           label="侧身像·下装突出", desc="侧身，重心在下装",
           prompt_hint="侧身站立，下装占画面主体"),
    Preset(preset_id="C-HALF-FRONT", kind="composition",
           label="正面半身", desc="腰部以上近景",
           prompt_hint="正面半身近景，突出面料与领口"),
    Preset(preset_id="C-DETAIL-MACRO", kind="composition",
           label="细节特写", desc="面料/做工微距",
           prompt_hint="微距特写：面料纹理与走线"),
    Preset(preset_id="C-FLAT-LAY", kind="composition",
           label="平铺挂拍", desc="平铺或挂拍",
           prompt_hint="平铺俯拍，商品铺满画面"),
)

_SCENE_STYLES = (
    Preset(preset_id="S-STUDIO-FORMAL", kind="scene_style",
           label="影棚正式风", desc="灰白影棚背景 + 柔和棚拍光",
           prompt_hint="影棚拍摄，灰白无缝背景，柔和定向光"),
    Preset(preset_id="S-STREET-FRESH", kind="scene_style",
           label="街拍清新风", desc="街头自然光 + 生活化背景",
           prompt_hint="城市街头自然光，环境虚化，清新色调"),
    Preset(preset_id="S-FACTORY-PRACTICAL", kind="scene_style",
           label="工厂实用风", desc="车间/仓库真实场景",
           prompt_hint="工厂仓库实景，实用主义构图"),
    Preset(preset_id="S-WHITE-E-COM", kind="scene_style",
           label="白底电商风", desc="纯白背景，无道具",
           prompt_hint="纯白背景，无道具，标准电商主图"),
    Preset(preset_id="S-HOME-COZY", kind="scene_style",
           label="家居温馨风", desc="室内暖光居家场景",
           prompt_hint="室内居家场景，暖色自然光"),
)

_SLOTS = (
    SlotSpec(slot=1, role="主图", type="generate",
             composition_id="C-FULL-FRONT-TOP", scene_style_id="S-WHITE-E-COM"),
    SlotSpec(slot=2, role="上装卖点", type="generate",
             composition_id="C-HALF-FRONT", scene_style_id="S-STUDIO-FORMAL"),
    SlotSpec(slot=3, role="下装卖点", type="generate",
             composition_id="C-SIDE-LOWER", scene_style_id="S-STUDIO-FORMAL"),
    SlotSpec(slot=4, role="场景 A", type="generate",
             composition_id="C-FULL-FRONT-TOP", scene_style_id="S-STREET-FRESH"),
    SlotSpec(slot=5, role="场景 B", type="generate",
             composition_id="C-SIDE-LOWER", scene_style_id="S-STREET-FRESH"),
    SlotSpec(slot=6, role="细节特写", type="generate",
             composition_id="C-DETAIL-MACRO", scene_style_id="S-STUDIO-FORMAL"),
    SlotSpec(slot=7, role="尺码对照", type="layout",
             composition_id="C-FLAT-LAY", scene_style_id="S-WHITE-E-COM"),
    SlotSpec(slot=8, role="配件/包装清单", type="layout",
             composition_id="C-FLAT-LAY", scene_style_id="S-WHITE-E-COM"),
    SlotSpec(slot=9, role="售后/退换", type="photo_required"),
)

_RULES = {
    "淘系": PlatformRules(platform="淘系", aspect="1:1", min_px=800, max_mb=3.0,
                         background="纯白", text_allowed=True,
                         forbidden_words=["全网最低价", "国家级", "绝对"],
                         notes="主图 1:1 白底，不得拼接与水印"),
    "通用": PlatformRules(platform="通用", aspect="1:1", min_px=800, max_mb=5.0,
                         background="纯白或浅色", text_allowed=True,
                         forbidden_words=["伪造", "官方认证"],
                         notes="通用电商主图规范"),
}

_SEED_IMAGES = {"P-1001": 3, "P-1002": 2, "P-2001": 4, "P-3001": 2}


class FakeCatalog:
    """离线中台替身；行为与 `McpCatalog` 对齐（同入参同出参形状）。"""

    name = "fake"

    def __init__(self) -> None:
        self.fail_next: str = ""      # 测试注入：设为 "unavailable"/"invalid" 让下一次调用失败

    # ---------------------------------------------------------------- 商品
    async def list_products(self, *, keyword: str = "", limit: int = 20) -> list[ProductSummary]:
        self._maybe_fail("list_products")
        rows = [ProductSummary(product_id=pid, title=spec["title"], category=spec["category"],
                               main_image_id=f"{pid}-img-1" if spec["images"] else "",
                               updated_at="2026-11-17T09:00:00+08:00")
                for pid, spec in _PRODUCTS.items()]
        if keyword:
            rows = [row for row in rows if keyword in row.title or keyword in row.product_id]
        return rows[: max(1, int(limit))]

    async def get_product(self, product_id: str) -> ProductData:
        self._maybe_fail(getattr(self, "fail_product", ""))
        spec = _PRODUCTS.get((product_id or "").strip())
        if spec is None:
            raise_image("IMG_PRODUCT_NOT_FOUND", f"商品不存在：{product_id}")
        count = int(spec.get("images") or 0)
        images = [
            ProductImage(image_id=f"{product_id}-img-{i + 1}", mime="image/png",
                         is_main=(i == 0), width=640, height=640,
                         bytes_b64=base64.b64encode(
                             placeholder_png(f"{product_id}:{i}", 640, 640)).decode("ascii"))
            for i in range(count)
        ]
        return ProductData(product_id=product_id, title=spec["title"],
                           category=spec["category"], attrs=dict(spec.get("attrs") or {}),
                           images=images)

    async def list_compositions(self, *, category: str = "",
                                platform: str = "") -> list[Preset]:
        self._maybe_fail("list_compositions")
        return list(_COMPOSITIONS)

    async def list_scene_styles(self, *, category: str = "", platform: str = "") -> list[Preset]:
        self._maybe_fail("list_scene_styles")
        return list(_SCENE_STYLES)

    async def get_plan_policy(self, *, platform: str = "", category: str = "") -> PlanPolicy:
        self._maybe_fail("get_plan_policy")
        return PlanPolicy(default_count=6, max_count=9, slots=list(_SLOTS),
                          default_composition_id="C-FULL-FRONT-TOP",
                          default_scene_style_id="S-WHITE-E-COM",
                          default_composition_label="正面全身像·上装突出",
                          default_scene_style_label="白底电商风")

    async def get_platform_rules(self, *, platform: str = "") -> PlatformRules:
        self._maybe_fail("get_platform_rules")
        return _RULES.get(platform or "淘系", _RULES["通用"])

    async def save_result(self, payload: dict) -> dict:
        self._maybe_fail("save_result")
        return {"saved": True, "deduped": False, "result_id": payload.get("task_id", "")}

    # ---------------------------------------------------------------- 调试
    def _maybe_fail(self, method: str) -> None:
        mode, self.fail_next = self.fail_next, ""
        if not mode:
            return
        if mode == "unavailable":
            raise_image("IMG_UPSTREAM_MCP_UNAVAILABLE",
                        f"中台不可用（fake 注入，方法 {method}）", retryable=True)
        if mode == "invalid":
            raise_image("IMG_UPSTREAM_INVALID", f"中台返回数据不合 schema（fake 注入，{method}）")


def seed_image_ids() -> dict[str, int]:
    """给测试用：商品 → 图片数。"""
    return dict(_SEED_IMAGES)


__all__ = ["FakeCatalog", "seed_image_ids"]
