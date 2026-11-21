"""资产表读写：落盘（storage 插件）+ 元数据入库 + 去重 + 读取。

v6 起资产只有三类：`ref`（中台商品图落地）、`output`（出图）、`thumb`（缩略图）。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app.agents_image.storage.image_storage import get_storage
from app.agents_image.store.models import ImageAsset
from app.core.errors import raise_image

logger = logging.getLogger("sku-agent.image.assets")


def _dedupe_key(kind: str, sha256: str, username: str, product_id: str | None,
                color_key: str) -> str | None:
    """参考图/素材图的去重键（第五轮 P2）：并发下由唯一索引兜底，不再只靠"先查后写"。"""
    if kind not in {"ref", "library"}:
        return None
    return f"{kind}|{sha256}|{username}|{product_id or ''}|{color_key or ''}"[:200]


def save_bytes(session, data: bytes, *, kind: str, mime: str = "",
               task_id: str | None = None, slot: int = 0,
               username: str = "anonymous", department: str | None = None,
               product_id: str | None = None, color_key: str = "",
               source: str = "mcp", blocked_reason: str = "") -> ImageAsset:
    """落盘并入库；参考图/素材图按去重键幂等，产出按 task 独立成行。

    去重分两层（第五轮 P2）：
    1. 先查（便宜路径，绝大多数命中走这里）；
    2. 插入时用 `dedupe_key` 唯一索引兜底：并发两个请求同时写同一张图时，
       后到的会撞唯一约束 → 回滚这一条并复用已存在行（savepoint 不污染外层事务）。
    """
    storage = get_storage()
    ref = storage.put(data, kind=kind, mime=mime)
    key = _dedupe_key(kind, ref.sha256, username, product_id, color_key)
    if key:
        existing = session.execute(
            select(ImageAsset).where(ImageAsset.dedupe_key == key)
        ).scalars().first()
        if existing is not None:
            return existing
    asset = ImageAsset(
        asset_id=uuid.uuid4().hex, task_id=task_id, slot=slot,
        username=username, department=department, product_id=product_id,
        color_key=color_key or "",
        blocked_reason=blocked_reason or "",
        kind=kind, source=source, sha256=ref.sha256, dedupe_key=key,
        storage=ref.storage, key=ref.key, mime=ref.mime,
        width=ref.width, height=ref.height, bytes=ref.bytes,
    )
    if key:
        from sqlalchemy.exc import IntegrityError

        try:
            with session.begin_nested():        # SAVEPOINT：撞唯一索引只回滚这一条
                session.add(asset)
                session.flush()
            return asset
        except IntegrityError:
            hit = session.execute(
                select(ImageAsset).where(ImageAsset.dedupe_key == key)
            ).scalars().first()
            if hit is not None:
                return hit
            raise
    session.add(asset)
    session.flush()
    return asset


def register_remote(session, *, task_id: str | None, slot: int, sha256: str, storage: str,
                    key: str, mime: str = "", width: int = 0, height: int = 0,
                    size_bytes: int = 0, username: str = "anonymous",
                    department: str | None = None, product_id: str | None = None,
                    color_key: str = "", source: str = "mcp", kind: str = "output",
                    blocked_reason: str = "") -> ImageAsset:
    """登记一张**已经在远端存储里**的产出（COS / 中台文件夹），不再写本地文件。

    第十二轮：worker 独立运行后，图片由 worker 直接落 COS/中台并把路径交回来；
    父进程只做"资产行登记 + 回写中台"，本地 `data/images/outputs` 不再新增文件。
    """
    asset = ImageAsset(
        asset_id=uuid.uuid4().hex, task_id=task_id, slot=slot,
        username=username, department=department, product_id=product_id,
        color_key=color_key or "", blocked_reason=(blocked_reason or "")[:60],
        kind=kind, source=source, sha256=sha256, dedupe_key=None,
        storage=storage, key=key, mime=mime, width=int(width or 0),
        height=int(height or 0), bytes=int(size_bytes or 0),
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
    except Exception as exc:  # noqa: BLE001 —— 文件已丢/存储异常都不阻断元数据清理
        from app.agents_image import metrics as image_metrics

        image_metrics.record_swallowed("delete_asset_file")
        logging.getLogger("sku-agent.image.assets").warning(
            "删除素材文件失败（行仍会删）：asset=%s %s", asset.asset_id, exc)
    session.delete(asset)
    session.flush()
