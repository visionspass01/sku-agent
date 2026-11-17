"""Redis 客户端工厂。

- 配置 `REDIS_URL` 时使用真实 Redis；
- 未配置时自动使用进程内 fakeredis（同一套 redis-py 接口，演示零依赖启动）。

**同步 / 异步两套客户端**（评审 v3-①）：
缓存与队列的同步调用点已经有 `to_thread` 包装，但**锁**不一样——会话锁是在请求入口、
`await` 之前就要拿的，如果它用同步客户端（`socket_timeout` 默认 3s），Redis 抖动一次
就能把整个事件循环冻住 3 秒（所有 SSE 心跳、其它会话一起停）。
所以锁统一走 `redis.asyncio`：参数与同步客户端完全一致，保证语义对等。

fakeredis 下两套客户端**共享同一个 `FakeServer`**：否则 `is_locked()`（同步探活）看不到
异步锁写进去的键，一条运维探针会稳定误报「没人在锁」。
"""

from __future__ import annotations

import fakeredis
import redis

from app.config import Settings, get_settings

_client = None
_async_client = None
_fake_server = None


def _client_kwargs(settings: Settings) -> dict:
    return {
        "decode_responses": True,
        "socket_connect_timeout": float(getattr(settings, "redis_socket_connect_timeout", 3.0)),
        # 关键：不设 socket_timeout 时 redis-py 的读操作会**无限阻塞**
        "socket_timeout": float(getattr(settings, "redis_socket_timeout", 3.0)),
        "health_check_interval": int(getattr(settings, "redis_health_check_interval", 30)),
    }


def _shared_fake_server() -> "fakeredis.FakeServer":
    """进程内假 Redis 的唯一数据面（同步与异步客户端共用）。"""
    global _fake_server
    if _fake_server is None:
        _fake_server = fakeredis.FakeServer()
    return _fake_server


def make_client(settings: Settings | None = None):
    settings = settings or get_settings()
    if settings.redis_url:
        return redis.Redis.from_url(settings.redis_url, **_client_kwargs(settings))
    return fakeredis.FakeRedis(server=_shared_fake_server(), decode_responses=True)


def make_async_client(settings: Settings | None = None):
    """异步客户端（锁专用）。参数与同步版保持一致，避免"两种锁语义不一样"。"""
    settings = settings or get_settings()
    if settings.redis_url:
        import redis.asyncio as aioredis

        return aioredis.Redis.from_url(settings.redis_url, **_client_kwargs(settings))
    import fakeredis.aioredis as fake_async

    return fake_async.FakeRedis(server=_shared_fake_server(), decode_responses=True)


def get_redis():
    global _client
    if _client is None:
        _client = make_client()
    return _client


def get_async_redis():
    global _async_client
    if _async_client is None:
        _async_client = make_async_client()
    return _async_client


def set_redis(client) -> None:
    """测试注入（同步客户端）。"""
    global _client
    _client = client


def set_async_redis(client) -> None:
    """测试注入（异步客户端）。

    注入的客户端不共享进程内 `FakeServer`（fakeredis 不对外暴露该属性），
    所以同步探活 `is_locked()` 看不见注入客户端的键——这类用例请直接断言注入客户端。
    """
    global _async_client
    _async_client = client


def reset_redis() -> None:
    """丢弃进程内单例并清空假 Redis 数据面（测试隔离用）。

    真实 Redis 下会 `flushall` —— 只在测试里调用，不要在生产代码路径上用。
    """
    global _client, _async_client, _fake_server
    if _client is not None:
        try:
            _client.flushall()
        except Exception:  # noqa: BLE001 —— 清理失败不该影响用例收尾
            pass
    elif _fake_server is not None:
        # 只注入了异步客户端：同步侧没有句柄，但仍要清掉共享的假服务端数据
        try:
            fakeredis.FakeRedis(server=_fake_server, decode_responses=True).flushall()
        except Exception:  # noqa: BLE001
            pass
    _client = None
    _async_client = None
    _fake_server = None


async def aclose_async_redis() -> None:
    """关闭异步客户端（应用 shutdown 时调用）。"""
    global _async_client
    client = _async_client
    _async_client = None
    if client is None:
        return
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001 —— fakeredis 可能没有 aclose，关不掉也不影响退出
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


def redis_mode() -> str:
    settings = get_settings()
    return "redis" if settings.redis_url else "fakeredis"
