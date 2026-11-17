"""演示：多维度量化筛选（不出 LLM 调用，纯规则 + 回归特征）。

用法：python scripts/run_screening.py [--top 20]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from app.db.engine import session_scope  # noqa: E402
from app.pipeline.screening import screen  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    with session_scope() as session:
        results = screen(session)

    results.sort(key=lambda r: (r.severity != "高", r.sku_id))
    print(f"异常 SKU 共 {len(results)} 个（高 {sum(1 for r in results if r.severity == '高')} / "
          f"中 {sum(1 for r in results if r.severity != '高')}）\n")
    print(f"{'SKU':<16}{'严重度':<6}{'可售天数':>8}{'日均销量':>8}{'斜率':>8}{'退货率':>8}  原因")
    for r in results[: args.top]:
        m = r.metrics
        codes = " | ".join(x["code"] for x in r.reasons)
        print(f"{r.sku_id:<16}{r.severity:<6}{m['days_of_supply']:>8.1f}{m['base_daily']:>8.1f}"
              f"{m['slope_norm']:>8.2f}{m['return_rate'] * 100:>7.1f}%  {codes}")


if __name__ == "__main__":
    main()
