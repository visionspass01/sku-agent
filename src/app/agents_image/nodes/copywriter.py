"""图内文案生成（M5）：**确定性、离线、可审查**，不调 LLM。

参考仓库 `04_Product_Image_Agent` 的做法是"按图位给出文案 + 字数表"，且文案不是广告语：
写信息（材质/版型/卖点数字），不写情绪；数值必须与商品数据一致；一句只讲一件事。

规则：
- 平台 `copy_policy.headline_allowed=False`（抖系主图）→ 一律无字；
- 每图位有字数上限（`copy_table`，缺省按角色给默认值）；
- 文案只从**中台给的商品属性**里取（`key_features` / `materials` / `color_names` / 版型），
  拼不出合格文案就降级为无字并留 warning —— 绝不编造参数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agents_image.schemas import FORBIDDEN_WORDS, ClothingAttrs

# 角色 → (headline 上限词数, subhead 上限词数)；中台 `copy_table` 可按角色覆盖
_DEFAULT_LIMITS: dict[str, tuple[int, int]] = {
    "主图": (4, 7),
    "上装卖点": (4, 7),
    "下装卖点": (4, 7),
    "场景 A": (4, 7),
    "场景 B": (4, 7),
    "细节特写": (4, 7),
}
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass
class SlotCopy:
    """一个图位的图内文字（`allowed=False` 时其余字段应为空）。"""

    allowed: bool = False
    headline: str = ""
    subhead: str = ""
    badge: str = ""
    char_count: int = 0
    limit: int = 0
    source: str = "auto"
    notes: str = ""
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "headline": self.headline,
                "subhead": self.subhead, "badge": self.badge,
                "char_count": self.char_count, "limit": self.limit,
                "source": self.source, "notes": self.notes,
                "warnings": list(self.warnings)}


def _words(text: str) -> list[str]:
    return [part for part in re.split(r"[\s,，、·/]+", text or "") if part]


def _clip_words(text: str, limit: int) -> str:
    words = _words(text)
    return " ".join(words[:limit]) if words else ""


def _clean(text: str) -> str:
    out = text or ""
    for word in FORBIDDEN_WORDS:
        out = out.replace(word, "")
    return out.strip(" ，,、")


def build_copy(attrs: ClothingAttrs, role: str, *, platform_rules=None,
               copy_table: dict | None = None, direction: str = "") -> SlotCopy:
    """为某个图位生成图内文案；不可用时返回 `allowed=False` 的降级结果。"""
    table = dict(copy_table or {})
    role_limit = table.get(role) or {}
    max_headline = int(role_limit.get("max_headline_words", 4) or 4)
    max_subhead = int(role_limit.get("max_subhead_words", 7) or 7)
    if platform_rules is not None:
        max_headline = min(max_headline, int(getattr(platform_rules, "max_headline_words", 4)))
        max_subhead = min(max_subhead, int(getattr(platform_rules, "max_subhead_words", 7)))
        if not getattr(platform_rules, "headline_allowed", True):
            return SlotCopy(allowed=False, limit=0, source="platform",
                            notes="平台规则禁止主图营销文字（如抖系），本图不生成文字")

    features = [_clean(item) for item in (attrs.key_features or []) if _clean(item)]
    headline_source = direction or (features[0] if features else "")
    headline = _clip_words(headline_source, max_headline)
    sub_parts = [part for part in (
        _clean("/".join(attrs.color_names)),
        _clean(attrs.materials),
        _clean(attrs.shape),
    ) if part]
    subhead = _clip_words("、".join(sub_parts), max_subhead)
    # 角标只放数字（铁律⑥：角标仅数字），从材质/特征里挑一个数字
    badge = ""
    for text in (attrs.materials, *features):
        match = _NUMBER_RE.search(text or "")
        if match:
            badge = match.group(0)
            break
    if not headline and not subhead:
        return SlotCopy(allowed=False, limit=0, source="auto",
                        notes="商品数据不足，无法生成可读文案 → 降级为无字")

    char_count = len(headline) + len(subhead) + len(badge)
    limit = max(12, max_headline * 4 + max_subhead * 3)
    warnings: list[str] = []
    if char_count > limit:
        warnings.append(f"IMG_COPY_TOO_LONG: 文案 {char_count} 字超过上限 {limit}，已截断")
        headline = _clip_words(headline, max_headline)
        subhead = _clip_words(subhead, max_subhead)
        char_count = len(headline) + len(subhead) + len(badge)
    return SlotCopy(allowed=True, headline=headline, subhead=subhead, badge=badge,
                    char_count=char_count, limit=limit, source="auto",
                    notes=f"{role}：主标语 ≤{max_headline} 词 / 副文案 ≤{max_subhead} 词 / 角标仅数字",
                    warnings=warnings)


def describe(copy: SlotCopy) -> str:
    """把文案渲染成 Prompt 里的 `[图内文案]` 块。"""
    if not copy.allowed:
        return "本图不生成任何文字（无水印、无角标、无价格标签）"
    parts = []
    if copy.headline:
        parts.append(f"主标语「{copy.headline}」")
    if copy.subhead:
        parts.append(f"副文案「{copy.subhead}」")
    if copy.badge:
        parts.append(f"角标数字「{copy.badge}」")
    return ("；".join(parts) + f"；总字数 {copy.char_count}/{copy.limit}，"
            "文字放留白区、不压商品主体与图案") if parts else "本图不生成任何文字"


__all__ = ["SlotCopy", "build_copy", "describe"]
