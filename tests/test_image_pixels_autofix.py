"""第四轮 W6：非 PNG 自动修复（P2-15）。

- 装了 Pillow：JPEG/WebP 也走"归白 → 按比例裁 → 补长边 → 转 PNG"，修复后按 PNG 复检；
- 没装 Pillow：保持 `degraded`（可见降级），不假装校验过。
本机 venv 没装 Pillow，所以用桩模块验证调用链与参数；无 Pillow 的路径用 sys.modules 置空模拟。
"""

from __future__ import annotations

import sys
import types

from app.agents_image import pixels
from app.agents_image.nodes import compliance
from app.agents_image.png import placeholder_compliant_png


class _FakeImage:
    """Pillow Image 的最小替身：只实现 autofix_nonpng 用到的几个方法。"""

    def __init__(self, width: int, height: int):
        self.size = (width, height)

    def convert(self, mode: str) -> "_FakeImage":
        return self

    def point(self, func) -> "_FakeImage":
        return self

    def crop(self, box) -> "_FakeImage":
        left, top, right, bottom = box
        return _FakeImage(right - left, bottom - top)

    def resize(self, size, resample=None) -> "_FakeImage":
        return _FakeImage(int(size[0]), int(size[1]))

    def save(self, buffer, format=None) -> None:      # noqa: A002
        width, height = self.size
        buffer.write(placeholder_compliant_png("fixed", width, height))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakePILImageModule:
    NEAREST = "nearest"

    @staticmethod
    def open(buffer):                                 # noqa: A002
        return _FakeImage(300, 450)


def _fake_pil():
    return types.SimpleNamespace(Image=_FakePILImageModule)


# ---------------------------------------------------------------- 无 Pillow：保持降级
def test_autofix_nonpng_returns_none_without_pillow(monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", None)     # 模拟"没装 Pillow"
    assert pixels.autofix_nonpng(b"not-a-png", ratio=1.0, min_px=800) is None


# ---------------------------------------------------------------- 有 Pillow：转 PNG + 裁 + 放大
def test_autofix_nonpng_converts_crops_and_upscales(monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", _fake_pil())
    fixed = pixels.autofix_nonpng(b"\xff\xd8jpeg-bytes", ratio=1.0, min_px=800)
    assert fixed is not None and fixed.startswith(b"\x89PNG")
    width, height, _channels, _pixels = pixels.decode_png(fixed)
    assert (width, height) == (800, 800)              # 1:1 裁切后长边补到平台下限


def test_autofix_nonpng_respects_requested_ratio(monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", _fake_pil())
    fixed = pixels.autofix_nonpng(b"\xff\xd8jpeg-bytes", ratio=3 / 4, min_px=400)
    assert fixed is not None
    width, height, _channels, _pixels = pixels.decode_png(fixed)
    assert abs(width / height - 3 / 4) < 0.01


# ---------------------------------------------------------------- 门禁接线
def _jpeg_undecodable(monkeypatch):
    """只让 JPEG 字节"解不了像素"，PNG 仍走真实解码（复检要用）。"""
    real = pixels.decode_any
    monkeypatch.setattr(compliance, "decode_any",
                        lambda data: None if data.startswith(b"\xff\xd8") else real(data))


def test_gate_autofixes_nonpng_when_fixer_available(monkeypatch):
    good = placeholder_compliant_png("fixed", 1024, 1024)
    _jpeg_undecodable(monkeypatch)
    monkeypatch.setattr(compliance, "autofix_nonpng",
                        lambda data, **kwargs: good)
    verdict = compliance.gate(b"\xff\xd8jpeg-bytes", slot=1, role="主图",
                              aspect="1:1", autofix=True)
    assert verdict.passed is True
    assert verdict.fixed is True
    assert verdict.degraded is False          # 修复后是真像素校验过的


def test_gate_keeps_degraded_without_fixer(monkeypatch):
    _jpeg_undecodable(monkeypatch)
    monkeypatch.setattr(compliance, "autofix_nonpng", lambda data, **kwargs: None)
    warn = compliance.gate(b"\xff\xd8jpeg-bytes", slot=1, role="主图",
                           aspect="1:1", autofix=True, nonpng_policy="warn")
    assert warn.passed is True and warn.degraded is True
    blocked = compliance.gate(b"\xff\xd8jpeg-bytes", slot=1, role="主图",
                              aspect="1:1", autofix=True, nonpng_policy="block")
    assert blocked.passed is False and blocked.degraded is True


def test_gate_reports_failure_when_fixed_image_still_bad(monkeypatch):
    bad = placeholder_compliant_png("bad", 200, 200)     # 长边不足下限且比例不符
    _jpeg_undecodable(monkeypatch)
    monkeypatch.setattr(compliance, "autofix_nonpng", lambda data, **kwargs: bad)
    verdict = compliance.gate(b"\xff\xd8jpeg-bytes", slot=1, role="主图",
                              aspect="1:1", autofix=True, nonpng_policy="warn")
    assert verdict.passed is False
    assert verdict.reasons, "修复后不达标必须给出原因"
