"""资产表读写：落盘（storage 插件）+ 元数据入库 + 去重 + 读取。"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.agents_image.storage.image_storage import get_storage
from app.agents_image.store.models import ImageAsset
from app.core.errors import raise_image


def save_bytes(session, data: bytes, *, kind: str, mime: str = "",
               task_id: str | None = None, slot: int = 0,
               username: str = "anonymous", department: str | None = None) -> ImageAsset:
    """落盘并入库；同 (sha256, kind) 已存在则复用已有资产行（不重复占盘、不重复计费）。"""
    storage = get_storage()
    ref = storage.put(data, kind=kind, mime=mime)
    # 上传件按内容去重（同一张图反复上传不重复占盘）；产出必须按 task 独立成行，
    # 否则两次任务产出同样字节（比如同 prompt 同 seed）会被并成一条、任务查不到自己的图。
    existing = None
    if kind == "upload":
        existing = session.execute(
            select(ImageAsset).where(ImageAsset.sha256 == ref.sha256, ImageAsset.kind == kind)
        ).scalars().first()
    if existing is not None:
        return existing
    asset = ImageAsset(
        asset_id=uuid.uuid4().hex, task_id=task_id, slot=slot,
        username=username, department=department, kind=kind, sha256=ref.sha256,
        storage=ref.storage, key=ref.key, mime=ref.mime,
        width=ref.width, height=ref.height, bytes=ref.bytes,
    )
    session.add(asset)
    session.flush()
    return asset


def get_asset(session, asset_id: str) -> ImageAsset | None:
    return session.get(ImageAsset, asset_id)


def list_outputs(session, task_id: str) -> list[ImageAsset]:
    rows = session.execute(
        select(ImageAsset).where(ImageAsset.task_id == task_id,
                                 ImageAsset.kind == "output").order_by(ImageAsset.slot)
    ).scalars().all()
    return list(rows)


def read_asset_bytes(asset: ImageAsset) -> bytes:
    """读取资产内容（本地存储 / COS 同接口）。"""
    return get_storage(asset.storage).get(asset.key)


def require_asset(session, asset_id: str) -> ImageAsset:
    asset = get_asset(session, asset_id)
    if asset is None:
        raise_image("IMG_TASK_NOT_FOUND", f"图片不存在：{asset_id}")
    return asset
