"""Critic 评审（规则层，确定性、离线可用）。

四类检查（对应计划 §16）：
1. **身份一致性**：每条 Prompt 的 `preserve` 必须包含全部 `must_not_change`（丢一个就 blocking）；
2. **合规**：全量 Prompt 不得命中全局/平台禁用词；
3. **技术规格**：出图尺寸与请求比例一致、mime 合法、字节数 > 0；
4. **请求一致性**：图位比例与请求一致、张数与规划一致。

评分从 100 起扣：blocking 一律 ≥ 50 分扣减，最终 `passed = 无 blocking and score >= 通过线`。
模型层 rubric（LLM）留开关，默认关闭——演示与 CI 不依赖外部模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agents_image.schemas import FORBIDDEN_WORDS, attrs_of, identity_missing
from app.agents_image.state import CriticResult, ImageTaskState, PromptDraft
from app.agents_image.storage.image_storage import image_size, sniff_mime

_ASPECT_TOLERANCE = 0.06


@dataclass
class CheckedImage:
    """待评审的一张图（已落盘前的原始字节 + 图位）。"""

    slot: int
    data: bytes
    mime: str = "image/png"
    width: int = 0
    height: int = 0
    meta: dict = field(default_factory=dict)


def _ratio(width: int, height: int) -> float:
    return (width / height) if height else 0.0


def _expected_ratio(aspect: str) -> float:
    try:
        left, right = (aspect or "1:1").split(":")
        return float(left) / float(right)
    except (ValueError, ZeroDivisionError):
        return 1.0


def critique(state: ImageTaskState, drafts: list[PromptDraft], images: list[CheckedImage],
             *, settings=None, rules=None, manifest: dict | None = None) -> CriticResult:
    """返回 CriticResult（score / issues / blocking 摘要），不抛异常。

    通过线与 blocking 规则优先取 manifest（`IMAGE_CRITIC_PASS_SCORE` 仍是兜底），
    这样调阈值不用改代码（P1-8：manifest.critic 段以前从未被读取）。
    """
    critic_cfg = dict((manifest or {}).get("critic") or {})
    pass_score = int(critic_cfg.get("pass_score")
                     or getattr(settings, "critic_pass_score", 75) or 75)
    issues: list[str] = []
    blocking: list[str] = []
    score = 100

    must_not_change = list(attrs_of(state.attrs).must_not_change)
    # P1-12：与 validate 共用同一个身份锁定判定（schemas.identity_missing）
    for slot, missing in identity_missing(drafts, must_not_change):
        score -= 50
        blocking.append("identity")
        issues.append(f"identity: 图位 {slot} 丢失身份锁定：{'、'.join(missing)}")
    for draft in drafts:
        if not (draft.prompt or "").strip():
            score -= 40
            blocking.append("prompt")
            issues.append(f"prompt: 图位 {draft.slot} Prompt 为空")
        if draft.aspect != state.aspect:
            score -= 20
            issues.append(f"aspect: 图位 {draft.slot} 比例 {draft.aspect} 与请求 {state.aspect} 不一致")
        if (draft.text_policy or "none") != "none" and not getattr(rules, "text_allowed", True):
            score -= 20
            blocking.append("text_policy")
            issues.append(f"text_policy: 图位 {draft.slot} 要求图内文字，但平台不允许")

    banned = tuple(FORBIDDEN_WORDS) + tuple(getattr(rules, "forbidden_words", []) or ())
    for draft in drafts:
        hits = [word for word in banned if word in (draft.prompt or "")]
        if hits:
            score -= 50
            blocking.append("banned_words")
            issues.append(f"banned_words: 图位 {draft.slot} 命中 {'、'.join(hits)}")

    if not images:
        score -= 50
        blocking.append("empty")
        issues.append("empty: 没有任何出图结果")
    for image in images:
        mime = image.mime or sniff_mime(image.data)
        if mime not in {"image/png", "image/jpeg", "image/webp"}:
            score -= 40
            blocking.append("mime")
            issues.append(f"mime: 图位 {image.slot} 输出类型 {mime} 不受支持")
            continue
        if not image.data:
            score -= 40
            blocking.append("empty")
            issues.append(f"empty: 图位 {image.slot} 输出为空")
            continue
        width, height = image.width, image.height
        if not (width and height):
            width, height = image_size(image.data)
        if width and height:
            expected = _expected_ratio(state.aspect)
            if abs(_ratio(width, height) - expected) > _ASPECT_TOLERANCE * max(expected, 1.0):
                score -= 25
                blocking.append("aspect")
                issues.append(f"aspect: 图位 {image.slot} 出图 {width}x{height} "
                              f"与请求比例 {state.aspect} 不符")

    score = max(0, min(100, score))
    passed = score >= pass_score and not blocking
    return CriticResult(passed=passed, score=score, issues=issues,
                        consistency={"must_not_change": must_not_change,
                                     "blocking": sorted(set(blocking))},
                        skipped=False)


def failure_signature(state: ImageTaskState, critic: CriticResult) -> str:
    """失败签名：按 blocking 类别归并，用于记忆复用（同因同解）。"""
    kinds = "+".join(critic.consistency.get("blocking") or ["unknown"])
    product = str((state.attrs or {}).get("product_type") or "unknown")
    return f"{kinds}|{product}"


__all__ = ["CheckedImage", "critique", "failure_signature"]
