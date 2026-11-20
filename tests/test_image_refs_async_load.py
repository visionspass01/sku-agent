"""第八轮 P2-4：参考图字节读取必须在线程池里跑（不能阻塞事件循环）。

`execute_task` 过去在事件循环内同步做「资产表查询 + 存储读盘」；8 个并发任务同时进
执行体时，单个任务读盘会把整个 loop 卡住（SSE 心跳/取消检查/其它任务全被拖后）。
现在走 `graph._load_refs_async()`（`asyncio.to_thread`，线程内自建会话）。
"""

from __future__ import annotations

import asyncio
import inspect

from app.agents_image import graph
from app.agents_image.png import placeholder_png
from app.agents_image.store import assets as asset_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


def test_load_refs_async_is_coroutine_and_reads_bytes(tmp_path, monkeypatch, _db):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    from app.agents_image.config import reset_image_settings

    reset_image_settings()
    ensure_image_schema()
    with session_scope() as session:
        ref = asset_store.save_bytes(session, placeholder_png("async-ref", 32, 32),
                                     kind="ref", product_id="P-3001", username="admin")
        ref_id, ref_sha = ref.asset_id, ref.sha256

    assert inspect.iscoroutinefunction(graph._load_refs_async), "必须异步（丢线程池）"
    refs = asyncio.run(graph._load_refs_async([ref_id]))
    assert refs[0]["asset_id"] == ref_id
    assert refs[0]["sha256"] == ref_sha
    assert refs[0]["data"].startswith(b"\x89PNG")


def test_load_refs_async_reports_missing_asset(tmp_path, monkeypatch, _db):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    from app.agents_image.config import reset_image_settings
    from app.core.errors import ImageError

    reset_image_settings()
    ensure_image_schema()
    try:
        asyncio.run(graph._load_refs_async(["missing-asset"]))
    except ImageError as exc:
        assert exc.code == "IMG_REFERENCE_INVALID"
    else:  # pragma: no cover
        raise AssertionError("缺失参考图必须报 IMG_REFERENCE_INVALID")
