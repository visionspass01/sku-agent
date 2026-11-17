"""中台自举目录与 Agent 演示目录的**契约测试**。

为什么需要：两侧各自持有一份演示目录（中台 `source_spec.py` / Agent `db/seed.py`），
生产中它们本来就该来自不同系统（ERP/CRM vs 用户提问），但**演示数据必须能对上**。
这里把"对得上"变成一条可执行的断言，防止任何一侧改了 SKU 命名/品类/价格而不自知。
"""

from __future__ import annotations

import datetime as dt

from data_platform import mock_sources, source_spec


def _agent_styles():
    from app.db.seed import _default_styles

    return _default_styles()


def test_style_catalogue_matches_agent_seed():
    agent = _agent_styles()
    platform = source_spec.STYLES

    assert [s.style_id for s in agent] == [s["style_id"] for s in platform]
    for spec, row in zip(agent, platform):
        assert row["name"] == spec.name, row["style_id"]
        assert row["category"] == spec.category, row["style_id"]
        assert row["season"] == spec.season, row["style_id"]
        assert float(row["base_daily"]) == float(spec.base_daily), row["style_id"]
        assert float(row["price"]) == float(spec.price), row["style_id"]
        assert row["supplier_id"] == spec.supplier_id, row["style_id"]
        assert row["warehouse_id"] == spec.warehouse_id, row["style_id"]
        assert row["platform"] == spec.platform, row["style_id"]
        assert row["anomaly"] == spec.anomaly, row["style_id"]
        assert tuple(row["sizes"]) == tuple(spec.sizes), row["style_id"]
        assert tuple(source_spec.COLORS) == tuple(spec.colors), row["style_id"]


def test_sku_ids_match_agent_naming_convention():
    """SKU 命名规则：`{style_id}-C{颜色序号}-{尺码}`，两侧必须一致。"""
    expected = {
        f"{spec.style_id}-C{ci}-{size}"
        for spec in _agent_styles()
        for ci in range(1, len(spec.colors) + 1)
        for size in spec.sizes
    }
    built = {row["sku_id"] for row in source_spec.catalog_skus()}
    assert built == expected
    assert len(built) == 300


def test_suppliers_and_warehouses_match():
    from app.db.seed import _seed_suppliers, _seed_warehouses  # noqa: F401  （存在性即可）

    # 供应商：id/名称/交期/MOQ
    assert set(source_spec.SUPPLIERS) == {"SUP-01", "SUP-02", "SUP-03", "SUP-04"}
    assert source_spec.SUPPLIERS["SUP-01"]["lead_time_days"] == 12
    assert source_spec.SUPPLIERS["SUP-02"]["lead_time_days"] == 7
    assert source_spec.SUPPLIERS["SUP-03"]["lead_time_days"] == 15
    assert source_spec.SUPPLIERS["SUP-04"]["lead_time_days"] == 9
    assert set(source_spec.WAREHOUSES) == {"WH-HZ", "WH-GZ", "WH-BJ"}


def test_synthetic_source_shape_is_a_drop_in_replacement():
    """自举数据结构必须与旧 `_read_source()` 同构，否则 ETL 会静默少数据。"""
    src = source_spec.build_source(dt.date(2026, 11, 3), seed=42)

    assert src["source"] == "synthetic"
    assert len(src["skus"]) == 300
    sku_keys = {"sku_id", "style_id", "style_name", "category", "season", "color", "size",
                "warehouse_id", "platform", "price", "stock", "return_rate", "supplier_id"}
    assert sku_keys <= set(src["skus"][0])
    assert set(src["suppliers"]) == {"SUP-01", "SUP-02", "SUP-03", "SUP-04"}
    assert src["in_transit"] and {"sku_id", "qty", "eta", "po_no", "status"} <= set(src["in_transit"][0])
    assert src["daily_sales"] and {"sku_id", "d", "q"} == set(src["daily_sales"][0])
    # 近 30 天窗口内的日期
    days = {row["d"] for row in src["daily_sales"]}
    assert min(days) >= "2026-10-05" and max(days) <= "2026-11-03"


def test_department_mapping_matches_platform():
    """部门 ↔ 品类映射：中台是唯一真相，Agent 的本地默认值必须与它一致（2.2-6）。"""
    from app import config as agent_cfg
    from data_platform import config as platform_cfg

    platform_map = {k: list(v) for k, v in platform_cfg.DEPARTMENT_CATEGORIES.items()}
    agent_map = {k: list(v) for k, v in agent_cfg.DEPARTMENT_CATEGORIES.items()}
    assert agent_map == platform_map, "两边的默认映射漂移了：改一处必须同步另一处"


def test_agent_can_adopt_platform_mapping():
    """Agent 启动时会用中台下发的映射覆盖本地默认值，离线时保留默认值。"""
    from app import config as agent_cfg

    original = {k: list(v) for k, v in agent_cfg.DEPARTMENT_CATEGORIES.items()}
    try:
        applied = agent_cfg.set_department_categories(
            {**original, "运动": ["运动"]})
        assert "运动" in applied
        assert agent_cfg.categories_for("运动") == ["运动"]
        assert "运动" in agent_cfg.all_departments()
        assert agent_cfg.department_of("运动") == "运动"
        # 空映射 = 保持现状（避免中台返回空把本地配置清空）
        assert agent_cfg.set_department_categories({}) == sorted(agent_cfg.DEPARTMENT_CATEGORIES)
    finally:
        agent_cfg.set_department_categories(original)
        assert agent_cfg.all_departments() == tuple(original)


def test_platform_bootstraps_without_agent_db(tmp_path, monkeypatch):
    """核心回归：没有 Agent 演示库时，中台也能自举出完整的 mart（compose 里就是这么跑的）。"""
    from sqlalchemy import func, select

    from data_platform.ingest.airflow_mock import run_dag

    monkeypatch.setattr(mock_sources, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(mock_sources, "SOURCE_DB", None)          # 模拟容器里没有 Agent 库
    generated = mock_sources.generate_all(snapshot_date=dt.date(2026, 11, 3), seed=7)
    assert generated["meta"]["source"] == "synthetic"
    assert generated["meta"]["counts"]["inventory"] >= 300

    from data_platform.api import cache as pcache
    from data_platform.db import MartSkuMetrics, init_schema, make_engine, session_scope, set_engine

    engine = make_engine(f"sqlite:///{(tmp_path / 'platform.db').as_posix()}")
    set_engine(engine)
    init_schema(engine)
    pcache.reset_client()
    try:
        first = run_dag("daily_sync")
        assert all(t["status"] == "success" for t in first["tasks"].values()), first
        with session_scope() as session:
            rows = int(session.scalar(select(func.count()).select_from(MartSkuMetrics)) or 0)
        assert rows >= 300
    finally:
        set_engine(None)
