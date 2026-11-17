"""本地存储：落盘/读回、尺寸解析、去重、24h 清理、非法类型拒绝。"""

from __future__ import annotations

import datetime as dt
import struct
import zlib

import pytest

from app.agents_image.config import reset_image_settings
from app.agents_image.storage import image_storage as storage_contract
from app.agents_image.storage.local_store import LocalStore
from app.core.errors import ImageError


def make_png(width: int = 32, height: int = 32) -> bytes:
    """造一张合法 PNG（纯标准库），用于上传/存储测试。"""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes([200, 180, 160] * width) for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(
        b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.delenv("IMAGE_STORAGE", raising=False)
    reset_image_settings()
    yield LocalStore()
    reset_image_settings()


def test_registered_and_named():
    assert "local" in storage_contract.storage_names()
    assert storage_contract.get_storage("local").name == "local"


def test_put_get_roundtrip(store):
    png = make_png(40, 24)
    ref = store.put(png, kind="upload", mime="image/png")
    assert ref.mime == "image/png"
    assert (ref.width, ref.height) == (40, 24)
    assert ref.bytes == len(png)
    assert store.get(ref.key) == png
    assert store.signed_url(ref.key, asset_id="a1") == "/api/image/assets/a1"


def test_same_content_dedupes_to_same_key(store):
    png = make_png()
    first = store.put(png, kind="upload", mime="image/png")
    second = store.put(png, kind="upload", mime="image/png")
    assert first.key == second.key
    assert first.sha256 == second.sha256


def test_rejects_non_image(store):
    with pytest.raises(ImageError) as exc:
        store.put(b"not an image", kind="upload", mime="text/plain")
    assert exc.value.code == "IMG_REFERENCE_INVALID"


def test_sweep_expired_removes_old_files(store, monkeypatch):
    png = make_png()
    ref = store.put(png, kind="output", mime="image/png")
    path = store._path(ref.key)
    old = dt.datetime.now() - dt.timedelta(hours=25)
    import os

    os.utime(path, (old.timestamp(), old.timestamp()))
    removed = store.sweep_expired(hours=24)
    assert removed == 1
    assert not path.exists()


def test_sweep_keeps_fresh_files(store):
    store.put(make_png(), kind="output", mime="image/png")
    assert store.sweep_expired(hours=24) == 0


def test_health_reports_backend(store):
    health = store.health()
    assert health["ok"] is True and health["backend"] == "local"
