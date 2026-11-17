"""生图 Agent 配置：默认值、env 覆盖、边界与安全自检。"""

from __future__ import annotations

import pytest

from app.agents_image.config import (
    DEFAULT_IMAGE_AUTH_SECRET,
    ImageSettings,
    get_image_settings,
    reset_image_settings,
)


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch):
    for key in ("IMAGE_PROVIDER", "IMAGE_MAX_REFS", "IMAGE_MAX_TOTAL_IMAGES",
                "IMAGE_CONCURRENCY", "IMAGE_RETENTION_HOURS", "IMAGE_AUTH_SECRET",
                "IMAGE_ENABLED", "IMAGE_STORAGE", "IMAGE_DISPATCHER"):
        monkeypatch.delenv(key, raising=False)
    reset_image_settings()
    yield
    reset_image_settings()


def test_defaults_are_zero_cost_local():
    settings = get_image_settings()
    assert settings.provider == "fake"
    assert settings.vision_provider == "fake"
    assert settings.storage == "local"
    assert settings.dispatcher == "local"
    assert settings.max_refs == 6
    assert settings.max_total_images == 9
    assert settings.concurrency == 8
    assert settings.retention_hours == 24
    assert settings.upload_retention_hours == 24
    assert settings.cost_per_image_cny == 0.0
    assert settings.ark_api_key == ""
    assert settings.is_fake_provider is True


def test_secret_default_is_flagged():
    settings = get_image_settings()
    assert settings.auth_secret == DEFAULT_IMAGE_AUTH_SECRET
    assert settings.secret_is_default is True

    custom = ImageSettings(auth_secret="x" * 32)
    assert custom.secret_is_default is False


def test_env_override(monkeypatch):
    monkeypatch.setenv("IMAGE_PROVIDER", "volcengine")
    monkeypatch.setenv("IMAGE_MAX_REFS", "4")
    monkeypatch.setenv("IMAGE_STORAGE", "cos")
    reset_image_settings()
    settings = get_image_settings()
    assert settings.provider == "volcengine"
    assert settings.max_refs == 4
    assert settings.storage == "cos"
    assert settings.is_fake_provider is False


def test_max_refs_is_capped_at_six():
    with pytest.raises(Exception):
        ImageSettings(max_refs=7)


def test_images_dir_layout():
    settings = get_image_settings()
    assert settings.uploads_dir.name == "uploads"
    assert settings.outputs_dir.name == "outputs"
    assert settings.thumbs_dir.name == "thumbnails"
    assert settings.images_dir.name == "images"
