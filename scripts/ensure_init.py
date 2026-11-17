"""幂等初始化：容器启动时按"表里有没有数据"决定是否灌演示数据。

为什么不再用 `[ -f data/sku_demo.db ]`：切到 MySQL 后没有这个文件，判断会永远为真 /
永远为假；SQLite 与 MySQL 统一用"表 + 行数"判断，容器重建不会重复灌数据。

用法：
    python scripts/ensure_init.py              # Agent 库（SQLite 或 MySQL）
    python scripts/ensure_init.py --platform   # 数据中台库
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def ensure_agent() -> dict:
    from sqlalchemy import func, inspect, select

    from app.config import get_settings
    from app.db.engine import get_engine, init_schema, session_scope
    from app.db.models import KbChunk, Sku
    from app.db.seed import seed_database, seed_orders
    from app.pipeline.screening import screen
    from app.rag.index import build_index

    settings = get_settings()
    init_schema()
    engine = get_engine()
    tables = set(inspect(engine).get_table_names())
    with session_scope() as session:
        sku_count = int(session.scalar(select(func.count()).select_from(Sku)) or 0)
        if sku_count == 0:
            summary = seed_database(session, n_days=settings.seed_days, seed=settings.seed)
            results = screen(session)
            orders = seed_orders(session, seed=settings.seed)
            kb = build_index(session)
            return {
                "action": "seeded", "db": "mysql" if not settings.is_sqlite else "sqlite",
                "skus": summary.skus, "flows": summary.flows, "anomalies": len(results),
                "orders": orders, "kb_chunks": kb.chunks,
            }
        chunks = int(session.scalar(select(func.count()).select_from(KbChunk)) or 0)
        if chunks == 0:  # 知识库单独补建（重建库后常见）
            kb = build_index(session)
            return {"action": "kb_built", "db": "mysql" if not settings.is_sqlite else "sqlite",
                    "kb_chunks": kb.chunks}
        return {"action": "skip", "db": "mysql" if not settings.is_sqlite else "sqlite",
                "skus": sku_count, "kb_chunks": chunks,
                "tables": len(tables)}


def ensure_platform() -> dict:
    from sqlalchemy import func, select

    from data_platform.db import init_schema, session_scope

    init_schema()
    from data_platform.db import MartSkuMetrics

    with session_scope() as session:
        count = int(session.scalar(select(func.count()).select_from(MartSkuMetrics)) or 0)
    if count:
        return {"action": "skip", "mart_rows": count}
    # 复用 platform_bootstrap 的流程（模拟源 → 入湖 → dbt 转换）。
    # 数据源默认由中台自举（source_spec），不依赖 Agent 演示库；
    # 要用 Agent 库请设 PLATFORM_SOURCE_DB=<sqlite 路径>。
    sys.path.insert(0, str(ROOT / "scripts"))
    import platform_bootstrap

    try:
        code = platform_bootstrap.main([])
    except Exception as exc:  # noqa: BLE001 —— 转成可执行的提示，而不是抛栈
        raise BootstrapError(
            f"中台自举失败：{type(exc).__name__}: {exc}\n"
            "  排查顺序：① 目录 data/platform/raw 是否可写；② 若设了 PLATFORM_SOURCE_DB，"
            "该 SQLite 文件是否存在；③ 手动跑 `python scripts/platform_bootstrap.py` 看详细输出"
        ) from exc
    with session_scope() as session:
        count = int(session.scalar(select(func.count()).select_from(MartSkuMetrics)) or 0)
    if code != 0 or not count:
        raise BootstrapError("中台自举完成但指标宽表为空，请检查 ETL 日志（etl_log / etl_run_log）")
    return {"action": "bootstrapped", "mart_rows": count}


class BootstrapError(RuntimeError):
    """初始化失败：给运维可执行的下一步，而不是一串 Python 栈。"""


def main() -> int:
    parser = argparse.ArgumentParser(description="幂等初始化（Agent / 平台）")
    parser.add_argument("--platform", action="store_true")
    args = parser.parse_args()
    try:
        result = ensure_platform() if args.platform else ensure_agent()
    except BootstrapError as exc:
        print(f"ensure_init: ✗ {exc}", file=sys.stderr)
        return 1
    print(f"ensure_init: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
