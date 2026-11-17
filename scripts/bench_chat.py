"""对话链路压测：给「并发多了会怎样」一个可复现的数字。

两种模式：

1. `--mock`（默认，零成本、可进 CI）：走**真实链路**（鉴权 → 会话 → 记忆 → 限流 →
   并发闸门 → grounding），只把模型换成"睡眠固定时长后返回假答复"的桩。
   用来验证并发闸门与限流是否真的生效，以及吞吐随并发怎么变化。

2. `--live --url http://127.0.0.1:8000`：真打一个跑着的服务（真模型、真花钱），
   给出端到端 P50/P95 与错误率；token 用 `--token` 或环境变量 `BENCH_TOKEN`。

示例：
    PYTHONPATH=src python scripts/bench_chat.py --concurrency 8 --requests 24
    PYTHONPATH=src python scripts/bench_chat.py --concurrency 4 --max-concurrency 2
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _pct(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(ratio * (len(ordered) - 1)))))
    return round(ordered[idx], 1)


async def _drive(send, *, concurrency: int, requests: int) -> dict:
    """按并发上限压 requests 次，返回耗时分布与错误数。"""
    durations: list[float] = []
    errors: list[str] = []
    sem = asyncio.Semaphore(concurrency)

    async def one(idx: int) -> None:
        async with sem:
            started = time.perf_counter()
            try:
                status, detail = await send(idx)
                if status != 200:
                    errors.append(f"{status}: {detail[:80]}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}"[:120])
            finally:
                durations.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    await asyncio.gather(*[one(i) for i in range(requests)])
    wall = (time.perf_counter() - started) * 1000
    return {
        "requests": requests,
        "concurrency": concurrency,
        "wall_ms": round(wall, 1),
        "p50_ms": _pct(durations, 0.50),
        "p95_ms": _pct(durations, 0.95),
        "max_ms": round(max(durations), 1) if durations else 0.0,
        "errors": len(errors),
        "error_samples": errors[:3],
        "throughput_rps": round(requests / (wall / 1000), 2) if wall else 0.0,
    }


def _report(title: str, row: dict, extra: dict | None = None) -> None:
    print(f"\n== {title} ==")
    print(f"  请求 {row['requests']} 条 · 客户端并发 {row['concurrency']}")
    print(f"  墙钟 {row['wall_ms']}ms · P50 {row['p50_ms']}ms · P95 {row['p95_ms']}ms · 最慢 {row['max_ms']}ms")
    print(f"  吞吐 {row['throughput_rps']} req/s · 错误 {row['errors']}")
    for sample in row["error_samples"]:
        print(f"    ! {sample}")
    if extra:
        for key, value in extra.items():
            print(f"  {key}: {value}")


async def run_mock(args) -> dict:
    import httpx
    from langchain_core.messages import AIMessage

    from app.agents import chat_agent
    from app.auth import AuthUser, issue_token
    from app.config import get_settings
    from app.main import app

    settings = get_settings()
    # 压测期间放开限流（否则 20 rpm 会先于并发闸门生效），并固定并发上限
    settings.chat_rate_limit_per_minute = 10 ** 6
    settings.chat_max_concurrency = max(1, args.max_concurrency)
    settings.grounding_enabled = False
    chat_agent.reset_chat_semaphores()

    latency = max(0, args.latency_ms) / 1000.0

    class _BenchAgent:
        async def ainvoke(self, payload):
            await asyncio.sleep(latency)
            return {"messages": payload["messages"] + [AIMessage(content="（benchmark 假答复）")]}

    # 桩签名必须与 `chat_agent.build_chat_agent(user, system_prompt, manifest=None)` 对齐，
    # 否则 `--mock` 会在第一次调用就 TypeError（2026-11 审计发现的过期桩）。
    chat_agent.build_chat_agent = lambda user, prompt, manifest=None: _BenchAgent()
    token = issue_token(AuthUser(username="admin", role="admin", department=None))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bench", timeout=600) as client:
        async def send(idx: int):
            resp = await client.post(
                "/api/chat",
                headers={"Authorization": f"Bearer {token}"},
                json={"message": f"压测请求 #{idx}"},
            )
            return resp.status_code, resp.text

        row = await _drive(send, concurrency=args.concurrency, requests=args.requests)

    # 理论墙钟：K 个并发槽、每次 latency → ceil(n/K)*latency
    slots = min(args.concurrency, max(1, args.max_concurrency))
    ideal = round((args.requests / slots) * args.latency_ms, 1)
    _report("对话压测（mock 模型，真实链路）", row, {
        "chat_max_concurrency": args.max_concurrency,
        "模型延迟": f"{args.latency_ms}ms",
        "理论墙钟": f"{ideal}ms（K=min(客户端并发, 闸门)={slots}）",
        "结论": "墙钟接近理论值 = 并发闸门按预期排队；远高于理论值说明链路里还有串行点",
    })
    return row


async def run_live(args) -> dict:
    import httpx

    token = args.token or os.environ.get("BENCH_TOKEN", "")
    if not token:
        print("缺少 token：用 --token 或环境变量 BENCH_TOKEN 提供登录 token", file=sys.stderr)
        raise SystemExit(2)

    async with httpx.AsyncClient(base_url=args.url.rstrip("/"), timeout=600) as client:
        async def send(idx: int):
            resp = await client.post(
                "/api/chat",
                headers={"Authorization": f"Bearer {token}"},
                json={"message": f"压测请求 #{idx}：ST-01-C1-M 当前可用库存？"},
            )
            return resp.status_code, resp.text

        row = await _drive(send, concurrency=args.concurrency, requests=args.requests)

    _report(f"对话压测（真实服务 {args.url}）", row, {
        "提示": "真实模型延迟受供应商波动影响；错误率高时看是否触发了 429（限流）",
    })
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description="对话链路并发压测")
    parser.add_argument("--mock", action="store_true", help="用桩模型跑真实链路（默认）")
    parser.add_argument("--live", action="store_true", help="打真实服务（真模型、真花钱）")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default="")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--requests", type=int, default=24)
    parser.add_argument("--max-concurrency", type=int, default=4,
                        help="mock 模式下覆盖 CHAT_MAX_CONCURRENCY")
    parser.add_argument("--latency-ms", type=int, default=800,
                        help="mock 模型单次延迟（贴近真实一轮 7~9s 时可设 7000）")
    args = parser.parse_args()

    row = asyncio.run(run_live(args) if args.live else run_mock(args))
    return 1 if row["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
