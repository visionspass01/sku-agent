"""MCP 客户端：memory（进程内 MCP JSON-RPC，默认）/ stdio（真实子进程）/ direct（函数直调）。

对外只暴露 `call(server, tool, arguments)`，结果自动走 Redis TTL+抖动缓存；
Agent/Worker 不感知传输方式。

实现要点：
1. **会话与任务绑定**：MCP 的会话（anyio 任务组）必须与创建它的任务在同一任务内关闭，
   因此每个 server 连接由**独立的 asyncio 任务**持有，调用请求经队列投递、在持有任务内执行。
2. **连接池**：单个 server 默认持 `MCP_POOL_SIZE`（2）条会话，调用投给当前排队最少的会话，
   避免「一个慢工具把同 server 的其它工具全堵住」——ReAct 一轮里并行取数不再串行。
3. **熔断**：同一 server 连续失败 `MCP_BREAKER_THRESHOLD`（3）次后打开 `MCP_BREAKER_RESET_SECONDS`
   （60s），期间请求直接快速失败并标记 outcome=breaker_open，避免每个请求都先超时再重试。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import sys
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_client_server_memory_streams

from app.cache.ttl_cache import get_ttl_cache
from app.config import BASE_DIR, Settings, get_settings
from app.observability import latency, tool_stats
from app.resilience import Breaker as _Breaker


def _trace_id() -> str:
    """本轮 trace（= turn_id）：让工具失败日志能按一轮对话串起来。"""
    try:
        from app.observability import trace as trace_mod

        return trace_mod.current_trace_id()
    except Exception:  # noqa: BLE001 —— 观测失败不影响调用
        return ""

SERVER_MODULES = {
    "erp": "app.mcp_servers.erp_server",
    "pailitao": "app.mcp_servers.pailitao_server",
    "order": "app.mcp_servers.order_server",
}

SERVER_TOOLS = {
    "erp": [
        "get_sku_inventory", "get_sales_history", "get_in_transit",
        "get_warehouse_transfers", "get_sibling_sizes",
        "get_supplier_info", "get_platform_activities",
    ],
    "pailitao": ["search_same_style", "get_price_band", "get_style_heat"],
    "order": [
        "get_order_status", "list_orders", "get_logistics",
        "get_return_risk", "create_after_sales_proposal",
    ],
}

# direct 传输（测试）时按模块取工具表：ERP_TOOLS / PAILITAO_TOOLS / ORDER_TOOLS
_TOOLS_ATTR = {"erp": "ERP_TOOLS", "pailitao": "PAILITAO_TOOLS", "order": "ORDER_TOOLS"}


class McpCallError(RuntimeError):
    """MCP 调用失败。

    `retryable=False` 表示**确定性错误**（例如工具名不存在、server 未注册）：
    重试不可能变好，也不该把它算成"服务端不健康"（会误开熔断）。复审 R5。
    """

    retryable = True


def _load_fastmcp(server: str):
    module = importlib.import_module(SERVER_MODULES[server])
    return module.mcp


async def _parse_result(session: ClientSession, tool: str, arguments: dict) -> dict:
    result = await session.call_tool(tool, arguments=arguments)
    if getattr(result, "isError", False):
        raise McpCallError(f"MCP 工具调用失败: {result}")
    texts: list[str] = []
    for item in result.content or []:
        text = getattr(item, "text", None)
        if text is not None:
            texts.append(text)
    payload = "\n".join(texts)
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return {"raw_text": payload}


# ---------------------------------------------------------------- 会话工厂
@asynccontextmanager
async def _memory_session(server: str) -> AsyncIterator[ClientSession]:
    fastmcp = _load_fastmcp(server)
    lowlevel = fastmcp._mcp_server
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        server_task = asyncio.create_task(
            lowlevel.run(*server_streams, lowlevel.create_initialization_options())
        )
        try:
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                yield session
        finally:
            server_task.cancel()
            try:
                await server_task
            except (asyncio.CancelledError, Exception):
                pass


@asynccontextmanager
async def _stdio_session(server: str) -> AsyncIterator[ClientSession]:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", SERVER_MODULES[server]],
        cwd=str(BASE_DIR),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


class _SessionRunner:
    """在独立任务内持有 MCP 会话，接收队列中的调用请求。"""

    def __init__(self, session_factory) -> None:
        self._factory = session_factory
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._error: BaseException | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="mcp-session-runner")
        await self._ready.wait()
        if self._error is not None:
            raise McpCallError(f"MCP 会话启动失败: {self._error}") from self._error

    async def _run(self) -> None:
        try:
            async with self._factory() as session:
                self._ready.set()
                while True:
                    item = await self._queue.get()
                    if item is None:
                        break
                    fut, tool, args = item
                    try:
                        fut.set_result(await _parse_result(session, tool, args))
                    except BaseException as exc:  # noqa: BLE001
                        if not fut.done():
                            fut.set_exception(exc)
        except BaseException as exc:  # noqa: BLE001
            self._error = exc
            self._ready.set()
            while not self._queue.empty():
                item = self._queue.get_nowait()
                if item is not None and not item[0].done():
                    item[0].set_exception(exc)

    async def call(self, tool: str, arguments: dict) -> dict:
        if self._task is None:
            raise McpCallError("MCP 会话未启动")
        if self._task.done():
            raise McpCallError(f"MCP 会话已关闭: {self._error or 'closed'}")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._queue.put((fut, tool, arguments))
        return await fut

    @property
    def pending(self) -> int:
        """排队中（尚未开始执行）的请求数：池内据此挑最闲的会话。"""
        return self._queue.qsize()

    async def stop(self) -> None:
        if self._task is None:
            return
        if not self._task.done():
            await self._queue.put(None)
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None


class _PooledClient:
    """按 server 持 N 条会话：调用投给「排队最少」的那条，实现同 server 并行取数。

    之前每个 server 只有一条会话，ReAct 一轮里并行发的工具调用会在队列里串行排队，
    表现为「工具 P95 忽高忽低」。连接池把并行度还给调用方（默认 2 条，见 MCP_POOL_SIZE）。
    """

    def __init__(self, server: str, factory, pool_size: int = 1) -> None:
        self.server = server
        self._factory = factory
        self._pool_size = max(1, int(pool_size or 1))
        self._runners: list[_SessionRunner] = []

    async def start(self) -> None:
        runners = [_SessionRunner(self._factory) for _ in range(self._pool_size)]
        for runner in runners:
            await runner.start()
        self._runners = runners

    async def stop(self) -> None:
        for runner in self._runners:
            try:
                await runner.stop()
            except Exception:  # noqa: BLE001
                pass
        self._runners = []

    @property
    def pending(self) -> int:
        return sum(runner.pending for runner in self._runners)

    def _pick(self) -> _SessionRunner:
        if not self._runners:
            raise McpCallError(f"MCP server `{self.server}` 未启动")
        return min(self._runners, key=lambda runner: runner.pending)

    async def call(self, tool: str, arguments: dict) -> dict:
        return await self._pick().call(tool, arguments)


class MemoryMCPClient:
    def __init__(self, server: str, pool_size: int = 1) -> None:
        self._pool = _PooledClient(server, lambda: _memory_session(server), pool_size)

    @property
    def server(self) -> str:
        return self._pool.server

    @property
    def pending(self) -> int:
        return self._pool.pending

    async def start(self) -> None:
        await self._pool.start()

    async def stop(self) -> None:
        await self._pool.stop()

    async def call(self, tool: str, arguments: dict) -> dict:
        return await self._pool.call(tool, arguments)


class StdioMCPClient:
    def __init__(self, server: str, pool_size: int = 1) -> None:
        self._pool = _PooledClient(server, lambda: _stdio_session(server), pool_size)

    @property
    def server(self) -> str:
        return self._pool.server

    @property
    def pending(self) -> int:
        return self._pool.pending

    async def start(self) -> None:
        await self._pool.start()

    async def stop(self) -> None:
        await self._pool.stop()

    async def call(self, tool: str, arguments: dict) -> dict:
        return await self._pool.call(tool, arguments)


class DirectMCPClient:
    """进程内直调：工具函数即服务端函数，跳过协议与序列化（仅供测试）。"""

    def __init__(self, server: str) -> None:
        self.server = server

    @property
    def pending(self) -> int:
        return 0

    async def start(self) -> None:  # noqa: D401
        return None

    async def stop(self) -> None:
        return None

    async def call(self, tool: str, arguments: dict) -> dict:
        module = importlib.import_module(SERVER_MODULES[self.server])
        tools = getattr(module, _TOOLS_ATTR.get(self.server, "ORDER_TOOLS"))
        fn = tools.get(tool)
        if fn is None:
            # 工具名写错是**确定性**错误：重试没意义，也不该算服务端不健康
            err = McpCallError(f"未知工具: {self.server}.{tool}")
            err.retryable = False
            raise err
        # 适配器化之后 MCP 工具是协程（内部 await 中台 REST）：协程函数直接 await，
        # 同步函数仍丢线程池（保持"同步工具不阻塞事件循环"的老行为）。
        if inspect.iscoroutinefunction(fn):
            return await fn(**arguments)
        return await asyncio.to_thread(fn, **arguments)


class MCPHub:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._clients: dict[str, object] = {}
        self._breakers: dict[str, _Breaker] = {}
        self._started = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ 生命周期
    async def startup(self) -> None:
        async with self._lock:
            if self._started:
                return
            pool = max(1, int(getattr(self._settings, "mcp_pool_size", 1) or 1))
            for server in SERVER_MODULES:
                if self._settings.mcp_transport == "direct":
                    client = DirectMCPClient(server)
                elif self._settings.mcp_transport == "stdio":
                    client = StdioMCPClient(server, pool_size=pool)
                else:
                    client = MemoryMCPClient(server, pool_size=pool)
                await client.start()
                self._clients[server] = client
                self._breakers[server] = _Breaker(
                    threshold=getattr(self._settings, "mcp_breaker_threshold", 3),
                    reset_seconds=getattr(self._settings, "mcp_breaker_reset_seconds", 60.0),
                )
            self._started = True

    async def shutdown(self) -> None:
        for client in self._clients.values():
            try:
                await client.stop()
            except Exception:
                pass
        self._clients.clear()
        self._breakers.clear()
        self._started = False

    def health(self) -> dict:
        """连接池与熔断状态（/api/health 暴露，用于排查「为什么这个工具立刻失败」）。"""
        servers = {}
        for name, client in self._clients.items():
            breaker = self._breakers.get(name)
            servers[name] = {
                "pending": int(getattr(client, "pending", 0) or 0),
                "sessions": max(1, int(getattr(self._settings, "mcp_pool_size", 1) or 1))
                if name in SERVER_MODULES else 0,
                "breaker": breaker.state() if breaker else {"state": "unknown"},
            }
        return {
            "transport": self._settings.mcp_transport,
            "started": self._started,
            "pool_size": int(getattr(self._settings, "mcp_pool_size", 1) or 1),
            "servers": servers,
        }

    # ------------------------------------------------------------ 调用
    async def call(self, server: str, tool: str, arguments: dict | None = None,
                   use_cache: bool = True) -> dict:
        if not self._started:
            await self.startup()
        arguments = arguments or {}
        if not use_cache:
            return await self._call_raw(server, tool, arguments)
        cache = get_ttl_cache()
        cache_key = f"mcp:{server}:{tool}:" + hashlib.md5(
            json.dumps(arguments, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return await cache.aget_or_set_async(
            cache_key,
            self._settings.cache_ttl_mcp,
            lambda: self._call_raw(server, tool, arguments),
            # 业务错误（工具返回 {"error": ...}）不进缓存：否则「订单不存在」这类
            # 结果会被缓存 10 分钟，后续正确的重试也一直拿到同一个错误
            should_cache=lambda payload: isinstance(payload, dict) and not payload.get("error"),
        )

    async def _call_raw(self, server: str, tool: str, arguments: dict) -> dict:
        started = time.perf_counter()
        breaker = self._breakers.get(server)
        if breaker is not None and not breaker.allow():
            state = breaker.state()
            latency.record("mcp_call", (time.perf_counter() - started) * 1000)
            tool_stats.record(server, tool, False, (time.perf_counter() - started) * 1000,
                              outcome="breaker_open", trace_id=_trace_id())
            raise McpCallError(
                f"MCP server `{server}` 熔断中（{state['retry_in_s']}s 后重试）："
                f"最近连续 {state['failures']} 次失败，已快速失败避免拖垮本轮"
            )
        last_exc: Exception | None = None
        retries = 0
        timeout = False
        try:
            for attempt in range(self._settings.mcp_max_retries + 1):
                try:
                    client = self._clients.get(server)
                    if client is None:
                        err = McpCallError(f"MCP server `{server}` 未注册")
                        err.retryable = False
                        raise err
                    result = await asyncio.wait_for(
                        client.call(tool, arguments), timeout=self._settings.mcp_call_timeout
                    )
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    latency.record("mcp_call", elapsed_ms)
                    tool_stats.record(server, tool, True, elapsed_ms, retries=retries, trace_id=_trace_id())
                    if breaker is not None:
                        breaker.record_success()
                    return result
                except (asyncio.TimeoutError, McpCallError) as exc:
                    last_exc = exc
                    timeout = isinstance(exc, asyncio.TimeoutError)
                    retries += 1
                    # 复审 R5：确定性错误（未知工具/服务未注册）立即结束，不做无效重试
                    if getattr(exc, "retryable", True) is False:
                        break
                    await asyncio.sleep(0.4 * (attempt + 1))
        finally:
            # 调用被取消（客户端断连 / 本轮被中断）时 record_* 不会执行：
            # 必须放掉熔断器的探测标记，否则半开状态会永久卡住
            if breaker is not None:
                breaker.release_probe()
        elapsed_ms = (time.perf_counter() - started) * 1000
        latency.record("mcp_call", elapsed_ms)
        tool_stats.record(server, tool, False, elapsed_ms, retries=retries, timeout=timeout,
                          trace_id=_trace_id())
        deterministic = getattr(last_exc, "retryable", True) is False
        if breaker is not None and not deterministic:
            # 确定性错误（工具名写错/服务未注册）不代表服务端不健康：不计熔断失败
            breaker.record_failure()
        message = (f"调用 MCP 工具 {server}.{tool} 失败（确定性错误，未重试）: {last_exc}"
                   if deterministic else
                   f"调用 MCP 工具 {server}.{tool} 失败（已重试）: {last_exc}")
        raise McpCallError(message)



_hub: MCPHub | None = None


def get_mcp_hub() -> MCPHub:
    global _hub
    if _hub is None:
        _hub = MCPHub()
    return _hub


async def shutdown_mcp_hub() -> None:
    global _hub
    if _hub is not None:
        await _hub.shutdown()
        _hub = None
