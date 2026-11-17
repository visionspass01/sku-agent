"""生成合规校验夹具（5 张合成 PNG，供 `tests/test_image_compliance.py` 与手工演示）。

夹具刻意覆盖 5 种情况：合规 / 白底不纯 / 留白过多（主体太小）/ 非正方形 / 超尺寸。
用纯标准库生成，CI 不需要 Pillow。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app.agents_image.png import encode_png  # noqa: E402

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "data" / "eval" / "image_fixtures"


def _canvas(width: int, height: int, background: tuple[int, int, int]) -> bytearray:
    return bytearray(background * (width * height))


def _fill_rect(rows: bytearray, width: int, height: int,
               left: int, top: int, right: int, bottom: int,
               color: tuple[int, int, int]) -> None:
    for y in range(max(0, top), min(height, bottom)):
        start = (y * width + left) * 3
        end = (y * width + right) * 3
        rows[start:end] = bytes(color * (right - left))


def _save(path: Path, width: int, height: int, rows: bytearray) -> None:
    path.write_bytes(encode_png(width, height, bytes(rows)))


def build(directory: Path = DEFAULT_DIR) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}

    # 1) 合规：900x900 纯白底，主体居中占 ~40%
    size = 900
    rows = _canvas(size, size, (255, 255, 255))
    _fill_rect(rows, size, size, 270, 240, 630, 660, (42, 92, 138))
    path = directory / "ok_main.png"
    _save(path, size, size, rows)
    outputs["ok"] = path

    # 2) 白底不纯：整张灰底（RGB 236）
    rows = _canvas(size, size, (236, 236, 236))
    _fill_rect(rows, size, size, 270, 240, 630, 660, (42, 92, 138))
    path = directory / "dirty_white.png"
    _save(path, size, size, rows)
    outputs["dirty_white"] = path

    # 3) 留白过多 / 主体过小：主体只占 ~1%
    rows = _canvas(size, size, (255, 255, 255))
    _fill_rect(rows, size, size, 430, 430, 470, 470, (42, 92, 138))
    path = directory / "tiny_subject.png"
    _save(path, size, size, rows)
    outputs["tiny_subject"] = path

    # 4) 非正方形：1200x800
    width, height = 1200, 800
    rows = _canvas(width, height, (255, 255, 255))
    _fill_rect(rows, width, height, 380, 220, 820, 620, (42, 92, 138))
    path = directory / "not_square.png"
    _save(path, width, height, rows)
    outputs["not_square"] = path

    # 5) 超尺寸（长边 4000）+ 超大文件（噪音让 PNG 无法压到 3MB 以下）
    width = height = 4000
    rows = bytearray()
    seed = 20261118
    for y in range(height):
        line = bytearray()
        for x in range(width):
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            value = 150 + (seed >> 16) % 90
            line += bytes((value, value, value))
        rows += line
    _fill_rect(rows, width, height, 1200, 1000, 2800, 3000, (30, 60, 90))
    path = directory / "too_large.png"
    _save(path, width, height, rows)
    outputs["too_large"] = path

    return outputs


def main() -> int:
    outputs = build()
    for name, path in outputs.items():
        print(f"{name}: {path}（{path.stat().st_size // 1024}KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
