"""演示：单 SKU 全链路研判（Supervisor → Worker → Skill 子图 → 预案 → 校验）。

用法：python scripts/demo_single.py --sku ST-05-C1-M [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from app.config import get_settings  # noqa: E402
from app.db import repos  # noqa: E402
from app.db.engine import init_schema, session_scope  # noqa: E402
from app.db.models import KbChunk  # noqa: E402
from app.mcp_client.client import get_mcp_hub, shutdown_mcp_hub  # noqa: E402
from app.pipeline.patrol import inspect_sku  # noqa: E402
from app.rag.index import build_index  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sku", default="ST-05-C1-M")
    parser.add_argument("--json", action="store_true", help="打印完整 JSON 结果")
    args = parser.parse_args()

    settings = get_settings()
    settings.ensure_dirs()
    init_schema()
    with session_scope() as session:
        if session.query(KbChunk).count() == 0:
            print("[kb] 知识库为空，先构建索引 ...")
            stats = build_index(session)
            print("[kb]", stats.to_dict())
        run = repos.create_run(session, kind="single", params={"sku_id": args.sku, "source": "cli"})
        run_id = run.id

    hub = get_mcp_hub()
    await hub.startup()
    print(f"[mcp] hub 启动完成（transport={settings.mcp_transport}）")
    print(f"[run] {args.sku} run_id={run_id}")
    result = await inspect_sku(args.sku, run_id)
    await shutdown_mcp_hub()

    final = result.get("final_state") or {}
    with session_scope() as session:
        repos.finish_run(session, run_id, status=result["status"], stats={"elapsed_ms": result["elapsed_ms"]},
                         error=result.get("error"))

    if args.json:
        print(json.dumps(final, ensure_ascii=False, indent=2, default=str))
        return

    plan = final.get("plan") or {}
    judgment = final.get("judgment") or {}
    replenish = final.get("replenish") or {}
    verification = final.get("verification") or {}
    erp = (final.get("data") or {}).get("erp") or {}
    sku = erp.get("sku") or {}

    print("=" * 72)
    print(f"SKU {sku.get('sku_id')} {sku.get('style_name')} {sku.get('color')}/{sku.get('size')} "
          f"库存={sku.get('stock')} 退货率={sku.get('return_rate')}")
    print(f"状态={result['status']} 耗时={result['elapsed_ms'] / 1000:.1f}s 步骤数={len(final.get('trace') or [])}")
    if result.get("error"):
        print(f"[error] {result['error']}")
    print(f"\n[风险] {plan.get('risk_level')}  {plan.get('verdict')}")
    print("\n[证据]")
    for e in plan.get("evidence") or []:
        print("  -", e)
    print("\n[预案]")
    for a in plan.get("plan") or []:
        print(f"  - {a.get('action')} qty={a.get('qty')} eta={a.get('eta_days')} 理由: {a.get('reason')}")
    print("\n[Skill 研判] flags=", judgment.get("risk_flags"), "健康度=", judgment.get("health_score"),
          "可售天数=", judgment.get("days_of_supply"))
    print("[补货测算] 建议=", replenish.get("suggested_qty"), "再订货点=", replenish.get("reorder_point"),
          "MOQ=", replenish.get("moq"), "交期=", replenish.get("lead_time_days"))
    print("\n[引用]")
    rag_citations = {c.get("chunk_id"): c for c in ((final.get("data") or {}).get("rag") or {}).get("citations") or []}
    for cid in plan.get("citations") or []:
        c = rag_citations.get(cid) or {}
        print(f"  - {cid}  {c.get('title', '')}  {str(c.get('snippet', ''))[:80]}")
    print(f"\n[校验 Agent] passed={verification.get('passed')}")
    for k, v in (verification.get("compliance") or {}).items():
        print(f"  - {k}: {'✓' if v else '✗'}")
    for issue in verification.get("issues") or []:
        print("  !", issue)
    print("\n[执行轨迹]")
    for step in final.get("trace") or []:
        print(f"  [{step.get('node'):<14}] {step.get('status'):<9} {step.get('ms'):>6}ms  {step.get('detail', '')}")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
