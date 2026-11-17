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


def health_block() -> dict:
    """健康段：只报"当前生效的形态"，依赖健康（provider/storage 探测）后续里程碑再补。"""
    settings = get_image_settings()
    return {
        "enabled": bool(settings.enabled),
        "provider": settings.provider,
        "product_source": settings.product_source,
        "vision_provider": settings.vision_provider,
        "storage": settings.storage,
        "dispatcher": settings.dispatcher,
        "concurrency": settings.concurrency,
        "max_refs": settings.max_refs,
        "secret_is_default": settings.secret_is_default,
        "degraded": False,
        "degraded_reason": "",
        "tasks_pending": 0,
        "in_flight_images": 0,
    }


def sweep_local_files(now: dt.datetime | None = None) -> dict:
    """删除本地存储里过期的参考图/产出（本地存储 = 24h 保留）。"""
    settings = get_image_settings()
    if settings.storage.strip().lower() != "local":
        return {"removed": 0, "skipped": "storage!=local"}
    reference = now or dt.datetime.now()
    removed = 0
    for directory in (settings.refs_dir, settings.outputs_dir, settings.thumbs_dir):
        if not directory.exists():
            continue
        cutoff = reference - dt.timedelta(hours=settings.retention_hours)
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
                except OSError:  # pragma: no cover —— 权限/占用
                    logger.warning("清理文件失败：%s", path)
    return {"removed": removed}


async def _cleanup_loop(interval_seconds: int) -> None:
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            result = sweep_local_files()
            if result.get("removed"):
                logger.info("本地图片清理：%s", result)
        except asyncio.CancelledError:  # pragma: no cover —— 关机时正常退出
            raise
        except Exception as exc:  # noqa: BLE001 —— 清理失败不影响服务
            logger.warning("图片清理任务异常：%s", exc)


async def startup() -> None:
    settings = get_image_settings()
    if not settings.enabled:
        logger.info("Image Agent 已通过 IMAGE_ENABLED=false 关闭")
        return
    result = ensure_image_schema()
    if result["created"]:
        logger.info("image_* 表已就绪：%s", result["created"])
    # 独立账号：预置 3 个演示账号（幂等，已存在不覆盖密码）
    try:
        from app.agents_image.store.users import ensure_seed_users
        from app.db.engine import session_scope

        with session_scope() as session:
            created_users = ensure_seed_users(session)
        if created_users:
            logger.info("生图账号已预置：%s", created_users)
    except Exception as exc:  # noqa: BLE001 —— 账号预置失败不阻塞启动
        logger.warning("生图账号预置失败：%s", exc)
    if settings.secret_is_default:
        logger.warning("IMAGE_AUTH_SECRET 仍是默认值，上线前务必改掉（当前令牌可被伪造）")
    health_registry.register("image", health_block)
    if settings.storage.strip().lower() == "local":
        for directory in (settings.refs_dir, settings.outputs_dir, settings.thumbs_dir):
            directory.mkdir(parents=True, exist_ok=True)
    global _cleanup_task
    if settings.cleanup_interval_minutes > 0:
        _cleanup_task = asyncio.create_task(
            _cleanup_loop(settings.cleanup_interval_minutes * 60),
            name="image-cleanup",
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


async def shutdown() -> None:
    global _cleanup_task
    if _cleanup_task is not None:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _cleanup_task = None
    health_registry.unregister("image")


def reset_for_tests() -> None:
    """测试用：清掉进程内状态（后台任务在测试里不启动）。"""
    global _cleanup_task
    if _cleanup_task is not None:
        _cleanup_task.cancel()
        _cleanup_task = None
    health_registry.unregister("image")
    shutil.rmtree(get_image_settings().images_dir, ignore_errors=True)
