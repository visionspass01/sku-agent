"""第三轮 P0-3 / P1-4：像素门禁的比例基准与"解不了像素"的处理。

背景（评审证据）：参数层放行 1:1/3:4/4:3/16:9，但门禁用平台卡的默认 1:1 判比例，
非方图请求必然"比例不符 → autofix 裁方 → 白底不达标 → 全拦 → 任务失败"。
"""

from __future__ import annotations

import sys
import time
import types

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.nodes import compliance
from app.agents_image.pixels import crop_to_aspect, decode_png
from app.agents_image.png import placeholder_compliant_png
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    monkeypatch.delenv("IMAGE_PRODUCT_SOURCE", raising=False)
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()
    image_metrics.reset()
    ensure_image_schema()
    with session_scope() as session:
        user_store.ensure_seed_users(session)
    yield app_client
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()


def login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _wait(env, headers, task_id, timeout=25.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail.get("status") in {"done", "failed", "canceled"} and \
                detail.get("backfill_status") != "pending":
            return detail
        time.sleep(0.05)
    return detail


def _rules():
    import asyncio

    return asyncio.run(ps.get_catalog().get_platform_rules(platform="淘系"))


# ---------------------------------------------------------------- 端到端：四种比例都能交付
@pytest.mark.parametrize("aspect,expected", [
    ("1:1", 1.0), ("3:4", 3 / 4), ("4:3", 4 / 3), ("16:9", 16 / 9),
])
def test_all_allowed_aspects_are_deliverable(env, aspect, expected):
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白底主图", "aspect": aspect, "image_count": 1,
    })
    assert created.status_code == 201, created.text
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert detail["status"] == "done", detail
    assert detail["delivered_images"] == 1
    width, height = detail["assets"][0]["width"], detail["assets"][0]["height"]
    assert abs(width / height - expected) < 0.02, (width, height, aspect)


# ---------------------------------------------------------------- 门禁基准 = 请求比例
def test_gate_uses_requested_aspect_as_baseline():
    rules = _rules()
    image = placeholder_compliant_png("aspect-probe", 1024, 576)      # 16:9 合规占位图
    ok = compliance.gate(image, slot=1, role="主图", platform_rules=rules,
                         aspect="16:9", autofix=False)
    assert ok.passed is True, ok.reasons
    bad = compliance.gate(image, slot=1, role="主图", platform_rules=rules,
                          aspect="1:1", autofix=False)
    assert bad.passed is False
    assert any("与请求 1.00 不符" in reason for reason in bad.reasons)


def test_autofix_crops_to_requested_aspect():
    rules = _rules()
    square = placeholder_compliant_png("autofix-probe", 1024, 1024)
    verdict = compliance.gate(square, slot=1, role="主图", platform_rules=rules,
                              aspect="16:9", autofix=True)
    assert verdict.fixed is True
    width, height, _channels, _pixels = decode_png(verdict.data)
    assert abs(width / height - 16 / 9) < 0.02, (width, height)


def test_crop_to_aspect_keeps_target_ratio():
    data = placeholder_compliant_png("crop-probe", 768, 1024)
    width, height, channels, pixels = decode_png(data)
    cropped = crop_to_aspect(width, height, channels, pixels, 3 / 4)
    new_w, new_h, _c, _p = decode_png(cropped)
    assert (new_w, new_h) == (768, 1024)          # 本来就是 3:4，保持原样
    cropped = crop_to_aspect(width, height, channels, pixels, 1.0)
    new_w, new_h, _c, _p = decode_png(cropped)
    assert (new_w, new_h) == (768, 768)


# ---------------------------------------------------------------- 非 PNG：warn / block
class _FakePillowImage:
    """模拟 Pillow 解出来的一张"白底 + 居中主体"的 JPEG。"""

    width = 1024
    height = 1024

    def convert(self, _mode: str):
        return self

    def tobytes(self) -> bytes:
        width, height = self.width, self.height
        pixels = bytearray()
        for y in range(height):
            for x in range(width):
                inside = width // 4 <= x < width * 3 // 4 and height // 4 <= y < height * 3 // 4
                pixels += bytes((120, 120, 120)) if inside else bytes((255, 255, 255))
        return bytes(pixels)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _no_pillow(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "PIL", types.SimpleNamespace(Image=types.SimpleNamespace(
        open=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no pillow")))))


def test_non_png_without_pillow_warns_but_delivers(monkeypatch):
    """没有 Pillow 时不再"静默通过"：交付 + degraded + 明确原因（P1-4）。"""
    _no_pillow(monkeypatch)
    verdict = compliance.gate(b"\xff\xd8\xff" + b"0" * 64, slot=1, role="主图",
                              platform_rules=_rules(), aspect="1:1",
                              nonpng_policy="warn")
    assert verdict.passed is True
    assert verdict.degraded is True
    assert any("pixel_check_skipped" in reason for reason in verdict.reasons)


def test_non_png_block_policy_blocks(monkeypatch):
    _no_pillow(monkeypatch)
    verdict = compliance.gate(b"\xff\xd8\xff" + b"0" * 64, slot=1, role="主图",
                              platform_rules=_rules(), aspect="1:1",
                              nonpng_policy="block")
    assert verdict.passed is False
    assert verdict.degraded is True


def test_non_png_uses_pillow_when_available(monkeypatch):
    """装了 Pillow 就走真解码：白底 + 居中主体应通过门禁。"""
    monkeypatch.setitem(sys.modules, "PIL", types.SimpleNamespace(
        Image=types.SimpleNamespace(open=lambda *_a, **_k: _FakePillowImage())))
    verdict = compliance.gate(b"\xff\xd8\xff" + b"0" * 64, slot=1, role="主图",
                              platform_rules=_rules(), aspect="1:1",
                              autofix=False, nonpng_policy="warn")
    assert verdict.degraded is False
    assert verdict.passed is True, verdict.reasons
    assert verdict.stats["pixel_check"] is True
    assert verdict.stats["width"] == 1024


def test_gate_skipped_is_visible_in_task(env, monkeypatch):
    """非 PNG 交付要留痕：任务 warnings 里有 IMG_GATE_SKIPPED，且指标计数 +1。"""
    from app.agents_image.providers.image_provider import ImageResult

    class _JpegProvider:
        name = "fake"

        async def generate(self, req):        # 返回伪造 JPEG 字节（解不了像素）
            return ImageResult(images=[b"\xff\xd8\xff" + b"0" * 128], mime="image/jpeg",
                               provider="fake", model="jpeg-stub", cost_cny=0.0,
                               latency_ms=1)

    _no_pillow(monkeypatch)
    monkeypatch.setattr("app.agents_image.providers.fake.FakeProvider.generate",
                        _JpegProvider.generate)
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1,
    })
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert detail["status"] == "done"
    assert any("IMG_GATE_SKIPPED" in item for item in detail["warnings"]), detail["warnings"]
    assert image_metrics.snapshot()["gate_skipped"] >= 1
    # 门禁/autofix 移出写事务后仍要保留原图 mime（不能把 JPEG 存成 image/png）
    assert detail["assets"][0]["mime"] == "image/jpeg"
