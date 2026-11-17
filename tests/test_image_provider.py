"""Fake Provider：契约、PNG 合法性、确定性、参考图上限。"""

from __future__ import annotations

import asyncio

import pytest

from app.agents_image.providers import image_provider as contract
from app.agents_image.providers.fake import FakeProvider
from app.core.errors import ImageError


def _req(**kwargs):
    base = dict(
        prompt="白底主图，正面全身像上装突出",
        init_images=[contract.ImageInput(data=b"ref-image-bytes", mime="image/png", sha256="abc123")],
        width=64, height=64, n=1,
    )
    base.update(kwargs)
    return contract.ImageRequest(**base)


def _run(coro):
    return asyncio.run(coro)


def test_fake_is_registered_and_caps():
    assert "fake" in contract.providers()
    provider = contract.get_provider("fake")
    assert provider.caps.supports_reference is True
    assert provider.caps.max_refs == 6
    assert provider.caps.max_n == 4


def test_fake_returns_valid_png_with_requested_size():
    result = _run(FakeProvider().generate(_req(width=96, height=64)))
    assert len(result.images) == 1
    png = result.images[0]
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    # IHDR 宽高（PNG 第 16..24 字节）
    width = int.from_bytes(png[16:20], "big")
    height = int.from_bytes(png[20:24], "big")
    assert (width, height) == (96, 64)
    assert result.meta.get("placeholder") is True
    assert result.provider == "fake"
    assert result.cost_cny == 0.0


def test_fake_is_deterministic():
    first = _run(FakeProvider().generate(_req())).images[0]
    second = _run(FakeProvider().generate(_req())).images[0]
    assert first == second

    other = _run(FakeProvider().generate(_req(prompt="换夜色街拍"))).images[0]
    assert other != first


def test_fake_honors_n():
    result = _run(FakeProvider().generate(_req(n=3)))
    assert len(result.images) == 3


def test_missing_reference_is_rejected():
    with pytest.raises(ImageError) as exc:
        _run(FakeProvider().generate(_req(init_images=[])))
    assert exc.value.code == "IMG_REFERENCE_REQUIRED"


def test_too_many_references_is_rejected():
    refs = [contract.ImageInput(data=b"x", sha256=f"s{i}") for i in range(7)]
    with pytest.raises(ImageError) as exc:
        _run(FakeProvider().generate(_req(init_images=refs)))
    assert exc.value.code == "IMG_TOO_MANY_REFS"


def test_unregistered_provider_reports_unavailable():
    with pytest.raises(ImageError) as exc:
        contract.get_provider("no-such-provider")
    assert exc.value.code == "IMG_PROVIDER_UNAVAILABLE"


def test_fake_health_reports_placeholder():
    health = _run(FakeProvider().health())
    assert health["ok"] is True
    assert health["mode"] == "placeholder"
