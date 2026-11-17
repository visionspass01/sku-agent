"""Reflexion：把 Critic 的问题翻译成**只改对应图位**的 Prompt 补丁（不整单重跑）。

原则：
- 一个 issue 一条补丁，补丁是「加到该图位 Prompt 末尾的修复指令」；
- 优先复用 `image_memory` 里同签名历史修复（Agentic-RAG 味道），没有就走内置规则；
- 最多 `IMAGE_REFLEXION_MAX_ROUNDS` 轮，超限直接判失败并归档，不无限重试。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agents_image.state import CriticResult, ImageTaskState, PromptDraft

# issue 前缀 → 修复指令（内置规则；历史经验优先）
_PATCH_RULES: dict[str, str] = {
    "identity": ("严格保持参考图中的颜色（写 hex）、版型、图案、logo 文字与关键部件位置不变；"
                 "把上述项原样写进否定清单"),
    "aspect": "严格按请求画幅比例生成，主体居中，不得裁掉商品边缘",
    "banned_words": "删除任何平台禁用表述，改用中性描述",
    "text_policy": "本图不生成任何文字、角标与水印",
    "mime": "输出标准 PNG 图片",
    "empty": "确保输出一张完整、非空的商品图",
    "prompt": "补齐五槽位 Prompt（产品身份/场景构图/光影/技术参数/否定清单）",
}


@dataclass
class Reflection:
    patches: dict[int, list[str]] = field(default_factory=dict)   # slot → 修复指令
    summary: list[str] = field(default_factory=list)

    def as_lines(self) -> list[str]:
        return list(self.summary)

    def merge(self, other: "Reflection") -> "Reflection":
        for slot, items in other.patches.items():
            existing = self.patches.setdefault(slot, [])
            for item in items:
                if item not in existing:
                    existing.append(item)
        for line in other.summary:
            if line not in self.summary:
                self.summary.append(line)
        return self

    def slots(self) -> list[int]:
        return sorted(self.patches)


def _slot_of(issue: str) -> int:
    """从 `identity: 图位 2 丢失…` 里取图位号；取不到就返回 0（表示全体）。"""
    for token in issue.replace("：", " ").replace(":", " ").split():
        if token.isdigit():
            return int(token)
    return 0


def reflect(state: ImageTaskState, critic: CriticResult, drafts: list[PromptDraft],
            *, rules=None, memory: dict[str, str] | None = None) -> Reflection:
    """把 critic.issues 翻成按图位的补丁；`memory` 是同签名的历史修复（有则优先）。"""
    reflection = Reflection()
    for issue in critic.issues or []:
        kind = issue.split(":", 1)[0].strip()
        slot = _slot_of(issue)
        patch = (memory or {}).get(kind) or _PATCH_RULES.get(kind)
        if not patch:
            continue
        targets = [slot] if slot else [draft.slot for draft in drafts]
        for target in targets:
            items = reflection.patches.setdefault(target, [])
            if patch not in items:
                items.append(patch)
        line = f"图位 {slot or '全部'}：{patch}"
        if line not in reflection.summary:
            reflection.summary.append(line)
    if reflection.patches:
        state.reflections.extend(reflection.as_lines())
    return reflection


def apply_patches(drafts: list[PromptDraft], reflection: Reflection) -> list[PromptDraft]:
    """生成新的 PromptDraft 列表（不改原对象，便于对比前后版本）。"""
    patched: list[PromptDraft] = []
    for draft in drafts:
        extra = reflection.patches.get(draft.slot) or []
        if not extra:
            patched.append(draft.model_copy(deep=True))
            continue
        clone = draft.model_copy(deep=True)
        clone.prompt = f"{clone.prompt}\n[修复要求] {'；'.join(extra)}"
        negatives = [item for item in (clone.negative_prompt or "").split("、") if item]
        for item in extra:
            if "否定清单" in item:
                continue
            if item not in negatives:
                negatives.append(item)
        clone.negative_prompt = "、".join(negatives)
        patched.append(clone)
    return patched


__all__ = ["Reflection", "reflect", "apply_patches", "_PATCH_RULES"]
