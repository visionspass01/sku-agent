"""资产表读写：落盘（storage 插件）+ 元数据入库 + 去重 + 读取。

v6 起资产只有三类：`ref`（中台商品图落地）、`output`（出图）、`thumb`（缩略图）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.agents_image.storage.image_storage import get_storage
from app.agents_image.store.models import ImageAsset
from app.core.errors import raise_image


def save_bytes(session, data: bytes, *, kind: str, mime: str = "",
               task_id: str | None = None, slot: int = 0,
               username: str = "anonymous", department: str | None = None,
               product_id: str | None = None, color_key: str = "",
               source: str = "mcp", blocked_reason: str = "") -> ImageAsset:
    """落盘并入库；参考图按 `(sha256, 账号, 商品)` 去重，产出按 task 独立成行。"""
    storage = get_storage()
    ref = storage.put(data, kind=kind, mime=mime)
    # ref 去重：同账号重复解析同一商品的同一张图，复用已有行（不重复占盘，也不跨账号串号）；
    # 素材库（第三轮 P2-10）按 (sha256, 账号) 去重：同一张图重复上传不该占两次额度；
    # output 必须按 task 独立成行，否则两次任务产出同样字节会被并成一条、任务查不到自己的图。
    existing = None
    if kind == "ref":
        existing = session.execute(
            select(ImageAsset).where(ImageAsset.sha256 == ref.sha256,
                                     ImageAsset.kind == kind,
                                     ImageAsset.username == username,
                                     ImageAsset.product_id == product_id,
                                     ImageAsset.color_key == (color_key or ""))
        ).scalars().first()
    elif kind == "library":
        existing = session.execute(
            select(ImageAsset).where(ImageAsset.sha256 == ref.sha256,
                                     ImageAsset.kind == kind,
                                     ImageAsset.username == username)
        ).scalars().first()
    if existing is not None:
        return existing
    asset = ImageAsset(
        asset_id=uuid.uuid4().hex, task_id=task_id, slot=slot,
        username=username, department=department, product_id=product_id,
        color_key=color_key or "",
        blocked_reason=blocked_reason or "",
        kind=kind, source=source, sha256=ref.sha256,
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


def list_refs(session, task_id: str) -> list[ImageAsset]:
    """任务的参考图（v6 由中台商品图落下）。

    注意：ref 资产是**按商品+账号共享**的（同一商品重复解析复用同一行），所以不能靠
    `asset.task_id` 反查某次任务用了哪些图——任务详情请用 `get_assets(task.ref_asset_ids)`。
    """
    rows = session.execute(
        select(ImageAsset).where(ImageAsset.task_id == task_id,
                                 ImageAsset.kind == "ref")
    ).scalars().all()
    return list(rows)


def get_assets(session, asset_ids: list[str]) -> list[ImageAsset]:
    """按 id 列表取资产（保持传入顺序，忽略不存在的）。"""
    if not asset_ids:
        return []
    rows = session.execute(
        select(ImageAsset).where(ImageAsset.asset_id.in_(list(asset_ids)))
    ).scalars().all()
    by_id = {row.asset_id: row for row in rows}
    return [by_id[item] for item in asset_ids if item in by_id]


def bind_refs_to_task(session, asset_ids: list[str], task_id: str) -> int:
    """把解析出来的 ref 资产（建任务前落库、`task_id` 为空）挂到具体任务上。"""
    bound = 0
    for asset_id in asset_ids:
        asset = session.get(ImageAsset, asset_id)
        if asset is None or asset.kind != "ref":
            continue
        if asset.task_id in (None, ""):
            asset.task_id = task_id
            bound += 1
    if bound:
        session.flush()
    return bound


def read_asset_bytes(asset: ImageAsset) -> bytes:
    """读取资产内容（本地存储 / COS 同接口）。"""
    return get_storage(asset.storage).get(asset.key)


def require_asset(session, asset_id: str) -> ImageAsset:
    asset = get_asset(session, asset_id)
    if asset is None:
        raise_image("IMG_TASK_NOT_FOUND", f"图片不存在：{asset_id}")
    return asset


# ---------------------------------------------------------------- 素材库（W3）
def list_library(session, *, username: str | None = None, limit: int = 100) -> list[ImageAsset]:
    """素材库列表：`username=None` 表示管理员视角（全部账号），否则只看本人。"""
    statement = select(ImageAsset).where(ImageAsset.kind == "library")
    if username:
        statement = statement.where(ImageAsset.username == username)
    statement = statement.order_by(ImageAsset.created_at.desc()).limit(max(1, int(limit)))
    return list(session.execute(statement).scalars().all())


def count_library(session, username: str) -> int:
    from sqlalchemy import func

    return int(session.execute(
        select(func.count()).select_from(ImageAsset)
        .where(ImageAsset.kind == "library", ImageAsset.username == username)
    ).scalar_one())


def find_library_by_sha(session, username: str, data: bytes) -> ImageAsset | None:
    """素材库按内容去重（第三轮 P2-10）：同一张图重复上传返回已有行。"""
    from app.core.artifacts import sha256_bytes

    digest = sha256_bytes(data)
    return session.execute(
        select(ImageAsset).where(ImageAsset.sha256 == digest,
                                 ImageAsset.kind == "library",
                                 ImageAsset.username == username)
    ).scalars().first()


def delete_asset(session, asset: ImageAsset) -> None:
    """删除素材图：先删文件、再删行（文件已丢也不影响元数据清理）。"""
    try:
        get_storage(asset.storage).delete(asset.key)
    except Exception:  # noqa: BLE001 —— 文件已丢/存储异常都不阻断元数据清理
        pass
    session.delete(asset)
    session.flush()
