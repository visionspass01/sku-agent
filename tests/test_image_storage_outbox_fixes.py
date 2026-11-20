"""第八轮 P2-1 / P2-2：磁盘清理覆盖 blocked 目录；outbox 重放尊重 claim 结果。

- P2-1：`sweep_expired` 只扫 refs/outputs/thumbnails，而配置注释写明 `blocked/`
  （门禁拦截图）也参与 24h 清理 —— 结果拦截图永久占盘。
- P2-2：`replay()` 拿到 `claim()` 的返回值却丢弃，没抢到占位的行仍会被重发。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import pathlib
import time

import pytest

import app.agents_image.store.outbox as outbox_store
from app.agents_image.config import reset_image_settings
from app.agents_image.storage.local_store import LocalStore
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


@pytest.fixture()
def images_root(tmp_path, monkeypatch, _db):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    reset_image_settings()
    ensure_image_schema()
    yield pathlib.Path(tmp_path / "images")
    reset_image_settings()


def _touch(directory: pathlib.Path, name: str, *, age_hours: float = 48.0) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    old = time.time() - age_hours * 3600
    os.utime(path, (old, old))
    return path


def test_sweep_expired_removes_stale_blocked_images(images_root):
    """P2-1：blocked/ 也要按保留期清理（否则拦截图永久占盘）。"""
    store = LocalStore()
    blocked_old = _touch(images_root / "blocked", "old.png")
    blocked_fresh = _touch(images_root / "blocked", "fresh.png", age_hours=0.1)
    output_old = _touch(images_root / "outputs", "old.png")
    library_keep = _touch(images_root / "library", "keep.png", age_hours=1000)

    removed = store.sweep_expired(24)
    assert removed >= 2, removed
    assert not blocked_old.exists(), "过期拦截图必须被清理"
    assert blocked_fresh.exists(), "未过期的拦截图要保留"
    assert not output_old.exists()
    assert library_keep.exists(), "素材库是人工资产，不参与自动清理"


class _Catalog:
    name = "stub"

    def __init__(self):
        self.calls: list[str] = []

    async def save_result(self, payload: dict) -> dict:
        self.calls.append(str((payload or {}).get("task_id") or ""))
        return {"saved": True}


def test_replay_respects_claimed_rows(images_root, monkeypatch):
    """P2-2：没抢到占位（claim 未返回）的行不能被重发。"""
    monkeypatch.setenv("IMAGE_OUTBOX_CLAIM_SECONDS", "120")
    reset_image_settings()
    with session_scope() as session:
        first = outbox_store.enqueue(session, task_id="task-claimed", payload={"task_id": "task-claimed"},
                                     error="x")
        outbox_store.enqueue(session, task_id="task-taken", payload={"task_id": "task-taken"},
                             error="x")
        first_id = int(first.id)
    # 模拟"别的副本刚抢到第二条"：claim 只返回第一条
    monkeypatch.setattr(outbox_store, "claim",
                        lambda ids, **kwargs: [first_id] if first_id in [int(i) for i in ids] else [])

    catalog = _Catalog()
    with session_scope() as session:
        for row in session.query(outbox_store.ImageOutbox).all():
            row.next_retry_at = dt.datetime.now() - dt.timedelta(seconds=1)
    result = asyncio.run(outbox_store.replay(catalog=catalog, limit=10))
    assert catalog.calls == ["task-claimed"], catalog.calls
    assert result["sent"] == 1, result
    assert result["skipped"] >= 1, result
