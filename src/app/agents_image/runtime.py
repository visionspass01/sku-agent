"""生图 Agent 运行期钩子：建表、健康上报、24h 清理任务（不 import SKU 业务包）。"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import shutil

from app.agents_image.config import get_image_settings
from app.agents_image.store.schema import ensure_image_schema
from app.core import health as health_registry

logger = logging.getLogger("sku-agent.image.runtime")

_cleanup_task: asyncio.Task | None = None
_recovery_task: asyncio.Task | None = None
_outbox_task: asyncio.Task | None = None


def health_block() -> dict:
    """健康段：报"当前生效的形态" + 库结构是否降级（第三轮 P2-4）。"""
    from app.agents_image.store.schema import schema_status

    settings = get_image_settings()
    status = schema_status()
    return {
        "enabled": bool(settings.enabled),
        "provider": settings.provider,
        "product_source": settings.product_source,
        "storage": settings.storage,
        "dispatcher": settings.dispatcher,
        "concurrency": settings.concurrency,
        "max_refs": settings.max_refs,
        "secret_is_default": settings.secret_is_default,
        "degraded": bool(status.get("degraded")),
        "degraded_reason": "；".join(status.get("issues") or [])[:300],
        "tasks_pending": 0,
        "in_flight_images": 0,
    }


def sweep_local_files(now: dt.datetime | None = None) -> dict:
    """删除本地存储里过期的参考图/产出（本地存储 = 24h 保留），并**同步标记资产行**。

    P1-4：只删文件不删资产行会造成"任务详情里有一张图、打开 404"的不一致，
    这里把对应 `image_asset` 行标 `purged_at`（保留审计线索，接口返回 purged=true）。
    """
    settings = get_image_settings()
    if settings.storage.strip().lower() != "local":
        return {"removed": 0, "purged": 0, "skipped": "storage!=local"}
    reference = now or dt.datetime.now()
    cutoff = reference - dt.timedelta(hours=settings.retention_hours)
    removed = 0
    purged_keys: list[str] = []
    for directory in (settings.refs_dir, settings.outputs_dir, settings.blocked_dir):
        if not directory.exists():
            continue
        for path in directory.glob("*"):
            if not path.is_file():
                continue
            try:
                mtime = dt.datetime.fromtimestamp(path.stat().st_mtime)
            except OSError:
                continue
            if mtime < cutoff:
                try:
                    path.unlink()
                    removed += 1
                    purged_keys.append(f"{directory.name}/{path.name}")
                except OSError:  # pragma: no cover —— 权限/占用
                    logger.warning("清理文件失败：%s", path)
    purged = _mark_purged(purged_keys, reference) if purged_keys else 0
    return {"removed": removed, "purged": purged}


def _mark_purged(keys: list[str], moment: dt.datetime) -> int:
    """把已删文件的资产行标 purged_at（按 storage key 匹配）。"""
    try:
        from sqlalchemy import update

        from app.agents_image.store.models import ImageAsset
        from app.db.engine import session_scope

        with session_scope() as session:
            result = session.execute(
                update(ImageAsset)
                .where(ImageAsset.key.in_(keys), ImageAsset.purged_at.is_(None))
                .values(purged_at=moment)
            )
            return int(result.rowcount or 0)
    except Exception as exc:  # noqa: BLE001 —— 标记失败不影响清理
        logger.warning("标记已清理资产失败：%s", exc)
        return 0


def sweep_tokens(now: dt.datetime | None = None) -> int:
    """清理过期的令牌黑名单（P1-8：以前这个方法没有任何调用点，表只增不减）。"""
    try:
        from app.agents_image.store import tokens as token_store
        from app.db.engine import session_scope

        with session_scope() as session:
            return token_store.sweep(session, now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理令牌黑名单失败：%s", exc)
        return 0


async def _outbox_loop(interval_seconds: int, batch: int) -> None:
    """回写补偿队列的定时重放（第三轮 P1-14）：不再只靠管理员手动点"重放回写"。

    第四轮 P1-5：`replay()` 自己管短会话，网络 IO 不再包在 `session_scope` 里。
    """
    from app.agents_image.store import outbox as outbox_store

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            result = await outbox_store.replay(limit=batch)
            if result.get("sent") or result.get("failed"):
                logger.info("outbox 重放：sent=%s failed=%s",
                            result.get("sent"), result.get("failed"))
        except asyncio.CancelledError:  # pragma: no cover —— 关机时正常退出
            raise
        except Exception as exc:  # noqa: BLE001 —— 重放失败不影响服务
            logger.warning("outbox 重放异常：%s", exc)


async def _cleanup_loop(interval_seconds: int) -> None:
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            result = sweep_local_files()
            expired_tokens = sweep_tokens()
            # P2-7：已回写（sent）的 outbox 行按保留期清理，否则表只涨不降
            from app.observability.trace import run_in_trace

            pruned = await run_in_trace(_prune_outbox)
            # P1-4：修复经验按 TTL 清理（人工采纳/驳回样本不动）
            pruned_memory = await run_in_trace(_prune_memory)
            # P2-4（第九轮）：幂等表 image_request（含完整响应体）按保留期清理
            pruned_requests = await run_in_trace(_prune_requests)
            if (result.get("removed") or expired_tokens or pruned or pruned_memory
                    or pruned_requests):
                logger.info("本地清理：文件 %s 个，令牌 %s 条，outbox %s 条，"
                            "修复经验 %s 条，幂等记录 %s 条",
                            result.get("removed"), expired_tokens, pruned, pruned_memory,
                            pruned_requests)
        except asyncio.CancelledError:  # pragma: no cover —— 关机时正常退出
            raise
        except Exception as exc:  # noqa: BLE001 —— 清理失败不影响服务
            logger.warning("图片清理任务异常：%s", exc)


def _prune_outbox() -> int:
    try:
        from app.agents_image.store import outbox as outbox_store

        return outbox_store.prune()
    except Exception as exc:  # noqa: BLE001 —— 清理失败不影响其它清理
        logger.warning("outbox 清理失败：%s", exc)
        return 0


def _prune_requests() -> int:
    """幂等表清理（P2-4）：done 行按保留期、pending 行按滞留期删除。"""
    try:
        from app.agents_image.store import requests as request_store

        return request_store.prune()
    except Exception as exc:  # noqa: BLE001
        logger.warning("幂等表清理失败：%s", exc)
        return 0


def _prune_memory() -> int:
    """修复经验的 TTL 清理（P1-4）：只清 repair/auto，人工样本永远保留。"""
    try:
        from app.agents_image.config import get_image_settings
        from app.agents_image.store import memory as memory_store
        from app.db.engine import session_scope

        keep_days = int(getattr(get_image_settings(), "memory_keep_days", 90) or 90)
        with session_scope() as session:
            return memory_store.prune(session, keep_days=keep_days)
    except Exception as exc:  # noqa: BLE001 —— 清理失败不影响其它清理
        logger.warning("修复经验清理失败：%s", exc)
        return 0


async def _recovery_loop(interval_seconds: int) -> None:
    """看门狗（P1-5）：周期性重新拾起 queued / 租约过期的 running 任务。

    之前只在启动时扫一次：运行期崩溃留下的 running（启动后 60s 内的）永远不会被恢复。
    """
    from app.agents_image.dispatch.task_dispatcher import get_dispatcher

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            resumed = await get_dispatcher().resume_pending()
            if resumed:
                logger.info("看门狗续跑任务：%d 个（%s）", len(resumed), resumed[:5])
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception as exc:  # noqa: BLE001 —— 续跑失败不影响服务
            logger.warning("看门狗续跑异常：%s", exc)


async def startup() -> None:
    settings = get_image_settings()
    if not settings.enabled:
        logger.info("Image Agent 已通过 IMAGE_ENABLED=false 关闭")
        return
    result = ensure_image_schema()
    if result["created"]:
        logger.info("image_* 表已就绪：%s", result["created"])
    # 共享人员账号：预置 3 个演示账号（幂等，已存在不覆盖密码），并把旧 image_user 搬过来
    try:
        from app.agents_image.store.users import ensure_seed_users, migrate_legacy_users
        from app.db.engine import session_scope

        with session_scope() as session:
            created_users = ensure_seed_users(session)
            migrated = migrate_legacy_users(session)
        if created_users:
            logger.info("共享账号已预置：%s", created_users)
        if migrated:
            logger.info("旧 image_user 已迁移到共享 user 表：%d 个", migrated)
    except Exception as exc:  # noqa: BLE001 —— 账号预置失败不阻塞启动
        logger.warning("账号预置失败：%s", exc)
    if settings.secret_is_default:
        logger.warning("IMAGE_AUTH_SECRET 仍是默认值，上线前务必改掉（当前令牌可被伪造）")
    health_registry.register("image", health_block)
    if settings.storage.strip().lower() == "local":
        for directory in (settings.refs_dir, settings.outputs_dir, settings.blocked_dir):
            directory.mkdir(parents=True, exist_ok=True)
    global _cleanup_task, _recovery_task, _outbox_task
    if settings.cleanup_interval_minutes > 0:
        _cleanup_task = asyncio.create_task(
            _cleanup_loop(settings.cleanup_interval_minutes * 60),
            name="image-cleanup",
        )
    if settings.recovery_interval_seconds > 0:
        _recovery_task = asyncio.create_task(
            _recovery_loop(settings.recovery_interval_seconds),
            name="image-recovery",
        )
    if getattr(settings, "outbox_replay_seconds", 0) > 0:
        _outbox_task = asyncio.create_task(
            _outbox_loop(settings.outbox_replay_seconds,
                         int(getattr(settings, "outbox_replay_batch", 50) or 50)),
            name="image-outbox",
        )
    # 无状态续跑：进程重启后，queued / 超时 running 的任务重新提交
    try:
        from app.agents_image.dispatch.task_dispatcher import get_dispatcher

        resumed = await get_dispatcher().resume_pending()
        if resumed:
            logger.info("生图任务续跑：%d 个", len(resumed))
    except Exception as exc:  # noqa: BLE001 —— 续跑失败不该阻塞启动
        logger.warning("生图任务续跑失败：%s", exc)
    logger.info("Image Agent 已启动：provider=%s storage=%s dispatcher=%s",
                settings.provider, settings.storage, settings.dispatcher)


async def _stop_task(task) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


async def shutdown() -> None:
    global _cleanup_task, _recovery_task, _outbox_task
    for name in ("_cleanup_task", "_recovery_task", "_outbox_task"):
        task = globals().get(name)
        await _stop_task(task)
        globals()[name] = None
    health_registry.unregister("image")


def reset_for_tests() -> None:
    """测试用：清掉进程内状态（后台任务在测试里不启动）。"""
    global _cleanup_task, _recovery_task, _outbox_task
    for name in ("_cleanup_task", "_recovery_task", "_outbox_task"):
        task = globals().get(name)
        if task is not None:
            task.cancel()
        globals()[name] = None
    health_registry.unregister("image")
    shutil.rmtree(get_image_settings().images_dir, ignore_errors=True)
