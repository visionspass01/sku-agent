"""交付前像素门禁（M5，第三轮 P0-3/P1-4 修复）：

- 阈值来自**中台平台规则卡**的 `compliance` 段（与 `scripts/check_image.py` 同源）；
- **比例基准 = 本次请求的 aspect**（过去用平台卡的默认 1:1，导致 3:4/16:9 请求必失败）；
- `IMAGE_COMPLIANCE_GATE=main|all|off`：默认只管主图（blocking），`all` 管所有 generate 图位；
- `IMAGE_COMPLIANCE_AUTOFIX=true` 时先归白 → 按请求比例裁 → 补足长边，再复检；
- `IMAGE_COMPLIANCE_NONPNG=warn|block`：解不了像素（非 PNG 且没有 Pillow）时，
  `warn` 交付但打 `IMG_GATE_SKIPPED`（可见降级），`block` 按不通过处理；
- 不过的图不交付，落 `blocked` 资产（带 `blocked_reason`），管理员可人工放行。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents_image.pixels import (
    crop_to_aspect,
    crop_to_square,
    decode_any,
    decode_png,
    resize,
    stats_of,
    whiten_near_white,
)
from app.agents_image.png import encode_png

# 默认阈值：与淘系平台卡一致（第三轮 P2-2：默认值也要与脚本/平台卡同源）；
# 平台卡里有 `compliance` 段时以平台卡为准。
DEFAULT_THRESHOLDS = {
    "aspect_tolerance": 0.04,
    "min_px": 800,
    "max_mb": 3.0,
    "white_ratio": 0.55,
    "subject_min": 0.15,
    "subject_max": 0.90,
    "margin_min": 0.15,
}


@dataclass
class Verdict:
    passed: bool
    reasons: list[str]
    data: bytes
    fixed: bool = False
    stats: dict = None
    degraded: bool = False          # 像素校验被跳过（解不了图），交付但要可见

    def as_dict(self) -> dict:
        return {"passed": self.passed, "reasons": list(self.reasons), "fixed": self.fixed,
                "degraded": self.degraded, "stats": dict(self.stats or {})}


def _ratio(aspect: str | None) -> float:
    try:
        left, right = str(aspect or "1:1").split(":")
        return float(left) / float(right)
    except (ValueError, ZeroDivisionError):
        return 1.0


def _thresholds(platform_rules, aspect: str | None = None) -> dict:
    merged = dict(DEFAULT_THRESHOLDS)
    merged.update(dict(getattr(platform_rules, "compliance", {}) or {}))
    if platform_rules is not None:
        merged["min_px"] = int(getattr(platform_rules, "min_px", merged["min_px"]))
        merged["max_mb"] = float(getattr(platform_rules, "max_mb", merged["max_mb"]))
    # 关键：比例基准用**请求**的 aspect；平台卡只贡献容差与阈值（P0-3）
    merged["aspect"] = _ratio(aspect or getattr(platform_rules, "aspect", "1:1"))
    return merged


def _check(data: bytes, thresholds: dict) -> tuple[bool, list[str], dict, bool]:
    """返回 (passed, reasons, stats, degraded)。degraded=True 表示像素校验被跳过。"""
    reasons: list[str] = []
    stats: dict = {"bytes": len(data), "mb": round(len(data) / (1024 * 1024), 3)}
    decoded = decode_any(data)
    if decoded is None:
        # 解不了像素：不再静默 passed=True（P1-4）——交给上层按 warn/block 策略处理
        stats["pixel_check"] = False
        return True, ["pixel_check_skipped: 无法解码像素（非 PNG 且未安装 Pillow）"], stats, True
    width, height, channels, pixels = decoded
    stats.update({"width": width, "height": height, "pixel_check": True})
    if stats["mb"] > thresholds["max_mb"]:
        reasons.append(f"文件大小 {stats['mb']}MB 超过 {thresholds['max_mb']}MB")
    if max(width, height) < thresholds["min_px"]:
        reasons.append(f"长边 {max(width, height)}px 小于 {thresholds['min_px']}px")
    expected = thresholds.get("aspect", 1.0)
    if abs(width / height - expected) > thresholds["aspect_tolerance"] * max(expected, 1.0):
        reasons.append(f"比例 {width}x{height}（{width / height:.2f}）与请求 {expected:.2f} 不符")
    pixel = stats_of(width, height, channels, pixels, white_threshold=250)
    stats.update(pixel)
    if pixel["white_ratio"] < thresholds["white_ratio"]:
        reasons.append(f"白底纯度 {pixel['white_ratio']:.0%} 低于 {thresholds['white_ratio']:.0%}")
    if not (thresholds["subject_min"] <= pixel["subject_ratio"] <= thresholds["subject_max"]):
        reasons.append(f"主体占比 {pixel['subject_ratio']:.0%} 超出 "
                       f"{thresholds['subject_min']:.0%}–{thresholds['subject_max']:.0%}")
    if pixel["margin_ratio"] < thresholds["margin_min"]:
        reasons.append(f"四周留白 {pixel['margin_ratio']:.0%} 不足 {thresholds['margin_min']:.0%}")
    return not reasons, reasons, stats, False


def _autofix(data: bytes, thresholds: dict) -> bytes:
    """修复：先归白，再按**请求比例**裁，最后补足长边。"""
    width, height, channels, pixels = decode_png(data)
    fixed = whiten_near_white(width, height, channels, pixels, threshold=200)
    w, h, channels, pixels = decode_png(fixed)
    expected = thresholds.get("aspect", 1.0)
    if abs(w / h - expected) > thresholds.get("aspect_tolerance", 0.04):
        fixed = crop_to_square(w, h, channels, pixels) if abs(expected - 1.0) < 1e-6 \
            else crop_to_aspect(w, h, channels, pixels, expected)
        w, h, channels, pixels = decode_png(fixed)
    if max(w, h) < thresholds["min_px"]:
        scale = thresholds["min_px"] / max(w, h)
        fixed = resize(w, h, channels, pixels, int(w * scale), int(h * scale))
    return fixed or encode_png(w, h, bytes(pixels[: w * h * channels]))


def gate(data: bytes, *, slot: int, role: str, platform_rules=None,
         gate_mode: str = "main", autofix: bool = False, aspect: str | None = None,
         nonpng_policy: str = "warn") -> Verdict:
    """对单张出图做门禁：返回是否通过、原因与（可能的）修复后字节。"""
    mode = (gate_mode or "main").strip().lower()
    if mode == "off":
        return Verdict(passed=True, reasons=[], data=data)
    is_main = int(slot) == 1 or role == "主图"
    if mode == "main" and not is_main:
        return Verdict(passed=True, reasons=[], data=data)

    thresholds = _thresholds(platform_rules, aspect)
    passed, reasons, stats, degraded = _check(data, thresholds)
    if degraded:
        policy = str(nonpng_policy or "warn").strip().lower()
        if policy == "block":
            return Verdict(passed=False, reasons=reasons or ["pixel_check_unavailable"],
                           data=data, stats=stats, degraded=True)
        return Verdict(passed=True, reasons=reasons, data=data, stats=stats, degraded=True)
    if passed:
        return Verdict(passed=True, reasons=[], data=data, stats=stats)
    if autofix:
        try:
            fixed = _autofix(data, thresholds)
            fixed_passed, fixed_reasons, fixed_stats, fixed_degraded = _check(fixed, thresholds)
            if fixed_passed and not fixed_degraded:
                return Verdict(passed=True, reasons=[], data=fixed, fixed=True,
                               stats=fixed_stats)
            return Verdict(passed=False, reasons=fixed_reasons, data=fixed, fixed=True,
                           stats=fixed_stats, degraded=fixed_degraded)
        except Exception as exc:  # noqa: BLE001 —— 修复失败就按原图判定
            reasons.append(f"autofix 失败：{type(exc).__name__}")
    return Verdict(passed=False, reasons=reasons, data=data, stats=stats)


__all__ = ["Verdict", "gate", "DEFAULT_THRESHOLDS"]
