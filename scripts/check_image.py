"""像素级合规校验（对应计划 §14，参照参考仓库 `check_main_image.py`）。

用法：
    PYTHONPATH=src python scripts/check_image.py 主图.png --platform 淘系
    PYTHONPATH=src python scripts/check_image.py 主图.png --platform 淘系 --fix 修好.png
    PYTHONPATH=src python scripts/check_image.py 主图.png --json      # 机器可读

检查项：尺寸/长边下限、1:1 比例、白底纯度、主体占比、四周留白、文件大小、图内文字启发式。
依赖：**PNG 用自带解码（零依赖）**；JPEG/WebP 需要 Pillow（可选，没有就退化为"只校验头部尺寸与大小"）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app.agents_image.pixels import (  # noqa: E402
    PngDecodeError,
    crop_to_square,
    decode_png,
    resize,
    rgb_stats,
    whiten_near_white,
)
from app.agents_image.storage.image_storage import image_size  # noqa: E402

# 平台阈值（生产以中台 `platform_rules` 为准；这里给脚本一份可执行的默认值）
PLATFORM_RULES = {
    "淘系": {"aspect": 1.0, "min_px": 800, "max_mb": 3.0, "white_ratio": 0.55,
             "subject_min": 0.05, "subject_max": 0.95, "margin_min": 0.15},
    "通用": {"aspect": 1.0, "min_px": 800, "max_mb": 5.0, "white_ratio": 0.35,
             "subject_min": 0.03, "subject_max": 0.97, "margin_min": 0.08},
}
ASPECT_TOLERANCE = 0.04


def _text_like_rows(pixels: bytearray, width: int, height: int, channels: int) -> float:
    """图内文字启发式：顶部 25% 区域里"高对比水平边缘"占比（不做 OCR，不做内容判定）。"""
    band = max(1, int(height * 0.25))
    edges = 0
    total = 0
    for y in range(band):
        row = y * width * channels
        for x in range(1, width):
            left = row + (x - 1) * channels
            right = row + x * channels
            delta = abs(pixels[right] - pixels[left]) + abs(pixels[right + 1] - pixels[left + 1])
            total += 1
            if delta > 120:
                edges += 1
    return edges / total if total else 0.0


def check(path: Path, *, platform: str = "淘系", text_detect: bool = False) -> dict:
    """返回 {ok, platform, checks[], warnings[], stats}；不抛异常（坏文件也算一条结果）。"""
    rules = PLATFORM_RULES.get(platform) or PLATFORM_RULES["通用"]
    data = path.read_bytes()
    checks: list[dict] = []
    warnings: list[str] = []
    stats: dict = {"bytes": len(data), "mb": round(len(data) / (1024 * 1024), 3)}

    def add(name: str, passed: bool, detail: str, blocking: bool = True) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail,
                       "blocking": blocking})

    add("文件大小", stats["mb"] <= rules["max_mb"],
        f"{stats['mb']}MB ≤ {rules['max_mb']}MB")

    width, height = image_size(data)
    stats.update({"width": width, "height": height})
    if not (width and height):
        add("图片可解析", False, "无法解析图片头")
        return {"ok": False, "platform": platform, "checks": checks,
                "warnings": warnings, "stats": stats}

    add("长边像素", max(width, height) >= rules["min_px"],
        f"{max(width, height)}px ≥ {rules['min_px']}px")
    ratio = width / height if height else 0
    add("比例", abs(ratio - rules["aspect"]) <= ASPECT_TOLERANCE,
        f"{width}x{height}（比例 {ratio:.3f}，要求 {rules['aspect']:.2f}±{ASPECT_TOLERANCE}）")

    try:
        w, h, channels, pixels = decode_png(data)
    except PngDecodeError as exc:
        warnings.append(f"像素级校验未启用：{exc}（JPEG/WebP 需要 pip install pillow）")
        stats["pixel_check"] = False
        return {"ok": all(item["passed"] for item in checks), "platform": platform,
                "checks": checks, "warnings": warnings, "stats": stats}

    pixel_stats = rgb_stats(data)
    stats.update({"pixel_check": True, **pixel_stats})
    add("白底纯度", pixel_stats["white_ratio"] >= rules["white_ratio"],
        f"近白像素 {pixel_stats['white_ratio']:.1%} ≥ {rules['white_ratio']:.0%}")
    add("主体占比", rules["subject_min"] <= pixel_stats["subject_ratio"] <= rules["subject_max"],
        f"主体 {pixel_stats['subject_ratio']:.1%}（{rules['subject_min']:.0%}–{rules['subject_max']:.0%}）")
    add("四周留白", pixel_stats["margin_ratio"] >= rules["margin_min"],
        f"边框白像素 {pixel_stats['margin_ratio']:.1%} ≥ {rules['margin_min']:.0%}")
    if text_detect:
        ratio_edges = _text_like_rows(pixels, w, h, channels)
        stats["text_edge_ratio"] = round(ratio_edges, 4)
        add("图内文字（启发式）", ratio_edges < 0.10,
            f"顶部高对比边缘 {ratio_edges:.1%}（≥10% 提示可能有文字）", blocking=False)

    return {"ok": all(item["passed"] for item in checks), "platform": platform,
            "checks": checks, "warnings": warnings, "stats": stats}


def fix(path: Path, target: Path, *, platform: str = "淘系") -> dict:
    """修复：近白归白 → 非方形居中裁切 → 长边不足则放大；返回修复后的校验结果。"""
    rules = PLATFORM_RULES.get(platform) or PLATFORM_RULES["通用"]
    data = path.read_bytes()
    width, height, channels, pixels = decode_png(data)
    # 修复用更宽的阈值（200）：把"发灰的背景"也归白；深色主体不会被误改
    fixed = whiten_near_white(width, height, channels, pixels, threshold=200)
    w, h, channels, pixels = decode_png(fixed)
    if abs(w - h) > max(2, int(min(w, h) * ASPECT_TOLERANCE)):
        fixed = crop_to_square(w, h, channels, pixels)
        w, h, channels, pixels = decode_png(fixed)
    if max(w, h) < rules["min_px"]:
        scale = rules["min_px"] / max(w, h)
        fixed = resize(w, h, channels, pixels, int(w * scale), int(h * scale))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(fixed)
    result = check(target, platform=platform)
    result["fixed_from"] = str(path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生图合规校验（像素级，PNG 零依赖）")
    parser.add_argument("image", help="待校验图片路径")
    parser.add_argument("--platform", default="淘系", choices=sorted(PLATFORM_RULES))
    parser.add_argument("--fix", default="", help="修复后的输出路径（PNG）")
    parser.add_argument("--text-detect", action="store_true", help="启用图内文字启发式")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    path = Path(args.image)
    if not path.is_file():
        print(f"图片不存在：{path}", file=sys.stderr)
        return 2
    result = fix(path, Path(args.fix), platform=args.platform) if args.fix \
        else check(path, platform=args.platform, text_detect=args.text_detect)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{path}（{result['platform']}）：{'通过' if result['ok'] else '不通过'}")
        for item in result["checks"]:
            mark = "[OK]" if item["passed"] else ("[FAIL]" if item["blocking"] else "[WARN]")
            print(f"  {mark} {item['name']}：{item['detail']}")
        for warning in result["warnings"]:
            print(f"  [WARN] {warning}")
        if args.fix:
            print(f"已修复 → {args.fix}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
