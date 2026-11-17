"""MCP 连接池 / 熔断基准：把「同 server 并行取数」的收益量化出来。

为保持可复现、不依赖数据库与子进程，这里用**合成会话**（注入固定工具延迟）压测
`_PooledClient` 的排队行为：对比 pool=1/2/3 下 6 个并发调用的墙钟耗时。

真实链路的数字来自：
- `GET /api/health` 的 `mcp.servers[*].pending / breaker`；
- 管理端「工具调用可靠性」表的 P50/P95 与失败分类。

用法：python scripts/bench_mcp.py
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

LATENCY_MS = 80          # 合成：单次工具调用耗时（贴近缓存未命中的取数）
CONCURRENCY = 6          # 一轮 ReAct 里并行发出的工具调用数


class _Session:
    def __init__(self, latency: float) -> None:
        self._latency = latency

    async def call_tool(self, tool, arguments=None):  # noqa: ANN001
        await asyncio.sleep(self._latency)
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text=json.dumps({"ok": True}))])


def _factory(latency: float):
    @asynccontextmanager
    async def factory():
        yield _Session(latency)

    return factory


async def _run(pool_size: int, latency: float) -> dict:
    from app.mcp_client.client import _PooledClient

    client = _PooledClient("erp", _factory(latency), pool_size=pool_size)
    await client.start()
    durations: list[float] = []

    async def one(index: int) -> None:
        started = time.perf_counter()
        await client.call("get_sku_inventory", {"i": index})
        durations.append((time.perf_counter() - started) * 1000)

    try:
        started = time.perf_counter()
        await asyncio.gather(*[one(i) for i in range(CONCURRENCY)])
        wall = (time.perf_counter() - started) * 1000
    finally:
        await client.stop()
    return {
        "pool": pool_size,
        "wall_ms": round(wall, 1),
        "p50_ms": round(statistics.median(durations), 1),
        "max_ms": round(max(durations), 1),
        "theoretical_ms": round(LATENCY_MS * (CONCURRENCY / pool_size), 1),
    }


def _breaker_demo() -> list[str]:
    from app.mcp_client.client import _Breaker

    breaker = _Breaker(threshold=3, reset_seconds=60.0)
    lines = [f"初始：{breaker.state()}"]
    for i in range(3):
        breaker.record_failure()
        lines.append(f"第 {i + 1} 次失败后：{breaker.state()}")
    lines.append(f"熔断期间 allow() = {breaker.allow()}（请求立即失败，不再等 30s 超时）")
    breaker.opened_at -= 61.0
    lines.append(f"窗口结束：state={breaker.state()['state']}，allow() = {breaker.allow()}（半开放一次）")
    breaker.record_success()
    lines.append(f"试探成功：{breaker.state()}")
    return lines


def main() -> int:
    print(f"合成延迟 {LATENCY_MS}ms / 并发 {CONCURRENCY} 个调用（只衡量连接池排队，不含真实网络）\n")
    rows = [asyncio.run(_run(pool, LATENCY_MS / 1000)) for pool in (1, 2, 3)]
    header = f"{'pool':>4} {'墙钟':>9} {'P50':>9} {'最慢':>9} {'理论下限':>9}"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(f"{row['pool']:>4} {row['wall_ms']:>8}ms {row['p50_ms']:>8}ms "
              f"{row['max_ms']:>8}ms {row['theoretical_ms']:>8}ms")
    base = rows[0]["wall_ms"]
    for row in rows[1:]:
        if base:
            print(f"pool={row['pool']} 相对 pool=1 提速 {base / row['wall_ms']:.2f}x")
    print("\n结论：单会话时同 server 的调用必须排队（墙钟 ≈ 并发数 × 单次耗时）；"
          "池化后并行度 = pool_size。生产建议 2~4，按 mcp_call_timeout 与上游承载调整。")

    print("\n== 熔断状态机 ==")
    for line in _breaker_demo():
        print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
