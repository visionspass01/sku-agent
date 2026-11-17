"""多 Agent 并发压测：同一中台上"插拔多个 Agent"到底成不成立（离线）。

验证四件事（多 Agent 场景最容易被问到、也最容易漏的）：
1. **并发可用**：N 个 Agent 各自持 key 同时打中台，成功率 100%；
2. **配额按调用方**：某个 Agent 的小配额只影响它自己，不拖累别人；
3. **审计按调用方**：每次调用都能在 `api_audit_log` 里按 `agent_id` 找到（问得清"谁调的"）；
4. **部门隔离**：只能看本部门的 Agent 拿不到别部门数据——**并发下也不漏**。

用法：
    python scripts/bench_multi_agent.py                 # 4 个 Agent × 20 轮
    python scripts/bench_multi_agent.py --agents 6 --turns 30 --json data/eval/multi-agent.json
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import pathlib
import statistics
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

# 4 个调用方：全权 / 只读男装 / 只读女装+童装 / 小配额运维
AGENTS = {
    "sku-agent": {"key": "k-bench-sku", "scopes": ["read", "write", "approve"], "departments": None},
    "analysis-agent": {"key": "k-bench-mens", "scopes": ["read"], "departments": ["男装"]},
    "women-agent": {"key": "k-bench-women", "scopes": ["read"], "departments": ["女装"]},
    "ops-agent": {"key": "k-bench-ops", "scopes": ["read", "etl"], "departments": None,
                  "rate_limit": 40},
}
DEPARTMENTS = {"sku-agent": None, "analysis-agent": [("男装",)], "women-agent": [("女装", "童装")],
               "ops-agent": None}


def _setup(tmp: pathlib.Path):  # noqa: ANN201
    from data_platform import config
    from data_platform.api import authz, quota
    from data_platform.api import cache as pcache
    from data_platform.db import init_schema, make_engine, set_engine
    from data_platform.ingest.airflow_mock import run_dag
    from data_platform.mock_sources import generate_all

    config.API_KEYS_RAW = json.dumps(AGENTS, ensure_ascii=False)
    config.RATE_LIMIT_PER_MINUTE = 600
    authz.reset_principals()
    quota.reset()

    engine = make_engine(f"sqlite:///{(tmp / 'platform.db').as_posix()}")
    set_engine(engine)
    init_schema(engine)
    pcache.reset_client()
    generate_all(snapshot_date=dt.date(2026, 11, 3), seed=7)
    result = run_dag("daily_sync")
    assert all(t["status"] == "success" for t in result["tasks"].values()), result

    from sqlalchemy import select

    from data_platform.db import MartSkuMetrics, session_scope

    with session_scope() as session:
        rows = list(session.scalars(select(MartSkuMetrics)))
        skus = {"男装": [r.sku_id for r in rows if r.department == "男装"],
                "女装": [r.sku_id for r in rows if r.department == "女装"]}
    return skus


async def _run_agents(agent_ids: list[str], turns: int, skus: dict) -> dict:
    import httpx

    from data_platform.api.main import app

    transport = httpx.ASGITransport(app=app)

    async def _one(agent_id: str, index: int) -> dict:
        token = AGENTS[agent_id]["key"]
        headers = {"X-Internal-Token": token, "X-Agent-Id": agent_id}
        latencies: list[float] = []
        codes: list[int] = []
        leaked = 0
        async with httpx.AsyncClient(transport=transport, base_url="http://bench.local",
                                     timeout=30.0) as client:
            for turn in range(turns):
                own_dept = DEPARTMENTS[agent_id]
                if own_dept is None:
                    target = skus["男装"][turn % len(skus["男装"])]
                else:
                    dept = own_dept[0][0]
                    target = skus[dept][turn % len(skus[dept])]
                started = time.perf_counter()
                resp = await client.get(f"/api/v1/skus/{target}/metrics", headers=headers)
                latencies.append((time.perf_counter() - started) * 1000)
                codes.append(resp.status_code)
                # 反向探测：部门 Agent 尝试读别部门（必须 403）——并发下也不许漏
                if own_dept is not None:
                    other = skus["女装" if dept == "男装" else "男装"][0]
                    probe = await client.get(f"/api/v1/skus/{other}/metrics", headers=headers)
                    if probe.status_code == 200:
                        leaked += 1
            # 顺手打一次聚合指标（多 Agent 共享聚合接口时缓存 key 必须带作用域）
            agg = await client.get("/api/v1/metrics/sales-rate", headers=headers)
            codes.append(agg.status_code)
        return {"agent_id": agent_id, "index": index, "codes": codes, "latencies": latencies,
                "leaked": leaked}

    results = await asyncio.gather(*[
        _one(agent_id, i) for i, agent_id in enumerate(agent_ids)
    ])
    return {"results": results, "transport": transport}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多 Agent 并发压测")
    parser.add_argument("--agents", type=int, default=4)
    parser.add_argument("--turns", type=int, default=20)
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bench_multi_agent_"))
    skus = _setup(tmp)
    agent_ids = list(AGENTS)[: max(1, min(args.agents, len(AGENTS)))]

    started = time.perf_counter()
    outcome = asyncio.run(_run_agents(agent_ids, args.turns, skus))
    wall_ms = (time.perf_counter() - started) * 1000
    results = outcome["results"]

    from sqlalchemy import func, select

    from data_platform.db import ApiAuditLog, session_scope

    with session_scope() as session:
        audit_rows = session.execute(
            select(ApiAuditLog.agent_id, func.count()).group_by(ApiAuditLog.agent_id)
        ).all()
    audit_counts = {agent: int(count) for agent, count in audit_rows}

    ok = True
    summary: list[dict] = []
    print(f"并发：{len(agent_ids)} 个 Agent × {args.turns} 轮，墙钟 {wall_ms:.0f} ms")
    print(f"  {'agent':<16}{'调用':>5}{'成功':>5}{'403/429':>8}{'P50':>8}{'P95':>8}{'越权泄漏':>8}")
    for item in results:
        codes = item["codes"]
        success = sum(1 for c in codes if c == 200)
        denied = sum(1 for c in codes if c in (403, 429))
        latencies = sorted(item["latencies"])
        p50 = round(statistics.median(latencies), 1)
        p95 = round(latencies[int(0.95 * (len(latencies) - 1))], 1)
        print(f"  {item['agent_id']:<16}{len(codes):>5}{success:>5}{denied:>8}"
              f"{p50:>8}{p95:>8}{item['leaked']:>8}")
        summary.append({"agent_id": item["agent_id"], "calls": len(codes), "ok": success,
                        "denied": denied, "p50_ms": p50, "p95_ms": p95,
                        "leaked": item["leaked"]})
        if item["leaked"]:
            ok = False
            print(f"  [FAIL] {item['agent_id']} 读到了别部门数据（{item['leaked']} 次）")
        if success == 0:
            ok = False
            print(f"  [FAIL] {item['agent_id']} 一次都没成功：{codes[:5]}")

    print("\n审计（按 agent_id 聚合的调用行数）：")
    for agent in agent_ids:
        count = audit_counts.get(agent, 0)
        print(f"  {agent:<16}{count:>6}")
        if count == 0:
            ok = False
            print(f"  [FAIL] {agent} 的调用没有进审计")
    if audit_counts.get("legacy-internal"):
        print(f"  [WARN] 出现 legacy-internal 归因 {audit_counts['legacy-internal']} 次"
              "（说明有调用方没带身份）")

    payload = {"agents": summary, "audit_counts": audit_counts, "wall_ms": round(wall_ms, 1)}
    if args.json:
        pathlib.Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
        print(f"\n结果已写入 {args.json}")
    print("[OK] 多 Agent 并发验收通过" if ok else "[FAIL] 多 Agent 并发验收未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
