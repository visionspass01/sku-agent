"""第五轮 W8：三项 P2（buckets 窗口下推 / admin 指标缓存 / assets 唯一索引去重）。

- `/admin/metrics`：折线桶只读窗口内的小时行（不再整表 ORM 加载），并加 30s 进程内缓存；
- `image_usage`：新增 (granularity, period_key) 与 (department, period_key) 索引；
- `image_asset`：参考图/素材图按 `dedupe_key` 唯一索引去重（并发安全），
  output/blocked 的 dedupe_key 为 NULL（仍各自成行）。
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import select

from app.agents_image import metrics as image_metrics
from app.agents_image.api import admin_routes
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.png import placeholder_png
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import assets as asset_store
from app.agents_image.store import usage as usage_store
from app.agents_image.store import users as user_store
from app.agents_image.store.models import ImageAsset
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
    admin_routes.reset_metrics_cache()
    with session_scope() as session:
        user_store.ensure_seed_users(session)
    yield app_client
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()
    admin_routes.reset_metrics_cache()


def login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


# ---------------------------------------------------------------- buckets 窗口下推
def test_buckets_only_reads_rows_inside_window(_db):
    """窗口外（一年前）的小时行不该被读进来：结果里不会出现它，且查询带 period_key 下界。"""
    ensure_image_schema()
    import datetime as dt

    old = dt.datetime(2020, 1, 1, 10, 5)
    now = dt.datetime(2026, 11, 20, 12, 0)
    with session_scope() as session:
        usage_store.record(session, username="u-window", delivered=5, tasks=1, now=old)
        usage_store.record(session, username="u-window", delivered=2, tasks=1, now=now)

        captured = {}
        real_execute = session.execute

        def spy(statement, *args, **kwargs):
            captured["sql"] = str(statement)
            return real_execute(statement, *args, **kwargs)

        session.execute = spy
        try:
            buckets = usage_store.buckets(session, granularity="day", count=7, now=now,
                                          username="u-window")
        finally:
            session.execute = real_execute

    assert "period_key >=" in captured["sql"].replace("\n", " "), captured["sql"]
    total = sum(bucket["images"] for bucket in buckets)
    assert total == 2, "窗口外的历史行不该被算进来"


# ---------------------------------------------------------------- admin/metrics 缓存
def test_admin_metrics_is_cached_and_refresh_bypasses(env):
    headers = login(env)
    first = env.get("/api/image/admin/metrics?granularity=day&count=7", headers=headers).json()
    assert first["cached"] is False

    calls = {"buckets": 0}
    real_buckets = usage_store.buckets

    def spy(*args, **kwargs):
        calls["buckets"] += 1
        return real_buckets(*args, **kwargs)

    import app.agents_image.api.admin_routes as module

    module.usage_store.buckets = spy
    try:
        second = env.get("/api/image/admin/metrics?granularity=day&count=7",
                         headers=headers).json()
        assert second["cached"] is True
        assert calls["buckets"] == 0, "命中缓存不该再查库"

        refreshed = env.get("/api/image/admin/metrics?granularity=day&count=7&refresh=1",
                            headers=headers).json()
        assert refreshed["cached"] is False
        assert calls["buckets"] == 1
    finally:
        module.usage_store.buckets = real_buckets


def test_admin_metrics_cache_can_be_disabled_by_config(env, monkeypatch):
    monkeypatch.setenv("IMAGE_METRICS_CACHE_SECONDS", "0")
    reset_image_settings()
    admin_routes.reset_metrics_cache()
    headers = login(env)
    env.get("/api/image/admin/metrics?granularity=day&count=7", headers=headers)
    second = env.get("/api/image/admin/metrics?granularity=day&count=7", headers=headers).json()
    assert second["cached"] is False


# ---------------------------------------------------------------- assets 去重
def test_ref_assets_dedupe_by_key(_db):
    ensure_image_schema()
    data = placeholder_png("dedupe-ref", 32, 32)
    with session_scope() as session:
        first = asset_store.save_bytes(session, data, kind="ref", product_id="P-3001",
                                       color_key="P-3001#黑色", username="u-asset")
        first_id, first_key = first.asset_id, first.dedupe_key
        second = asset_store.save_bytes(session, data, kind="ref", product_id="P-3001",
                                        color_key="P-3001#黑色", username="u-asset")
        second_id = second.asset_id
        count = len(session.execute(
            select(ImageAsset).where(ImageAsset.username == "u-asset",
                                     ImageAsset.kind == "ref")).scalars().all())
    assert first_id == second_id
    assert count == 1
    assert first_key and first_key.startswith("ref|")


def test_output_assets_are_not_deduped(_db):
    ensure_image_schema()
    data = placeholder_png("dedupe-output", 32, 32)
    with session_scope() as session:
        first = asset_store.save_bytes(session, data, kind="output", task_id="t-a",
                                       slot=1, username="u-asset")
        second = asset_store.save_bytes(session, data, kind="output", task_id="t-b",
                                        slot=1, username="u-asset")
        first_id, second_id = first.asset_id, second.asset_id
        keys = (first.dedupe_key, second.dedupe_key)
    assert first_id != second_id, "同一字节在不同任务里必须各自成行"
    assert keys == (None, None)


def test_concurrent_ref_writes_keep_a_single_row(_db):
    """两个线程同时写同一张参考图：唯一索引兜底，最终只有一行（且不抛异常）。"""
    ensure_image_schema()
    data = placeholder_png("dedupe-race", 32, 32)
    errors: list[BaseException] = []
    ids: list[str] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            with session_scope() as session:
                asset = asset_store.save_bytes(session, data, kind="library",
                                               username="u-race")
                ids.append(asset.asset_id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    assert len(set(ids)) == 1, ids
    with session_scope() as session:
        rows = session.execute(
            select(ImageAsset).where(ImageAsset.username == "u-race",
                                     ImageAsset.kind == "library")).scalars().all()
    assert len(rows) == 1
