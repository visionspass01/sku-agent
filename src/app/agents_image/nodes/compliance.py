"""交付前像素门禁（M5）：出图后**先过平台规则，再决定能不能交付**。

对应参考仓库 `scripts/check_main_image.py` 的定位——"校验不过的图不许交付"，本项目把它
从"事后脚本"变成"链路里的一步"：

- 阈值来自**中台平台规则卡**的 `compliance` 段（与 `scripts/check_image.py` 同源）；
- `IMAGE_COMPLIANCE_GATE=main|all|off`：默认只管主图（blocking），`all` 管所有 generate 图位；
- `IMAGE_COMPLIANCE_AUTOFIX=true` 时先做归白/裁方/放大再复检；
- 不过的图不交付，落 `blocked` 资产（带 `blocked_reason`），管理员可人工放行。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents_image.pixels import (
    PngDecodeError,
    crop_to_square,
    decode_png,
    resize,
    rgb_stats,
    whiten_near_white,
)
from app.agents_image.png import encode_png

DEFAULT_THRESHOLDS = {
    "aspect_tolerance": 0.04,
    "min_px": 800,
    "max_mb": 3.0,
    "white_ratio": 0.55,
    "subject_min": 0.05,
    "subject_max": 0.95,
    "margin_min": 0.15,
}


@dataclass
class Verdict:
    passed: bool
    reasons: list[str]
    data: bytes
    fixed: bool = False
    stats: dict = None

    def as_dict(self) -> dict:
        return {"passed": self.passed, "reasons": list(self.reasons), "fixed": self.fixed,
                "stats": dict(self.stats or {})}


def _thresholds(platform_rules) -> dict:
    merged = dict(DEFAULT_THRESHOLDS)
    merged.update(dict(getattr(platform_rules, "compliance", {}) or {}))
    if platform_rules is not None:
        merged["min_px"] = int(getattr(platform_rules, "min_px", merged["min_px"]))
        merged["max_mb"] = float(getattr(platform_rules, "max_mb", merged["max_mb"]))
        aspect = str(getattr(platform_rules, "aspect", "1:1") or "1:1")
        try:
            left, right = aspect.split(":")
            merged["aspect"] = float(left) / float(right)
        except (ValueError, ZeroDivisionError):
            merged["aspect"] = 1.0
    merged.setdefault("aspect", 1.0)
    return merged


def _check(data: bytes, thresholds: dict) -> tuple[bool, list[str], dict]:
    reasons: list[str] = []
    stats: dict = {"bytes": len(data), "mb": round(len(data) / (1024 * 1024), 3)}
    try:
        width, height, _channels, _pixels = decode_png(data)
    except PngDecodeError as exc:
        # 非 PNG（JPEG/WebP 需要 Pillow）：只做"能解码出头"的降级判断
        return True, [f"pixel_check_skipped: {exc}"], {**stats, "pixel_check": False}
    stats.update({"width": width, "height": height, "pixel_check": True})
    if stats["mb"] > thresholds["max_mb"]:
        reasons.append(f"文件大小 {stats['mb']}MB 超过 {thresholds['max_mb']}MB")
    if max(width, height) < thresholds["min_px"]:
        reasons.append(f"长边 {max(width, height)}px 小于 {thresholds['min_px']}px")
    expected = thresholds.get("aspect", 1.0)
    if abs(width / height - expected) > thresholds["aspect_tolerance"] * max(expected, 1.0):
        reasons.append(f"比例 {width}x{height} 与平台 {expected:.2f} 不符")
    pixel = rgb_stats(data)
    stats.update(pixel)
    if pixel["white_ratio"] < thresholds["white_ratio"]:
        reasons.append(f"白底纯度 {pixel['white_ratio']:.0%} 低于 {thresholds['white_ratio']:.0%}")
    if not (thresholds["subject_min"] <= pixel["subject_ratio"] <= thresholds["subject_max"]):
        reasons.append(f"主体占比 {pixel['subject_ratio']:.0%} 超出区间")
    if pixel["margin_ratio"] < thresholds["margin_min"]:
        reasons.append(f"四周留白 {pixel['margin_ratio']:.0%} 不足 {thresholds['margin_min']:.0%}")
    return not reasons, reasons, stats


def _autofix(data: bytes, thresholds: dict) -> bytes:
    width, height, channels, pixels = decode_png(data)
    fixed = whiten_near_white(width, height, channels, pixels, threshold=200)
    w, h, channels, pixels = decode_png(fixed)
    if abs(w - h) > max(2, int(min(w, h) * thresholds.get("aspect_tolerance", 0.04))):
        fixed = crop_to_square(w, h, channels, pixels)
        w, h, channels, pixels = decode_png(fixed)
    if max(w, h) < thresholds["min_px"]:
        scale = thresholds["min_px"] / max(w, h)
        fixed = resize(w, h, channels, pixels, int(w * scale), int(h * scale))
    return fixed or encode_png(w, h, bytes(pixels[: w * h * channels]))


def gate(data: bytes, *, slot: int, role: str, platform_rules=None,
         gate_mode: str = "main", autofix: bool = False) -> Verdict:
    """对单张出图做门禁：返回是否通过、原因与（可能的）修复后字节。"""
    mode = (gate_mode or "main").strip().lower()
    if mode == "off":
        return Verdict(passed=True, reasons=[], data=data)
    is_main = int(slot) == 1 or role == "主图"
    if mode == "main" and not is_main:
        return Verdict(passed=True, reasons=[], data=data)

    thresholds = _thresholds(platform_rules)
    passed, reasons, stats = _check(data, thresholds)
    if passed:
        return Verdict(passed=True, reasons=[], data=data, stats=stats)
    if autofix:
        try:
            fixed = _autofix(data, thresholds)
            fixed_passed, fixed_reasons, fixed_stats = _check(fixed, thresholds)
            if fixed_passed:
                return Verdict(passed=True, reasons=[], data=fixed, fixed=True,
                               stats=fixed_stats)
            return Verdict(passed=False, reasons=fixed_reasons, data=fixed, fixed=True,
                           stats=fixed_stats)
        except Exception as exc:  # noqa: BLE001 —— 修复失败就按原图判定
            reasons.append(f"autofix 失败：{type(exc).__name__}")
    return Verdict(passed=False, reasons=reasons, data=data, stats=stats)


__all__ = ["Verdict", "gate", "DEFAULT_THRESHOLDS"]
