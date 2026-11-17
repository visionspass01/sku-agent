"""LangGraph checkpointer 与「分析线程」持久化。

为什么换掉 MemorySaver：
- 进程内存储重启即丢：追问会 404、已删线程无法恢复；
- 无上限：反复研判会把所有线程留在内存里（原实现 trim_threads 从未被调用）。

当前实现：
- 默认 `AsyncSqliteSaver`（`langgraph-checkpoint-sqlite`），落 `data/checkpoints.sqlite`，
  重启后同一 thread 的追问仍可继续；
- `CHECKPOINT_BACKEND=redis` 且 `langgraph-checkpoint-redis` 已安装且 Redis 可达时用 RedisSaver，
  否则**自动回退 SQLite** 并在日志里注明；
- 线程上限 `CHECKPOINT_MAX_THREADS=200`：`checkpoint_thread` 表记录最近使用时间，
  超出后调用 `adelete_thread` 删除最旧线程。

工程要点：aiosqlite 的连接绑定事件循环、后台线程非 daemon，**必须用完即关**，
因此这里每次研判/追问/淘汰都以上下文管理器打开 saver（setup 是 IF NOT EXISTS，毫秒级），
而不是长期持有单例连接；这样测试里每个 `asyncio.run` 的 loop 都不会泄漏线程或连接。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import pathlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from app.config import DATA_DIR, get_settings

logger = logging.getLogger("sku-agent.checkpoint")

_saver_kind = ""
_MAX_THREADS = 200


def checkpoint_path() -> pathlib.Path:
    """checkpoint 文件路径：显式配置 > 与业务库同目录（测试 tmp）> data/checkpoints.sqlite。"""
    settings = get_settings()
    explicit = (getattr(settings, "checkpoint_path", "") or "").strip()
    if explicit:
        path = pathlib.Path(explicit)
    elif settings.is_sqlite:
        raw = settings.database_url.replace("sqlite:///", "").replace("sqlite://", "")
        base = pathlib.Path(raw)
        path = (base.parent if base.parent.name else DATA_DIR) / "checkpoints.sqlite"
    else:
        path = DATA_DIR / "checkpoints.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _backend() -> str:
    settings = get_settings()
    return (getattr(settings, "checkpoint_backend", "sqlite") or "sqlite").strip().lower()


@asynccontextmanager
async def _saver_cm() -> AsyncIterator[Any]:
    """按配置打开 saver，用完即关（SQLite 连接/Redis 会话都不外泄）。"""
    global _saver_kind
    settings = get_settings()
    if _backend() == "redis" and settings.redis_url:
        try:
            from langgraph.checkpoint.redis.aio import AsyncRedisSaver  # 可选依赖

            async with AsyncRedisSaver.from_conn_string(settings.redis_url) as saver:
                await saver.setup()
                _saver_kind = "redis"
                yield saver
                return
        except Exception as exc:  # noqa: BLE001 —— 可选依赖缺失/Redis 不可达都回退
            logger.warning("Redis checkpointer 不可用（%s），回退 SQLite", exc)
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = checkpoint_path()
    conn = await aiosqlite.connect(str(path), check_same_thread=False)
    try:
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        _saver_kind = "sqlite"
        yield saver
    finally:
        with contextlib.suppress(Exception):
            await conn.close()


@asynccontextmanager
async def inspection_graph() -> AsyncIterator[Any]:
    """打开一个带持久化 checkpointer 的图（用完自动关闭 saver）。"""
    from app.agents.graph import build_inspection_graph

    async with _saver_cm() as saver:
        yield build_inspection_graph(checkpointer=saver)


def saver_kind() -> str:
    return _saver_kind or "init"


async def _delete_thread(saver: Any, thread_id: str) -> None:
    try:
        await saver.adelete_thread(thread_id)
    except Exception:  # noqa: BLE001 —— 线程不存在/实现不支持时不阻塞主流程
        pass


async def reset_thread(thread_id: str) -> None:
    """重新研判前清空该线程，避免上一轮 attempts/verification 被带进来。"""
    async with _saver_cm() as saver:
        await _delete_thread(saver, thread_id)


async def touch_thread(thread_id: str) -> None:
    """记录线程最近使用时间（用于上限淘汰）。"""
    try:
        from app.db.engine import session_scope
        from app.db.models import CheckpointThread

        with session_scope() as session:
            row = session.get(CheckpointThread, thread_id)
            if row is None:
                session.add(CheckpointThread(thread_id=thread_id, updated_at=dt.datetime.now()))
            else:
                row.updated_at = dt.datetime.now()
    except Exception as exc:  # noqa: BLE001
        logger.debug("touch_thread 失败：%s", exc)


async def prune_threads(max_threads: int | None = None,
                        min_age_seconds: float | None = None) -> int:
    """超过上限时删除最旧线程（真删，不再只留文档）。

    `min_age_seconds`（复审 F2）：只淘汰"最近没被 touch 过"的线程。早先只按上限裁，
    一个还在跑的长研判（最多 `INSPECT_TIMEOUT_SECONDS`）可能因为"上一次结束时间很早"
    被并发的裁剪删掉——它的 checkpointer 数据一删，追问就复用不到上一轮采集数据了。
    现在 patrol 在开始时也会 touch，所以保护窗口只需覆盖"touch 间隔 + 一点余量"。
    """
    limit = int(max_threads or getattr(get_settings(), "checkpoint_max_threads", _MAX_THREADS))
    protect = float(min_age_seconds if min_age_seconds is not None else getattr(
        get_settings(), "checkpoint_prune_min_age_seconds", 600.0))
    try:
        from sqlalchemy import select

        from app.db.engine import session_scope
        from app.db.models import CheckpointThread

        with session_scope() as session:
            times = {r.thread_id: r.updated_at for r in session.scalars(select(CheckpointThread))}
        overflow = len(times) - limit
        if overflow <= 0:
            return 0
        cutoff = dt.datetime.now() - dt.timedelta(seconds=max(0.0, protect))
        candidates = [(tid, ts) for tid, ts in times.items()
                      if (ts or dt.datetime.min) < cutoff]
        if not candidates:
            return 0
        ordered = sorted(candidates, key=lambda kv: kv[1] or dt.datetime.min)
        targets = [tid for tid, _ in ordered[:overflow]]
        dropped = 0
        async with _saver_cm() as saver:
            for thread_id in targets:
                await _delete_thread(saver, thread_id)
                with session_scope() as session:
                    row = session.get(CheckpointThread, thread_id)
                    if row is not None:
                        session.delete(row)
                dropped += 1
        return dropped
    except Exception as exc:  # noqa: BLE001
        logger.debug("prune_threads 失败：%s", exc)
        return 0


async def startup() -> None:
    """应用启动：打开一次 saver 做 setup 自检（失败不阻塞启动）。"""
    try:
        async with _saver_cm():
            pass
        logger.info("checkpointer 就绪（backend=%s，path=%s）", _saver_kind,
                    checkpoint_path() if _saver_kind == "sqlite" else "-")
    except Exception as exc:  # noqa: BLE001
        logger.warning("checkpointer 初始化失败（追问将退化为仅限本进程内）：%s", exc)


async def shutdown() -> None:
    """当前实现连接随上下文关闭，这里只做状态清理。"""
    global _saver_kind
    _saver_kind = _saver_kind  # 保留最近一次类型供 health 展示


# 线程计数缓存：前端会轮询 /api/health，没必要每次都 COUNT（默认 10s）
_health_cache: dict[str, object] = {"at": 0.0, "threads": None}


def _count_threads() -> int | None:
    try:
        from sqlalchemy import func, select

        from app.db.engine import session_scope
        from app.db.models import CheckpointThread

        with session_scope() as session:
            return int(session.scalar(select(func.count()).select_from(CheckpointThread)) or 0)
    except Exception:  # noqa: BLE001 —— health 不能因为读不到计数就 500
        return None


def checkpoint_health() -> dict:
    """health 用：后端类型、文件路径、当前线程数与上限（不暴露连接串密码）。

    线程计数带 TTL 缓存（`HEALTH_CACHE_SECONDS`，默认 10s）：health 被前端轮询，
    每次都 COUNT 是纯浪费；ttl 内直接复用上次结果。
    """
    import time as _time

    settings = get_settings()
    backend = _backend()
    ttl = max(0.0, float(getattr(settings, "health_cache_seconds", 10.0)))
    now = _time.monotonic()
    if _health_cache["threads"] is not None and now - float(_health_cache["at"]) < ttl:
        threads = int(_health_cache["threads"])  # type: ignore[arg-type]
    else:
        threads = _count_threads()
        if threads is not None:
            _health_cache["at"] = now
            _health_cache["threads"] = threads
    return {
        "backend": _saver_kind or backend,
        "configured": backend,
        "path": str(checkpoint_path()) if _saver_kind != "redis" else "",
        "threads": threads,
        "max_threads": int(getattr(settings, "checkpoint_max_threads", _MAX_THREADS)),
        # 多副本提醒：SQLite 是单写者，多进程/多副本并发写会互相等锁（本模块的 aiosqlite
        # 连接没有 WAL/busy_timeout，压测下会直接 "database is locked"）。
        "warning": (
            "SQLite checkpointer 是单写者：多副本 / 多 worker 部署请改用 CHECKPOINT_BACKEND=redis"
            if backend == "sqlite" else ""
        ),
        "note": "分析线程持久化：重启后追问仍可用；超上限淘汰最旧线程",
    }
