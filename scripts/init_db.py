"""初始化本地演示数据库：建表 → 灌模拟数据 → 执行一次量化筛选。

用法：python scripts/init_db.py [--fresh] [--days 30] [--now "2026-03-01 10:00"]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from app.config import get_settings  # noqa: E402
from app.db.engine import drop_schema, init_schema, session_scope  # noqa: E402
from app.db.seed import seed_database  # noqa: E402
from app.pipeline.screening import screen  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化 SKU 监管 Agent 演示数据")
    parser.add_argument("--fresh", action="store_true", help="删除现有表后重建（默认直接灌数）")
    parser.add_argument("--days", type=int, default=None, help="流水天数（默认配置值）")
    parser.add_argument("--seed", type=int, default=None, help="随机种子")
    parser.add_argument("--now", type=str, default=None, help="数据截止时间，如 '2026-03-01 10:00'")
    args = parser.parse_args()

    settings = get_settings()
    settings.ensure_dirs()
    now = None
    if args.now:
        now = dt.datetime.strptime(args.now, "%Y-%m-%d %H:%M")

    if args.fresh:
        drop_schema()
    init_schema()

    with session_scope() as session:
        summary = seed_database(
            session,
            now=now,
            n_days=args.days or settings.seed_days,
            seed=args.seed or settings.seed,
        )
        results = screen(session)

    # 订单/物流/售后演示数据（订单 MCP 用；幂等，已有数据会跳过）
    from app.db.seed import seed_orders

    with session_scope() as session:
        orders_summary = seed_orders(session, seed=args.seed or settings.seed)

    # 知识库索引（RAG 依赖；重建库后必须重建）
    from app.rag.index import build_index

    with session_scope() as session:
        kb_stats = build_index(session)

    print("=" * 64)
    print("模拟数据初始化完成")
    print(f"  时间范围      : {summary.start} ~ {summary.end}")
    print(f"  SKU 数量      : {summary.skus}")
    print(f"  出库流水      : {summary.flows:,} 行")
    print(f"  小时快照      : {summary.stat_rows:,} 行")
    print(f"  竞品行        : {summary.competitors}")
    print(f"  ground-truth  : {summary.labels} 条标注（异常 {summary.anomalies_labeled} 个）")
    print(f"  知识库        : {kb_stats.chunks} 个 chunk（新增 {len(kb_stats.added)} / 更新 {len(kb_stats.updated)}）")
    print(f"  订单/物流/售后: {orders_summary}")
    print(f"  本次筛选异常  : {len(results)} 个 SKU")
    for r in results[:12]:
        codes = ",".join(x["code"] for x in r.reasons)
        print(f"    - {r.sku_id:<16} [{r.severity}] {codes}")
    if len(results) > 12:
        print(f"    ... 其余 {len(results) - 12} 个见数据库 anomaly 表 / 看板")
    print("=" * 64)


if __name__ == "__main__":
    main()
