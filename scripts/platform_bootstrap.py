"""数据中台一键初始化：生成模拟源 → 模拟 Airflow 入湖 → dbt 式转换 → 输出校验样例。

数据源默认**由中台自举**（`data_platform/source_spec.py`，不依赖 Agent 演示库）；
要复用 Agent 演示库（本地便利路径）用 `--from-agent-db <path>`。

用法：
    python scripts/platform_bootstrap.py
    python scripts/platform_bootstrap.py --from-agent-db data/sku_demo.db
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="初始化数据中台（模拟源 → 入湖 → dbt）")
    parser.add_argument("--from-agent-db", default="",
                        help="复用 Agent 演示库（SQLite 路径）；默认用中台自带目录自举")
    args = parser.parse_args(argv)

    if args.from_agent_db:
        # 必须在 import data_platform.config 之前设置（config 在导入期读环境变量）
        os.environ["PLATFORM_SOURCE_DB"] = str(Path(args.from_agent_db).expanduser())

    from data_platform.config import ensure_dirs

    ensure_dirs()

    from data_platform.db import init_schema, session_scope

    init_schema()

    from data_platform.mock_sources import generate_all

    generated = generate_all()
    source = generated["meta"].get("source", "synthetic")
    print(f"[1/3] 模拟源快照已生成（source={source}）：{generated['meta']['counts']}")

    from data_platform.ingest.airflow_mock import run_dag

    result = run_dag("daily_sync")
    print(f"[2/3] 模拟 Airflow 跑批完成 run_id={result['run_id']}")
    for task, info in result["tasks"].items():
        print(f"      {task:<14} {info['status']:<8} rows={info['rows']:<6} {info['elapsed_ms']}ms")

    from sqlalchemy import select

    from data_platform.db import MartSkuMetrics

    samples: list[dict] = []
    with session_scope() as session:
        rows = session.scalars(select(MartSkuMetrics).limit(3)).all()
        for r in rows:
            samples.append({
                "sku_id": r.sku_id, "available": r.available, "in_transit_7d": r.in_transit_7d,
                "daily_avg_7d": r.daily_avg_7d, "safety_stock": r.safety_stock,
                "reorder_point": r.reorder_point, "sell_through_days": r.sell_through_days,
                "stockout_risk": r.stockout_risk,
            })
    print("[3/3] 指标宽表样例：")
    for r in samples:
        print(f"      {r['sku_id']}: 可用={r['available']} 在途7d={r['in_transit_7d']} 日均7d={r['daily_avg_7d']} "
              f"安全库存={r['safety_stock']} 再订货点={r['reorder_point']} 售罄天数={r['sell_through_days']} "
              f"风险={r['stockout_risk']}")
    if not samples:
        print("      ✗ 宽表为空：检查 ETL 日志（etl_run_log）与 raw 快照目录")
        return 1
    print("\n数据中台就绪。启动服务：uvicorn data_platform.api.main:app --host 127.0.0.1 --port 8001")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
