"""中台 image 模块的 mock 数据源：商品（含参考图）/ 构图 / 场景风格 / 图位策略 / 平台规则。

为什么放在中台而不是 Agent：v6 的定位是"**参数与商品数据只有中台一份真相**"，
Agent 侧只保留开发用的 fake 替身（`agents_image/sources/fake_catalog.py`），
两边形状一致（见 `docs/myself/IMAGE_AGENT_PLAN.md` §4），便于契约测试对照。

商品数据本身在真实中台来自 `dim_sku`/商品库；这里用固定 mock（8 个商品 + 2 个"无图"商品），
预设类数据则由 `image_catalog` 表承载（首次启动按本文件的 `PRESETS` 播种，之后可改库热更新）。
"""

from __future__ import annotations

import base64
import datetime as dt

from data_platform.image_png import placeholder_png

# ---------------------------------------------------------------- 商品（mock）
# images: 该商品可用的参考图数量（第 1 张为主图）
PRODUCTS: dict[str, dict] = {
    "P-1001": {
        "title": "女装·针织开衫（短款）", "category": "女装", "images": 3,
        "colors": [{"color": "米色", "color_hex": "#C8B7A6"}, {"color": "黑色", "color_hex": "#111111"}, {"color": "藏蓝", "color_hex": "#2C5D8F"}],
        "sku_ids": ["ST-1001-C1-S", "ST-1001-C1-M", "ST-1001-C1-L"],
        "attrs": {"product_type": "针织开衫（短款）", "category": "女装",
                  "shape": "落肩、V 领、单排 5 粒扣", "color_hex": "#C8B7A6",
                  "color_names": ["米色"], "logo_text": "胸前 LILY 刺绣",
                  "materials": "羊毛混纺 30%、表面细密针织纹",
                  "key_features": ["罗纹袖口", "双口袋"],
                  "must_not_change": ["颜色", "版型", "扣子数量", "刺绣文字", "袖口罗纹"],
                  "source": "platform", "confidence": 0.95},
    },
    "P-1002": {
        "title": "男装·工装直筒裤", "category": "男装", "images": 2,
        "colors": [{"color": "藏蓝", "color_hex": "#4A6B8A"}],
        "sku_ids": ["ST-1002-C1-M", "ST-1002-C1-L"],
        "attrs": {"product_type": "工装直筒裤", "category": "男装",
                  "shape": "直筒、高腰、双侧贴袋", "color_hex": "#4A6B8A",
                  "color_names": ["藏蓝"], "logo_text": "无",
                  "materials": "棉 98%、弹力纤维 2%",
                  "key_features": ["侧边贴袋", "可调节裤脚"],
                  "must_not_change": ["颜色", "版型", "口袋位置", "裤脚结构"],
                  "source": "platform", "confidence": 0.93},
    },
    "P-2001": {
        "title": "童装·卫衣（加绒）", "category": "童装", "images": 4,
        "colors": [{"color": "草绿", "color_hex": "#3F7A5E"}],
        "sku_ids": ["ST-2001-C1-120", "ST-2001-C1-130"],
        "attrs": {"product_type": "加绒卫衣", "category": "童装",
                  "shape": "宽松套头、落肩", "color_hex": "#3F7A5E",
                  "color_names": ["草绿"], "logo_text": "胸前小熊图案",
                  "materials": "棉 80%、聚酯纤维 20%",
                  "key_features": ["加绒内里", "罗纹下摆"],
                  "must_not_change": ["颜色", "版型", "图案"],
                  "source": "platform", "confidence": 0.9},
    },
    "P-3001": {
        "title": "女装·半身裙（长款）", "category": "女装", "images": 2,
        "colors": [{"color": "黑色", "color_hex": "#111111"}],
        "sku_ids": ["ST-3001-C1-S", "ST-3001-C1-M"],
        "attrs": {"product_type": "半身裙（长款）", "category": "女装",
                  "shape": "A 字、中长、侧隐形拉链", "color_hex": "#111111",
                  "color_names": ["黑色"], "logo_text": "无",
                  "materials": "聚酯纤维 100%",
                  "key_features": ["侧隐形拉链", "内衬"],
                  "must_not_change": ["颜色", "版型", "长度"],
                  "source": "platform", "confidence": 0.91},
    },
    "P-3002": {
        "title": "男装·冲锋衣（三合一）", "category": "男装", "images": 3,
        "colors": [{"color": "正红", "color_hex": "#C0392B"}, {"color": "黑色", "color_hex": "#111111"}],
        "sku_ids": ["ST-3002-C1-M", "ST-3002-C1-XL"],
        "attrs": {"product_type": "冲锋衣（三合一）", "category": "男装",
                  "shape": "连帽、收腰抽绳、可拆内胆", "color_hex": "#C0392B",
                  "color_names": ["正红"], "logo_text": "左胸品牌标",
                  "materials": "防水涂层尼龙 100%",
                  "key_features": ["可拆内胆", "防水拉链"],
                  "must_not_change": ["颜色", "版型", "拉链位置", "logo"],
                  "source": "platform", "confidence": 0.92},
    },
    "P-3003": {
        "title": "童装·连衣裙", "category": "童装", "images": 2,
        "colors": [{"color": "雾蓝", "color_hex": "#2C5D8F"}],
        "sku_ids": ["ST-3003-C1-110"],
        "attrs": {"product_type": "连衣裙", "category": "童装",
                  "shape": "A 字、圆领、后隐形拉链", "color_hex": "#2C5D8F",
                  "color_names": ["雾蓝"], "logo_text": "无",
                  "materials": "棉 100%",
                  "key_features": ["后隐形拉链", "荷叶袖"],
                  "must_not_change": ["颜色", "版型", "图案"],
                  "source": "platform", "confidence": 0.9},
    },
    # 演示错误路径：商品存在但没有可用参考图
    "P-4001": {"title": "童装·背带裤", "category": "童装", "images": 0,
               "sku_ids": [], "attrs": {}},
    # 演示错误路径：商品存在、也有图片记录，但取图会失败（坏图）
    "P-4002": {"title": "女装·羽绒马甲", "category": "女装", "images": 1,
               "sku_ids": [], "attrs": {}, "broken_image": True},
}

# ---------------------------------------------------------------- 预设（可入库热更新）
PRESETS: tuple[dict, ...] = (
    {"preset_id": "C-FULL-FRONT-TOP", "kind": "composition", "code": "full_front_top",
     "label": "正面全身像·上装突出", "desc": "全身入镜，重心在上装",
     "prompt_hint": "正面站立全身像，上装占画面主体", "sort": 1},
    {"preset_id": "C-SIDE-LOWER", "kind": "composition", "code": "side_lower",
     "label": "侧身像·下装突出", "desc": "侧身，重心在下装",
     "prompt_hint": "侧身站立，下装占画面主体", "sort": 2},
    {"preset_id": "C-HALF-FRONT", "kind": "composition", "code": "half_front",
     "label": "正面半身", "desc": "腰部以上近景",
     "prompt_hint": "正面半身近景，突出面料与领口", "sort": 3},
    {"preset_id": "C-DETAIL-MACRO", "kind": "composition", "code": "detail_macro",
     "label": "细节特写", "desc": "面料/做工微距",
     "prompt_hint": "微距特写：面料纹理与走线", "sort": 4},
    {"preset_id": "C-FLAT-LAY", "kind": "composition", "code": "flat_lay",
     "label": "平铺挂拍", "desc": "平铺或挂拍",
     "prompt_hint": "平铺俯拍，商品铺满画面", "sort": 5},
    {"preset_id": "S-STUDIO-FORMAL", "kind": "scene_style", "code": "studio_formal",
     "label": "影棚正式风", "desc": "灰白影棚背景 + 柔和棚拍光",
     "prompt_hint": "影棚拍摄，灰白无缝背景，柔和定向光", "sort": 1},
    {"preset_id": "S-STREET-FRESH", "kind": "scene_style", "code": "street_fresh",
     "label": "街拍清新风", "desc": "街头自然光 + 生活化背景",
     "prompt_hint": "城市街头自然光，环境虚化，清新色调", "sort": 2},
    {"preset_id": "S-FACTORY-PRACTICAL", "kind": "scene_style", "code": "factory_practical",
     "label": "工厂实用风", "desc": "车间/仓库真实场景",
     "prompt_hint": "工厂仓库实景，实用主义构图", "sort": 3},
    {"preset_id": "S-WHITE-E-COM", "kind": "scene_style", "code": "white_ecom",
     "label": "白底电商风", "desc": "纯白背景，无道具",
     "prompt_hint": "纯白背景，无道具，标准电商主图", "sort": 4},
    {"preset_id": "S-HOME-COZY", "kind": "scene_style", "code": "home_cozy",
     "label": "家居温馨风", "desc": "室内暖光居家场景",
     "prompt_hint": "室内居家场景，暖色自然光", "sort": 5},
)

# 9 图位框架（服装版）：type=generate 走 AI 出图，layout / photo_required 不走出图通道
SLOTS: tuple[dict, ...] = (
    {"slot": 1, "role": "主图", "type": "generate",
     "composition_id": "C-FULL-FRONT-TOP", "scene_style_id": "S-WHITE-E-COM"},
    {"slot": 2, "role": "上装卖点", "type": "generate",
     "composition_id": "C-HALF-FRONT", "scene_style_id": "S-STUDIO-FORMAL"},
    {"slot": 3, "role": "下装卖点", "type": "generate",
     "composition_id": "C-SIDE-LOWER", "scene_style_id": "S-STUDIO-FORMAL"},
    {"slot": 4, "role": "场景 A", "type": "generate",
     "composition_id": "C-FULL-FRONT-TOP", "scene_style_id": "S-STREET-FRESH"},
    {"slot": 5, "role": "场景 B", "type": "generate",
     "composition_id": "C-SIDE-LOWER", "scene_style_id": "S-STREET-FRESH"},
    {"slot": 6, "role": "细节特写", "type": "generate",
     "composition_id": "C-DETAIL-MACRO", "scene_style_id": "S-STUDIO-FORMAL"},
    {"slot": 7, "role": "尺码对照", "type": "layout",
     "composition_id": "C-FLAT-LAY", "scene_style_id": "S-WHITE-E-COM"},
    {"slot": 8, "role": "配件/包装清单", "type": "layout",
     "composition_id": "C-FLAT-LAY", "scene_style_id": "S-WHITE-E-COM"},
    {"slot": 9, "role": "售后/退换", "type": "photo_required"},
)

PLAN_POLICY: dict[str, dict] = {
    "default": {"default_count": 6, "max_count": 9,
                "default_composition_id": "C-FULL-FRONT-TOP",
                "default_scene_style_id": "S-WHITE-E-COM"},
}

PLATFORM_RULES: dict[str, dict] = {
    "淘系": {"platform": "淘系", "aspect": "1:1", "min_px": 800, "max_mb": 3.0,
             "background": "纯白", "text_allowed": True,
             "forbidden_words": ["全网最低价", "国家级", "绝对"],
             "notes": "主图 1:1 白底，不得拼接与水印"},
    "通用": {"platform": "通用", "aspect": "1:1", "min_px": 800, "max_mb": 5.0,
             "background": "纯白或浅色", "text_allowed": True,
             "forbidden_words": ["伪造", "官方认证"],
             "notes": "通用电商主图规范"},
}


def list_products(keyword: str = "", limit: int = 20) -> list[dict]:
    rows = [{
        "product_id": product_id, "title": spec["title"], "category": spec["category"],
        "main_image_id": f"{product_id}-img-1" if spec["images"] else "",
        "updated_at": "2026-11-17T09:00:00+08:00",
        "sku_ids": list(spec.get("sku_ids") or []),
    } for product_id, spec in PRODUCTS.items()]
    if keyword:
        rows = [row for row in rows if keyword in row["title"] or keyword in row["product_id"]]
    return rows[: max(1, int(limit))]


def get_product(product_id: str) -> dict | None:
    spec = PRODUCTS.get((product_id or "").strip())
    if spec is None:
        return None
    colors = list(spec.get("colors") or [])
    images = []
    total = int(spec.get("images") or 0)
    for index in range(total):
        # 颜色维度：每个颜色一张主图，其余图挂到第一个颜色上做细节图
        color = colors[index] if index < len(colors) else (colors[0] if colors else {})
        images.append({
            "image_id": f"{product_id}-img-{index + 1}",
            "url": f"/api/v1/image/assets/{product_id}-img-{index + 1}.png",
            "mime": "image/png", "is_main": index < max(1, len(colors)),
            "width": 640, "height": 640,
            "color": color.get("color", ""), "color_hex": color.get("color_hex", ""),
        })
    skus = [{"sku_id": sku_id, "color": color.get("color", ""),
             "color_hex": color.get("color_hex", ""), "size": ""}
            for sku_id, color in zip(spec.get("sku_ids") or [], colors, strict=False)]
    return {"product_id": product_id, "title": spec["title"], "category": spec["category"],
            "attrs": dict(spec.get("attrs") or {}), "images": images,
            "skus": skus,
            "sku_ids": list(spec.get("sku_ids") or []),
            "broken_image": bool(spec.get("broken_image"))}


def image_bytes(image_id: str) -> bytes | None:
    """生成商品图字节；`broken_image` 商品返回 None（模拟坏图/取图失败）。"""
    for product_id, spec in PRODUCTS.items():
        if not image_id.startswith(f"{product_id}-img-"):
            continue
        if spec.get("broken_image"):
            return None
        return placeholder_png(image_id, 640, 640)
    return None


def image_b64(image_id: str) -> str:
    raw = image_bytes(image_id)
    return base64.b64encode(raw).decode("ascii") if raw else ""


def seed_catalog_rows(session) -> int:
    """把 `PRESETS` 播种进 `image_catalog`（幂等：已存在的 preset_id 不覆盖，便于人工改库热更新）。"""
    from data_platform.db import ImageCatalog

    existing = {row.preset_id for row in session.query(ImageCatalog).all()}
    created = 0
    for spec in PRESETS:
        if spec["preset_id"] in existing:
            continue
        session.add(ImageCatalog(
            preset_id=spec["preset_id"], kind=spec["kind"], code=spec["code"],
            label=spec["label"], desc=spec["desc"], attrs_json=spec,
            enabled=True, updated_at=dt.datetime.now(),
        ))
        created += 1
    if created:
        session.flush()
    return created


__all__ = ["PRODUCTS", "PRESETS", "SLOTS", "PLAN_POLICY", "PLATFORM_RULES",
           "list_products", "get_product", "image_bytes", "image_b64", "seed_catalog_rows"]
