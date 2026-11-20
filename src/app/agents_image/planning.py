"""建任务期的参数规划（P2-8，第九轮：从 `api/routes.py` 抽出的领域逻辑）。

为什么抽出来：这些函数原先长在 1300+ 行的 `api/routes.py` 单文件里，且 API 层直接
import 编排节点（`plan_slots` / `validate`）——HTTP 层与领域层耦合，规划口径散落各处、
难单测难复用。现在统一收在这里：

- `dedupe` / `platform_list` / `variant_matrix`：请求体 → 平台与变体矩阵；
- `resolve_request_parameters`：平台参数对齐中台（默认值/候选校验/labels）；
- `resolve_targets`：「选中商品 + 颜色」展开成出图目标（最小业务单位 = 商品+颜色）；
- `attrs_for_target`：按颜色锁定身份字段（hex 缺失时宁可报错，不静默沿用首色）；
- `plan_state`：把一个出图目标规划成 `ImageTaskState`（图位 + 身份锁定）；
- `validate_state`：建任务期校验（与执行期同一套规则）。
"""

from __future__ import annotations

from app.agents_image.nodes import plan_slots
from app.agents_image.nodes import validate as validate_node
from app.agents_image.sources import refs as refs_source
from app.agents_image.state import ImageTaskState, SlotPlan
from app.core.errors import ImageError


def dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


async def resolve_request_parameters(catalog, body: dict, settings) -> dict:
    """把请求参数对齐到中台：默认值、候选校验、labels 映射（全部来自 catalog）。

    M3 起**区分两个张数**：
    - `slot_count`：图位数（默认取中台 `plan_policy` 的 9 图位模板，可 1..9）；
    - `image_count`：真正出图的张数（只有 `type=generate` 的图位出图，默认=该平台 generate 图位数）。
    旧字段 `n` 兼容映射到 `image_count`。
    """
    platform = str(body.get("platform") or "淘系").strip()
    category = str(body.get("category") or "").strip()
    rules = await catalog.get_platform_rules(platform=platform)
    policy = await catalog.get_plan_policy(platform=platform, category=category)
    compositions = await catalog.list_compositions(category=category, platform=platform)
    scene_styles = await catalog.list_scene_styles(category=category, platform=platform)

    composition_ids = {item.preset_id for item in compositions}
    style_ids = {item.preset_id for item in scene_styles}
    chosen_compositions = dedupe([str(x) for x in (body.get("compositions") or [])])
    chosen_styles = dedupe([str(x) for x in (body.get("scene_styles") or [])])
    for item in chosen_compositions:
        if item not in composition_ids:
            raise ImageError("IMG_INPUT_INVALID", f"构图不在中台候选里：{item}")
    for item in chosen_styles:
        if item not in style_ids:
            raise ImageError("IMG_INPUT_INVALID", f"场景风格不在中台候选里：{item}")

    aspect = str(body.get("aspect") or rules.aspect or "1:1")
    max_slots = min(int(policy.max_count or 9), settings.max_total_images, 9)
    try:
        raw_n = body.get("n")
        # 兼容：老调用方只传 n（=图位数=出图张数），新调用方用 slot_count / image_count
        raw_slots = body.get("slot_count")
        slot_count = int(raw_slots if raw_slots is not None else
                         (raw_n if raw_n is not None else max_slots))
        raw_images = body.get("image_count")
        image_count = int(raw_images if raw_images is not None else
                          (raw_n if raw_n is not None else 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ImageError("IMG_INPUT_INVALID", "张数必须是整数") from exc
    slot_count = max(1, min(slot_count, max_slots))
    generate_slots = sum(1 for item in policy.slots
                         if int(item.slot) <= slot_count and (item.type or "generate") == "generate")
    generate_slots = max(1, generate_slots)
    if image_count <= 0:
        image_count = generate_slots              # 默认按该平台 generate 图位数量
    if image_count > generate_slots:
        raise ImageError("IMG_INPUT_INVALID",
                         f"出图张数 {image_count} 超过该平台可出图图位数 {generate_slots}"
                         "（尺码对照/包装清单/售后排版不走出图通道）")
    image_count = max(1, min(image_count, settings.max_total_images))

    return {
        "platform": platform,
        "category": category,
        "policy": policy,
        "rules": rules,
        "aspect": aspect,
        "slot_count": slot_count,
        "count": image_count,           # 兼容字段：v6 的 count 语义 = 出图张数
        "image_count": image_count,
        "compositions": chosen_compositions,
        "scene_styles": chosen_styles,
        "labels": {
            "compositions": {item.preset_id: item.label for item in compositions},
            "scene_styles": {item.preset_id: item.label for item in scene_styles},
        },
    }


async def resolve_targets(catalog, body: dict, product_ids: list[str],
                          products: dict | None = None) -> list[refs_source.RefTarget]:
    """把「选中商品 + 颜色」展开成出图目标（M3：出图最小单位 = 商品 + 颜色）。

    请求侧三种写法：
    - `all_colors: true`：每个商品的全部颜色都出图；
    - `product_colors: [{product_id, colors: ["米色", "黑色"]}]`：指定颜色；
    - 都不传：每个商品只出「第一个颜色」（没有颜色维度时就是商品主图）。
    """
    requested: dict[str, list[str]] = {}
    for item in body.get("product_colors") or []:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("product_id") or "").strip()
        if not product_id:
            continue
        requested[product_id] = dedupe([str(x) for x in (item.get("colors") or [])])
    all_colors = bool(body.get("all_colors"))

    targets: list[refs_source.RefTarget] = []
    for product_id in product_ids:
        data = (products or {}).get(product_id)
        if data is None:
            data = await catalog.get_product(product_id)
        available = data.colors()
        names = {item["color"] for item in available if item["color"]}
        if product_id in requested and requested[product_id]:
            unknown = [name for name in requested[product_id] if name not in names]
            if unknown:
                raise ImageError("IMG_INPUT_INVALID",
                                 f"商品 {product_id} 没有这些颜色：{'、'.join(unknown)}"
                                 f"（可选：{'、'.join(sorted(names)) or '无颜色维度'}）")
            chosen = [item for item in available if item["color"] in requested[product_id]]
        elif all_colors:
            chosen = available
        else:
            chosen = available[:1]
        for item in chosen:
            targets.append(refs_source.RefTarget(
                product_id=product_id, color=item.get("color", ""),
                color_hex=item.get("color_hex", "")))
    if not targets:
        raise ImageError("IMG_INPUT_INVALID", "没有可出图的目标（商品没有颜色/参考图）")
    return targets


def platform_list(body: dict) -> list[str]:
    """平台列表（M5）：单平台、多平台双套（platforms）或 platform_variant 开关。

    平台标识统一用 `淘系` / `抖系`；`通用` 只作为中台兜底规则，不作为出图平台。
    """
    explicit = [str(item).strip() for item in (body.get("platforms") or []) if str(item).strip()]
    single = str(body.get("platform") or "").strip()
    if body.get("platform_variant"):
        # W1：`platform_variant` 语义固定为"淘系+抖系双套"，不再被单平台字段降级成一套
        chosen = ["淘系", "抖系"]
    elif explicit:
        chosen = dedupe(explicit)
    elif single:
        chosen = [single]
    else:
        chosen = ["淘系"]
    allowed = {"淘系", "抖系"}
    unknown = [item for item in chosen if item not in allowed]
    if unknown:
        raise ImageError(
            "IMG_INPUT_INVALID",
            "不支持的平台：" + "、".join(unknown) + "（可选：" + "、".join(sorted(allowed)) + "）")
    return chosen


def variant_matrix(policy, body: dict, base_variant: str = "") -> list[dict]:
    """mode=standard/variant/iterate → 变体列表（variant 输出 A/B 两套差异化方案）。

    P1-3：iterate 必须**继承基座变体**——过去固定返回 `id=""`，而 A/B 变体基座的 key 是
    `(颜色, 平台, "A"/"B")`，两者永远对不上，于是"永不命中复用"且 `base_task_id` 血统丢失。
    """
    mode = str(body.get("mode") or "standard").strip().lower()
    if mode not in {"standard", "variant", "iterate"}:
        raise ImageError(
            "IMG_INPUT_INVALID",
            "不支持的 mode：" + mode + "（可选 standard / variant / iterate）")
    if mode == "iterate":
        return [{"id": str(base_variant or "").strip(), "label": "只改主图",
                 "hint": "重做主图；其余图位能复用就复用，缺失的图位按差额补齐"}]
    if mode != "variant":
        return [{"id": "", "label": "", "hint": ""}]
    directions = list(getattr(policy, "variant_directions", []) or [])
    if not directions:
        directions = [
            {"id": "A", "label": "版型优先", "hint": "主图突出版型与轮廓"},
            {"id": "B", "label": "面料优先", "hint": "主图突出面料质感与细节"},
        ]
    return [{"id": str(item.get("id") or ("V" + str(index + 1))),
             "label": str(item.get("label") or ""),
             "hint": str(item.get("hint") or "")}
            for index, item in enumerate(directions[:2])]


def attrs_for_target(base_attrs: dict, target) -> dict:
    """按颜色覆盖身份锁定字段：颜色必须锁到具体 hex，避免多色商品串色。

    W2：目标颜色**没有色值**时不能静默沿用商品首个颜色的 hex（会把米色画成黑色），
    必须报 `IMG_INPUT_INVALID` 让中台补齐；只有"商品本身只有这一个颜色、中台 attrs
    的色值就是它"时才允许沿用。
    """
    attrs = dict(base_attrs or {})
    if not target.color:
        return attrs
    known = [str(item) for item in (attrs.get("color_names") or [])]
    base_hex = str(attrs.get("color_hex") or "")
    attrs["color_names"] = [target.color]
    if target.color_hex:
        attrs["color_hex"] = target.color_hex
        return attrs
    if base_hex and known == [target.color]:
        return attrs          # 单色商品：中台 attrs 的色值就是这个颜色，可以沿用
    raise ImageError(
        "IMG_INPUT_INVALID",
        f"颜色「{target.color}」缺少色值（color_hex），无法锁定身份；请在中台补齐该颜色色值")


def plan_state(target, platform: str, params: dict, *, prompt: str, variant: dict,
               products: dict) -> ImageTaskState:
    """把一个出图目标规划成 state（图位 + 身份锁定），供建任务期预演/落库使用。"""
    state = ImageTaskState(
        prompt=prompt, aspect=params["aspect"], count=params["image_count"],
        compositions=params["compositions"], scene_styles=params["scene_styles"],
        platform=platform, variant_id=variant.get("id", ""),
        variant_direction=variant.get("hint", "") or variant.get("label", ""),
        refs=[],
    )
    state.slots = [SlotPlan(**item) for item in plan_slots.build_slots(
        state, policy=params["policy"], labels=params["labels"],
        slot_count=params["slot_count"])]
    state.attrs = attrs_for_target(products[target.product_id].attrs, target)
    return state


def validate_state(state: ImageTaskState, settings) -> None:
    """建任务期校验（与执行期同一套规则；API 层不再直接依赖编排节点）。"""
    validate_node.validate(state, settings=settings)


def is_generatable(slot) -> bool:
    """只有 `type=generate` 的图位走 AI 出图（尺码对照/包装清单/售后排版不生成图片）。"""
    return plan_slots.is_generatable(slot)


__all__ = ["dedupe", "resolve_request_parameters", "resolve_targets", "platform_list",
           "variant_matrix", "attrs_for_target", "plan_state", "validate_state",
           "is_generatable"]
