"""演示：批量巡检（量化筛选 → Stream 入队 → 消费 → 多 Agent 研判）。

用法：python scripts/run_patrol.py [--mode hourly|daily] [--limit 5]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from app.db import repos  # noqa: E402
from app.db.engine import init_schema, session_scope  # noqa: E402
from app.db.models import KbChunk  # noqa: E402
from app.pipeline.patrol import run_patrol  # noqa: E402
from app.rag.index import build_index  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["hourly", "daily"], default="hourly")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    init_schema()
    with session_scope() as session:
        if session.query(KbChunk).count() == 0:
            build_index(session)
        run = repos.create_run(session, kind=args.mode, params={"mode": args.mode, "limit": args.limit})
        run_id = run.id

    summary = await run_patrol(mode=args.mode, limit=args.limit, run_id=run_id)
    with session_scope() as session:
        repos.finish_run(session, run_id, status="ok", stats=summary)

    print("=" * 64)
    print(f"巡检完成 run_id={summary['run_id']} mode={summary['mode']}")
    print(f"  筛出异常 SKU: {summary['screened']}（本次处理 {summary['selected']}）")
    print(f"  入队 {summary['enqueued']}，幂等去重 {summary['deduped']}")
    print(f"  研判完成 {summary['processed']}：ok={summary['ok']} degraded={summary['degraded']} failed={summary['failed']}")
    print(f"  总耗时: {summary['elapsed_ms'] / 1000:.1f}s")
    print(f"  队列: {summary['queue']}")
    print("=" * 64)
    print("提示：再次以相同小时窗口执行，Stream 幂等去重会拦截重复任务（deduped>0）。")


if __name__ == "__main__":
    asyncio.run(main())
