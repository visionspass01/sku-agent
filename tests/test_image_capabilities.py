"""M5：图内文案（按图位+字数表）、淘系/抖系双套、variant 差异化、交付前像素门禁。"""

from __future__ import annotations

import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import ImageSettings, get_image_settings, reset_image_settings
from app.agents_image.nodes import compliance, copywriter
from app.agents_image.png import placeholder_compliant_png
from app.agents_image.schemas import ClothingAttrs
from app.agents_image.sources import product_source as ps
from app.agents_image.sources.fake_catalog import _RULES
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
    image_metrics.reset()
    ensure_image_schema()
    with session_scope() as session:
        user_store.ensure_seed_users(session)
    yield app_client
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()


def login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


# ---------------------------------------------------------------- 图内文案
def test_copy_respects_platform_policy():
    attrs = ClothingAttrs(product_type="针织开衫", shape="落肩、V 领",
                          color_names=["米色"], materials="羊毛混纺 30%",
                          key_features=["罗纹袖口", "双口袋"])
    taoxi = copywriter.build_copy(attrs, "上装卖点", platform_rules=_RULES["淘系"])
    assert taoxi.allowed is True
    assert taoxi.headline and taoxi.subhead == taoxi.subhead  # 允许有字
    assert taoxi.char_count <= taoxi.limit

    douxi = copywriter.build_copy(attrs, "上装卖点", platform_rules=_RULES["抖系"])
    # A2：抖系只允许品牌 + 纯数字角标，不允许主标语/副文案
    assert douxi.headline == "" and douxi.subhead == ""
    assert douxi.badge == "30" or douxi.badge == ""      # 来自「羊毛混纺 30%」的数字
    assert douxi.allowed is bool(douxi.badge)


def test_copy_is_numeric_and_avoids_banned_words():
    attrs = ClothingAttrs(product_type="卫衣", materials="棉 80%",
                          key_features=["加绒内里", "绝对保暖"])
    copy = copywriter.build_copy(attrs, "细节特写", platform_rules=_RULES["淘系"])
    assert "绝对" not in copy.headline and "绝对" not in copy.subhead
    assert copy.badge in {"80", "30", ""}          # 角标只放数字


def test_copy_describe_block():
    copy = copywriter.SlotCopy(allowed=True, headline="加绒内里", subhead="棉 80%",
                               badge="80", char_count=9, limit=37)
    text = copywriter.describe(copy)
    assert "主标语" in text and "副文案" in text and "角标" in text
    assert "不生成任何文字" in copywriter.describe(copywriter.SlotCopy(allowed=False))


# ---------------------------------------------------------------- 双平台
def test_platform_variant_creates_two_sets(env):
    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1,
        "platform_variant": True,
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["platforms"] == ["淘系", "抖系"]
    assert {item["platform"] for item in body["tasks"]} == {"淘系", "抖系"}
    assert len(body["tasks"]) == 2


def test_unknown_platform_is_rejected(env):
    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白底主图", "platforms": ["京东"],
    })
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "IMG_INPUT_INVALID"


def test_douxi_prompts_have_no_copy_text(env):
    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 2, "platform": "抖系",
    })
    assert resp.status_code == 201
    task_id = resp.json()["tasks"][0]["task_id"]
    deadline = time.time() + 45
    detail: dict = {}
    while time.time() < deadline:
        detail = env.get(f"/api/image/admin/tasks/{task_id}", headers=headers).json()
        if detail["status"] in {"done", "failed"} and detail["backfill_status"] != "pending":
            break
        time.sleep(0.05)
    assert detail["status"] == "done", detail
    for draft in detail["prompt_pack"]:
        if draft.get("type") == "generate":
            spec = draft["copy_spec"]
            # A2：抖系可以留数字角标，但绝不能有主标语/副文案
            assert spec["headline"] == "" and spec["subhead"] == ""


# ---------------------------------------------------------------- variant
def test_variant_mode_creates_two_directions(env):
    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1002"], "prompt": "白底主图", "image_count": 1, "mode": "variant",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["tasks"]) == 2
    assert {item["variant_id"] for item in body["tasks"]} == {"A", "B"}
    directions = {item["variant_direction"] for item in body["tasks"]}
    assert len(directions) == 2


def test_iterate_mode_only_redoes_main(env):
    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1002"], "prompt": "主图点击率低，只改主图",
        "mode": "iterate", "image_count": 1, "slot_count": 1,
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["slot_count"] == 1


def test_bad_mode_rejected(env):
    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1002"], "prompt": "白底主图", "mode": "banner",
    })
    assert resp.status_code == 422


# ---------------------------------------------------------------- 像素门禁
def _dirty_png(width: int = 900, height: int = 900, grey: int = 236) -> bytes:
    """灰底 + 居中主体（白底不纯的典型样张，用于门禁/修复测试）。"""
    from app.agents_image.png import encode_png

    rows = bytearray()
    for y in range(height):
        for x in range(width):
            inside = int(width * 0.2) <= x < int(width * 0.8) and \
                int(height * 0.2) <= y < int(height * 0.8)
            rows += bytes((42, 92, 138)) if inside else bytes((grey, grey, grey))
    return encode_png(width, height, bytes(rows))


def test_gate_blocks_dirty_background():
    dirty = _dirty_png()                                # 灰底（236）：白底纯度不够
    verdict = compliance.gate(dirty, slot=1, role="主图", platform_rules=_RULES["淘系"],
                              gate_mode="main", autofix=False)
    assert verdict.passed is False
    assert any("白底" in reason or "主体" in reason or "留白" in reason
               for reason in verdict.reasons)


def test_gate_autofix_repairs_dirty_background():
    dirty = _dirty_png(grey=236)
    verdict = compliance.gate(dirty, slot=1, role="主图", platform_rules=_RULES["淘系"],
                              gate_mode="main", autofix=True)
    assert verdict.passed is True
    assert verdict.fixed is True


def test_gate_passes_compliant_and_skips_non_main():
    good = placeholder_compliant_png("ok", 900, 900)
    assert compliance.gate(good, slot=1, role="主图", platform_rules=_RULES["淘系"],
                           gate_mode="main", autofix=False).passed is True
    dirty = _dirty_png(grey=236)
    # 默认只拦主图：非主图位不拦截
    assert compliance.gate(dirty, slot=3, role="场景 A", platform_rules=_RULES["淘系"],
                           gate_mode="main", autofix=False).passed is True
    # all 模式下非主图也拦
    assert compliance.gate(dirty, slot=3, role="场景 A", platform_rules=_RULES["淘系"],
                           gate_mode="all", autofix=False).passed is False
    # off 模式全放行
    assert compliance.gate(dirty, slot=1, role="主图", platform_rules=_RULES["淘系"],
                           gate_mode="off").passed is True


def test_gate_mode_setting_default():
    settings = ImageSettings()
    assert settings.compliance_gate == "main"
    assert settings.compliance_autofix is True
    assert get_image_settings().compliance_gate in {"main", "all", "off"}
