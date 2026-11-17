"""模拟 dbt run：按依赖顺序执行转换模型，并刷新治理元数据版本（供缓存失效）。"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from data_platform.db import MetaDataVersion, session_scope
from data_platform.dbt_like import models
from data_platform.governance import seed_governance


def run_transforms(session: Session, as_of: dt.date | None = None) -> dict:
    as_of = as_of or dt.date.today()
    stages = [
        ("stg_erp_inventory", lambda: models.build_stg_erp_inventory(session)),
        ("stg_orders", lambda: models.build_stg_orders(session)),
        ("dim_sku", lambda: models.build_dim_sku(session)),
        # 款维度必须在 SKU 维度之后：size_spec / color_count 由该款的 SKU 集合推导
        ("dim_style", lambda: models.build_dim_style(session)),
        ("dim_customer", lambda: models.build_dim_customer(session)),
        ("fct_inventory_daily", lambda: models.build_fct_inventory_daily(session, as_of)),
        ("fct_sales_daily", lambda: models.build_fct_sales_daily(session, as_of)),
        ("fct_in_transit", lambda: models.build_fct_in_transit(session, as_of)),
        ("mart_sku_metrics", lambda: models.build_mart_sku_metrics(session, as_of)),
        # 款级 mart 依赖 SKU 级 mart（尺码曲线是 SKU 聚合出来的）
        ("mart_style_metrics", lambda: models.build_mart_style_metrics(session, as_of)),
    ]
    rows: dict[str, int] = {}
    for name, fn in stages:
        rows[name] = int(fn())
    seed_governance(session)

    version = session.get(MetaDataVersion, "mart")
    if version is None:
        version = MetaDataVersion(key="mart", version=1, updated_at=dt.datetime.now())
        session.add(version)
    else:
        version.version += 1
        version.updated_at = dt.datetime.now()
    session.flush()
    return {"as_of": as_of.isoformat(), "rows": rows, "data_version": version.version}


def run_pipeline(as_of: dt.date | None = None) -> dict:
    """便捷入口：仅执行转换（不含接入）。"""
    with session_scope() as session:
        return run_transforms(session, as_of=as_of)
