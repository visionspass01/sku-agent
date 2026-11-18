"""图位规划与 Prompt 组装（D8–D9：9 图位 + 五槽位 + 七条铁律）。

边界（v6 起）：
- **图位模板、默认构图、默认场景风格一律来自中台**（`plan_policy` / `list_*`，经 MCP）；
  本模块不再硬编码"正面全身像·上装突出""白底电商风"这类默认值；
- 建任务时就把「图位 + 构图/场景 label + 是否出图」写进 `slots_json`，执行期（`graph`）
  不再回调中台 —— 任务保持无状态、可重放、可对比；
- `type != "generate"` 的图位（尺码对照 / 包装清单 / 售后排版）**不走 AI 出图通道**，
  只登记规划，不产生图片、不计入交付张数。

七条铁律（写进每条 Prompt，不是只写在文档里）：
① 颜色用 hex；② 商品占比数字化；③ 留白显式声明；④ 否定清单必含 `must_not_change`；
⑤ 主图预留平台角标区；⑥ 图内文字三层且受平台规则约束；⑦ 一次批量出图优于反复调参。
"""

from __future__ import annotations

from app.agents_image.schemas import attrs_of, mask_banned, merge_preserve
from app.agents_image.state import ImageTaskState, PromptDraft

# 图位角色 → 占比/留白/图内文字策略（可被 manifest.slot_rules 覆盖）
_DEFAULT_SLOT_RULES: dict[str, dict] = {
    "主图": {"fill": 70, "margin": 8, "text": "none", "reserve": True},
    "上装卖点": {"fill": 68, "margin": 10, "text": "headline", "reserve": False},
    "下装卖点": {"fill": 68, "margin": 10, "text": "headline", "reserve": False},
    "场景 A": {"fill": 55, "margin": 12, "text": "none", "reserve": False},
    "场景 B": {"fill": 55, "margin": 12, "text": "none", "reserve": False},
    "细节特写": {"fill": 80, "margin": 6, "text": "none", "reserve": False},
    "尺码对照": {"fill": 85, "margin": 5, "text": "spec", "reserve": False},
    "配件/包装清单": {"fill": 80, "margin": 8, "text": "spec", "reserve": False},
    "售后/退换": {"fill": 85, "margin": 5, "text": "spec", "reserve": False},
}
_FALLBACK_RULE = {"fill": 70, "margin": 8, "text": "none", "reserve": False}
_TEXT_BLOCK = {
    "none": "本图不生成任何文字（无水印、无角标、无价格标签）",
    "headline": "图内文字最多两层：主标语 ≤4 词、卖点 ≤7 词；不得出现价格与促销词",
    "spec": "图内文字仅允许数字与规格（尺码 cm、面料成分、清单条目），不得出现主观卖点词",
}


def is_generatable(slot) -> bool:
    """只有 `type=generate` 的图位走 AI 出图（其余是排版/实拍）。"""
    value = slot.get("type", "generate") if isinstance(slot, dict) else getattr(slot, "type", "")
    return (value or "generate") == "generate"


def generate_slots(slots: list) -> list[dict]:
    """筛出出图图位，统一成 dict（graph 用它决定实际生成几张）。"""
    out: list[dict] = []
    for item in slots or []:
        if not is_generatable(item):
            continue
        out.append(dict(item) if isinstance(item, dict) else item.model_dump())
    return out


def build_slots(state: ImageTaskState, *, policy=None, labels: dict | None = None,
                slot_count: int | None = None) -> list[dict]:
    """按中台 `plan_policy` 生成图位；`slot_count` 未给时退到 `state.count`（M3 起两者分离）。"""
    count = max(1, min(int(slot_count if slot_count is not None else (state.count or 1)), 9))
    labels = labels or {}
    composition_labels = labels.get("compositions") or {}
    style_labels = labels.get("scene_styles") or {}
    specs = list(getattr(policy, "slots", []) or [])
    user_composition = (state.compositions[0] if state.compositions else "")
    user_style = (state.scene_styles[0] if state.scene_styles else "")

    slots: list[dict] = []
    for index in range(1, count + 1):
        spec = next((item for item in specs if int(getattr(item, "slot", 0)) == index), None)
        composition_id = user_composition or getattr(spec, "composition_id", "") or \
            getattr(policy, "default_composition_id", "")
        style_id = user_style or getattr(spec, "scene_style_id", "") or \
            getattr(policy, "default_scene_style_id", "")
        role = getattr(spec, "role", "") or f"图位 {index}"
        slots.append({
            "slot": index,
            "role": role,
            "type": getattr(spec, "type", "") or "generate",
            "composition_id": composition_id,
            "composition_label": composition_labels.get(composition_id, "")
            or (getattr(policy, "default_composition_label", "")
                if composition_id == getattr(policy, "default_composition_id", "") else ""),
            "scene_style_id": style_id,
            "scene_style_label": style_labels.get(style_id, "")
            or (getattr(policy, "default_scene_style_label", "")
                if style_id == getattr(policy, "default_scene_style_id", "") else ""),
        })
    return slots


def slot_rule(slot, role: str, manifest: dict | None = None) -> dict:
    """图位规则：manifest 里按图位号或角色可覆盖默认值（改配置即生效）。"""
    rules = ((manifest or {}).get("slot_rules") or {})
    by_slot = rules.get(str(slot)) or {}
    by_role = rules.get(role) or {}
    rule = dict(_DEFAULT_SLOT_RULES.get(role, _FALLBACK_RULE))
    rule.update(by_role)
    rule.update(by_slot)
    return rule


def compose(state: ImageTaskState, registry, *, rules=None,
            fragments: list[str] | dict[int, list[str]] | None = None) -> list[PromptDraft]:
    """把「身份锁定 + 图位 + 构图/场景（中台 label）+ 平台规则 + RAG 片段」套进五槽位模板。"""
    attrs = attrs_of(state.attrs)
    must_not_change = list(attrs.must_not_change)
    product_block = attrs.prompt_block()
    manifest = registry.manifest or {}
    default_background = manifest.get("default_background", "")
    width, height = _aspect_size(state.aspect)
    platform_rules = rules
    text_allowed = bool(getattr(platform_rules, "text_allowed", True))
    extra_forbidden = list(getattr(platform_rules, "forbidden_words", []) or [])
    max_mb = getattr(platform_rules, "max_mb", 3.0)
    background_rule = getattr(platform_rules, "background", "") or default_background

    drafts: list[PromptDraft] = []
    for item in state.slots or build_slots(state):
        slot_no = int(_field(item, "slot", 1) or 1)
        role = _field(item, "role", "主图")
        composition = _field(item, "composition_label", "") or _first(state.compositions)
        scene_style = _field(item, "scene_style_label", "") or _first(state.scene_styles)
        rule = slot_rule(slot_no, role, manifest)
        text_key = rule.get("text", "none")
        if not text_allowed:
            text_key = "none"
        text_block = _TEXT_BLOCK.get(text_key, _TEXT_BLOCK["none"])
        background = ("纯白" if ("白底" in scene_style or "白底" in background_rule)
                      else (background_rule or "与场景风格一致"))
        reserve_block = ("左上角约 8% 面积留白，不放置任何关键信息（留给平台角标；主图硬性要求）"
                         if rule.get("reserve") else "四周留白均匀，不留平台角标位")
        rag_items = _fragments_of(fragments, slot_no)
        # 知识库片段本身会提到禁用词（"不得使用国家级"），注入前先 masking，
        # 既不违规也不丢语义；落库的 fragments 保留原文便于审计。
        rag_block = "；".join(mask_banned(item, extra_forbidden) for item in rag_items if item)
        # 禁用词只按"规则卡列出 N 项"表述，绝不把词本身写进 Prompt——
        # 否则评审阶段会把它们当成命中禁用词（自伤）。
        banned = (f"平台规则卡所列禁止词（{len(extra_forbidden)} 项，出现即违规）"
                  if extra_forbidden else "无")

        text, _version = registry.prompt(manifest["generator_prompt"], {
            "product_block": product_block,
            "scene_block": (f"{composition}；场景风格：{scene_style}；"
                            f"商品占画面 {rule['fill']}%，四周留白 {rule['margin']}%"
                            if (composition or scene_style)
                            else f"商品占画面 {rule['fill']}%，四周留白 {rule['margin']}%"),
            "light_block": "柔和棚拍光，方向自左上，sRGB，颜色准确",
            "aspect": state.aspect,
            "width": width,
            "height": height,
            "background": background,
            "text_block": text_block,
            "reserve_block": reserve_block,
            "rag_block": rag_block,
            "must_not_change": "、".join(must_not_change),
            "slot": slot_no,
            "role": role,
            "task_prompt": state.prompt,
            "max_mb": max_mb,
            "banned": banned,
        })
        drafts.append(PromptDraft(
            slot=slot_no, role=role,
            prompt=text,
            negative_prompt=("文字水印、边框、拼图、多余物件、改变款式与颜色"
                             + ("、平台规则卡禁止词" if extra_forbidden else "")),
            aspect=state.aspect, width=width, height=height,
            preserve=merge_preserve(None, must_not_change),
            type=_field(item, "type", "generate"),
            fill_percent=int(rule["fill"]), margin_percent=int(rule["margin"]),
            text_policy=text_key, fragments=list(rag_items),
        ))
    return drafts


def _fragments_of(fragments, slot_no: int) -> list[str]:
    """RAG 片段可以是「全体共用」或「按图位给」两种形态。"""
    if isinstance(fragments, dict):
        return [str(item) for item in (fragments.get(slot_no) or []) if item]
    return [str(item) for item in (fragments or []) if item]


def _field(item, name: str, default):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _first(values: list[str]) -> str:
    return values[0] if values else ""


def _aspect_size(aspect: str) -> tuple[int, int]:
    return {"1:1": (1024, 1024), "3:4": (768, 1024), "4:3": (1024, 768),
            "16:9": (1024, 576)}.get(aspect, (1024, 1024))


__all__ = ["build_slots", "compose", "generate_slots", "is_generatable", "slot_rule"]
