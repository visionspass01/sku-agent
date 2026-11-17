"""生成订单/物流/售后演示数据（订单 MCP 的数据源）。

从现有 `flow` 表的真实 order_id 抽样，保证"订单 ↔ 流水"能对上；
物流/售后按订单状态确定性生成（seed 固定，可复现）。幂等：表里已有数据就跳过。

用法：
    python scripts/seed_orders.py          # 补数据（已有跳过）
    python scripts/seed_orders.py --fresh  # 先清空再生成
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import delete  # noqa: E402

from app.db.engine import init_schema, session_scope  # noqa: E402
from app.db.models import AfterSalesTicket, MockLogistics, MockOrder  # noqa: E402
from app.db.seed import SAMPLE_ORDERS, seed_orders  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="生成订单/物流/售后演示数据")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sample", type=int, default=SAMPLE_ORDERS)
    parser.add_argument("--fresh", action="store_true")
    args = parser.parse_args()

    init_schema()
    with session_scope() as session:
        if args.fresh:
            session.execute(delete(AfterSalesTicket))
            session.execute(delete(MockLogistics))
            session.execute(delete(MockOrder))
        result = seed_orders(session, seed=args.seed, sample=args.sample)
    print("订单数据：" + str(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
