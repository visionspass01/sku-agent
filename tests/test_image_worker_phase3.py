"""第十二轮 Phase 3：数据中台"结果图片落库 + 签名读取"与 agent 侧 platform 存储打通。

覆盖点：
- `POST /api/v1/image/results/{task_id}/{slot}/binary`：鉴权、字节嗅探、上限 413、非图片 415、
  `(task_id, slot)` 幂等（同内容覆盖同一 key）、行上写 storage/storage_key/storage_url_expire_at；
- `GET /api/v1/image/results/assets/{key}`：短签名可读、篡改/过期 403、无签名要求 Bearer；
- `PlatformStorage`（worker 侧）调它并拿到 `storage=platform` + 可读 URL；
- agent 侧 `read_asset_bytes` 能代读 platform 存储里的图片（前端不感知落点）。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.agents_image.png import placeholder_png
from app.agents_image.worker.storage import PlatformStorage

PNG = placeholder_png("phase3-platform", 64, 64)


@pytest.fixture(scope="module")
def platform_env(tmp_path_factory):
    from tests.image_platform_support import init_platform_db, reset_platform_engine

    tmp = tmp_path_factory.mktemp("platform_upload")
    init_platform_db(tmp)
    yield tmp
    reset_platform_engine()


@pytest.fixture()
def platform_api(platform_env):
    from tests.image_platform_support import platform_client

    with platform_client() as client:
        yield client


def _upload(client, task_id="20261121030303p001", slot=1, data=PNG, **headers):
    from tests.image_platform_support import TOKEN

    return client.post(f"/api/v1/image/results/{task_id}/{slot}/binary",
                       content=data, headers={**TOKEN, **headers})


# ---------------------------------------------------------------- 上传
def test_upload_requires_token(platform_api):
    resp = platform_api.post("/api/v1/image/results/t-sk/1/binary", content=PNG)
    assert resp.status_code == 401


def test_upload_stores_and_returns_path(platform_api):
    resp = _upload(platform_api, task_id="20261121030303p002", slot=1)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["storage"] == "platform"
    assert body["storage_key"].startswith("results/20261121030303p002/slot1-")
    assert body["sha256"] and body["bytes"] == len(PNG)
    assert body["width"] == 64 and body["height"] == 64
    assert body["url"].startswith("/api/v1/image/results/assets/")
    assert "sig=" in body["url"]


def test_upload_is_idempotent_per_task_slot(platform_api):
    first = _upload(platform_api, task_id="20261121030303p003", slot=2).json()
    second = _upload(platform_api, task_id="20261121030303p003", slot=2).json()
    assert first["storage_key"] == second["storage_key"]        # 同内容同 key
    from sqlalchemy import select

    from data_platform.db import ImageResult, session_scope

    with session_scope() as session:
        rows = session.execute(select(ImageResult).where(
            ImageResult.task_id == "20261121030303p003")).scalars().all()
        snapshot = [(row.storage, row.storage_key) for row in rows]
    assert len(snapshot) == 1                                  # 同一行 upsert
    assert snapshot[0][0] == "platform" and snapshot[0][1]


def test_upload_rejects_non_image_and_oversize(platform_api):
    assert _upload(platform_api, task_id="20261121030303p004",
                   data=b"not-an-image").status_code == 415
    huge = b"\x89PNG\r\n\x1a\n" + b"0" * (9 * 1024 * 1024)
    assert _upload(platform_api, task_id="20261121030303p004",
                   data=huge).status_code == 413


# ---------------------------------------------------------------- 读取（签名）
def test_signed_url_serves_bytes_and_rejects_tampering(platform_api):
    body = _upload(platform_api, task_id="20261121030303p005", slot=3).json()
    url = body["url"]
    ok = platform_api.get(url)
    assert ok.status_code == 200 and ok.content == PNG
    assert ok.headers["content-type"].startswith("image/png")
    assert platform_api.get(url.replace("sig=", "sig=deadbeef")).status_code == 403
    exp = int(time.time()) + 60
    tampered = f"{url.split('?')[0]}?exp={exp}&sig=deadbeef"
    assert platform_api.get(tampered).status_code == 403


def test_unsigned_read_requires_token(platform_api):
    body = _upload(platform_api, task_id="20261121030303p006", slot=4).json()
    path = body["url"].split("?")[0]
    assert platform_api.get(path).status_code == 401
    from tests.image_platform_support import TOKEN

    assert platform_api.get(path, headers=TOKEN).status_code == 200


# ---------------------------------------------------------------- worker PlatformStorage
def test_platform_storage_put_and_get(platform_env, monkeypatch):
    import httpx

    from app.agents_image import config as image_config
    from tests.image_platform_support import platform_transport

    real_client = httpx.AsyncClient

    def fake_client(**kwargs):
        kwargs.pop("base_url", None)
        return real_client(transport=platform_transport(),
                           base_url="http://platform.test", **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    monkeypatch.setenv("IMAGE_PLATFORM_URL", "http://platform.test")
    image_config.reset_image_settings()
    storage = PlatformStorage()
    obj = storage.put(PNG, kind="output", mime="image/png",
                      task_id="20261121030303p007", slot=1)
    assert obj.storage == "platform"
    assert obj.key.startswith("results/20261121030303p007/slot1-")
    assert obj.url.startswith("http://platform.test/api/v1/image/results/assets/")
    assert asyncio.run(storage.get_async(obj.url)) == PNG
    image_config.reset_image_settings()


# ---------------------------------------------------------------- agent 代读
def test_agent_can_read_platform_asset(tmp_path, monkeypatch, _db):
    import httpx

    from app.agents_image import config as image_config
    from app.agents_image.store import assets as asset_store
    from app.agents_image.store.schema import ensure_image_schema
    from app.db.engine import session_scope
    from tests.image_platform_support import platform_transport

    real_client = httpx.AsyncClient

    def fake_client(**kwargs):
        kwargs.pop("base_url", None)
        return real_client(transport=platform_transport(),
                           base_url="http://platform.test", **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    monkeypatch.setenv("IMAGE_PLATFORM_URL", "http://platform.test")
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    image_config.reset_image_settings()
    ensure_image_schema()
    storage = PlatformStorage()
    obj = storage.put(PNG, kind="output", mime="image/png",
                      task_id="20261121030303p008", slot=1)
    with session_scope() as session:
        asset = asset_store.register_remote(
            session, task_id="20261121030303p008", slot=1, sha256=obj.sha256,
            storage=obj.storage, key=obj.key, mime=obj.mime, width=obj.width,
            height=obj.height, size_bytes=obj.bytes, username="admin")
        asset_id = asset.asset_id
    with session_scope() as session:
        again = asset_store.get_asset(session, asset_id)
        assert asset_store.read_asset_bytes(again) == PNG
    image_config.reset_image_settings()
