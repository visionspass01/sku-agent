"""pytest 公共夹具：临时 SQLite + fakeredis + direct MCP + 确定性小数据集。"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(scope="session")
def _settings(tmp_path_factory):
    from app.config import Settings, set_settings

    tmp = tmp_path_factory.mktemp("sku_demo")
    # CI 里可以用 TEST_DATABASE_URL 把整套测试切到 MySQL，验证生产数据库分支
    # （ensure_columns 的 MySQL 方言、utf8mb4、事务隔离），而不是只测 SQLite。
    database_url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{(tmp / 'test.db').as_posix()}"
    seed_skus = int(os.environ.get("TEST_SEED_SKUS") or 300)
    settings = Settings(
        database_url=database_url,
        redis_url="",
        mcp_transport="direct",
        scheduler_enabled=False,
        # 测试只构造客户端对象、不发真实请求；但 openai SDK 在 api_key 为空时会直接抛错，
        # CI 环境没有 DEEPSEEK_API_KEY，所以要给一个占位值（不是真 Key，任何用例都不联网）。
        deepseek_api_key="sk-test-not-used",
        seed_days=7,
        seed_skus=seed_skus,
        cache_jitter_ratio=0.2,
        llm_max_concurrency=4,
        checkpoint_backend="sqlite",
        checkpoint_path=str(tmp / "checkpoints.sqlite"),
        # 默认关掉出口溯源校验：它不通过时会让模型重写答案（真实网络调用 + 结果不确定）。
        # 溯源本身有专门用例（tests/test_grounding.py、tests/test_red_team.py），
        # 那里会显式打开并注入桩 LLM。
        grounding_enabled=False,
        # 档案主文档默认「双份保留 + 确定性合并」，不调模型（离线可复现）。
        # LLM 摘要整合的用例（tests/test_analysis_concurrency.py）会显式切成 merge 并注入桩 LLM。
        analysis_merge_mode="keep-both",
        analysis_merge_inline=False,
        # 测试不写仓库 data/：部门映射缓存与目录同步都关掉（各自有用例显式打开）
        department_mapping_cache=False,
        catalog_sync_on_startup=False,
    )
    # 档案目录也放到临时目录，避免测试往仓库 data/ 里写文件
    settings.analyses_dir = str(tmp / "analyses")
    set_settings(settings)
    return settings


@pytest.fixture(scope="session")
def _db(_settings):
    from app.db.engine import init_schema, make_engine, set_engine

    engine = make_engine(_settings)
    set_engine(engine)
    init_schema()
    return engine


@pytest.fixture(scope="session")
def seeded(_db, _settings):
    """写入一个小型确定性数据集：3 个 SKU + 7 天小时流水 + ground-truth。"""
    from sqlalchemy import insert

    from app.db.engine import session_scope
    from app.db.models import (
        EvalLabel,
        Flow,
        InTransit,
        Sku,
        Supplier,
        Warehouse,
    )
    from app.pipeline.snapshot import rebuild_stat_hours

    now = dt.datetime(2026, 11, 2, 11, 0)
    start = now - dt.timedelta(days=7)
    rng = np.random.default_rng(11)
    dts = [start + dt.timedelta(hours=int(h)) for h in range(7 * 24 + 1)]
    flows: list[dict] = []

    with session_scope() as session:
        session.add_all([
            Warehouse(id="WH-HZ", name="杭州1仓", city="杭州"),
            Supplier(id="SUP-02", name="东莞丽丰针织", lead_time_days=7, moq=300, on_time_rate=0.85),
        ])
        session.flush()

        specs = [
            # (sku_id, style_id, category, size, stock, return_rate, label)
            ("ST-90-C1-S", "ST-90", "女装", "S", 0, 0.08, "断码"),
            ("ST-90-C1-M", "ST-90", "女装", "M", 60, 0.06, "normal"),
            ("ST-90-C1-L", "ST-90", "女装", "L", 0, 0.09, "断码"),
            ("ST-91-C1-M", "ST-91", "女装", "M", 900, 0.04, "过季积压"),
            ("ST-92-C1-M", "ST-92", "男装", "M", 900, 0.04, "过季积压"),
        ]
        for sku_id, style_id, category, size, stock, rr, label in specs:
            session.add(Sku(
                sku_id=sku_id, style_id=style_id, style_name="测试款",
                category=category, season="秋冬", color="黑色", size=size,
                warehouse_id="WH-HZ", platform="淘系", price=299, list_price=299,
                stock=stock, return_rate=rr, supplier_id="SUP-02", status="在售",
                list_date=(now - dt.timedelta(days=30)).date(),
            ))
        # 先落 SKU 再落子表：MySQL 是真实外键约束（1452），SQLAlchemy 不应假设它会按 FK 排序
        # ——和 `app/db/seed.py` 里踩过的坑同一类，SQLite 默认不校验外键所以本地看不出来。
        session.flush()

        for sku_id, _style_id, _category, _size, _stock, _rr, label in specs:
            session.add(EvalLabel(
                sku_id=sku_id, anomaly_type=label if label != "normal" else "normal",
                expected_risk={"断码": "高", "过季积压": "中"}.get(label, "低"),
            ))
            session.add(InTransit(
                sku_id=sku_id, qty=50, eta=(now + dt.timedelta(days=5)).date(),
                po_no="PO-TEST", status="在途",
            ))
        session.flush()

        # 流水：正常 SKU 平稳；断码 SKU 零库存无销量但同款有动销；积压 SKU 低销量
        daily_rates = {"ST-90-C1-M": 10.0, "ST-91-C1-M": 0.5, "ST-92-C1-M": 0.5}
        for sku_id, daily in daily_rates.items():
            for i, ts in enumerate(dts):
                q = int(rng.poisson(daily / 24.0)) if daily > 0 else 0
                if q > 0:
                    flows.append({
                        "sku_id": sku_id, "warehouse_id": "WH-HZ", "qty": q,
                        "amt": q * 250.0, "ts": ts, "flow_type": "OUT", "order_id": f"T{i}",
                    })
        session.execute(insert(Flow), flows)
        session.flush()
        rebuild_stat_hours(session, start, now)

    from app.pipeline.screening import screen

    with session_scope() as session:
        screen(session)

    from app.rag.index import build_index

    with session_scope() as session:
        build_index(session)

    return now


@pytest.fixture(scope="session")
def app_client(_settings, seeded):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def login(app_client):
    """登录辅助：返回 Authorization 头，供数据接口测试使用。"""

    def _login(username: str, password: str) -> dict:
        resp = app_client.post("/api/auth/login", json={"username": username, "password": password})
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['token']}"}

    return _login


@pytest.fixture(autouse=True)
def _fake_platform(monkeypatch):
    """默认把「中台」替换成以演示库为数据源的替身（不联网）。

    业务域上移到中台后，批量 Worker / 订单工具 / 对话工具全部走 REST；单测里没有真中台，
    所以默认装一个 `tests/fake_platform.py`。需要验证真实契约的用例（`test_platform*`、
    `test_mcp_platform_adapter`）用 `httpx.ASGITransport` 指向真实 app，会覆盖本夹具。
    需要验证故障注入的用例自行 `set_platform_client(...)` / `set_transport(...)`，同样覆盖。
    """
    import httpx
    from fake_platform import handler

    from app.mcp_servers import _platform as mcp_platform
    from app.tools import platform_tools

    transport = httpx.MockTransport(handler)
    platform_tools.set_platform_client(platform_tools.PlatformClient(
        base_url="http://fake.platform", token="demo-internal-token", transport=transport,
    ))
    mcp_platform.set_transport(transport)
    try:
        yield transport
    finally:
        platform_tools.set_platform_client(None)
        mcp_platform.reset_transport()


@pytest.fixture(autouse=True)
def _image_context_window(monkeypatch):
    """生图侧的两个后台行为在测试里默认关掉：

    - 商品上下文合并窗口（真实 3 秒会让整套用例变慢）；
    - 看门狗周期扫描（会重投手工塞进库的 `queued` 任务，导致同一任务被执行两次、
      provider 调用次数与结算次数翻倍 —— 这是跨用例串味的常见来源）。

    专门验证它们的用例自己把 `IMAGE_CONTEXT_BATCH_WINDOW_SECONDS` /
    `IMAGE_RECOVERY_INTERVAL_SECONDS` 改成需要的值并 `reset_image_settings()` 覆盖。
    """
    monkeypatch.setenv("IMAGE_CONTEXT_BATCH_WINDOW_SECONDS", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("IMAGE_OUTBOX_REPLAY_SECONDS", "0")


def _reset_process_singletons() -> None:
    """重置进程级单例：MCP hub（带熔断状态）与 RAG 检索器（绑在构建时的库上）。

    不重置会跨用例传染：一个用例打出的熔断会让后续用例全部快失败；
    一个用例用临时库构建的检索器会让后续用例检索到别的知识库。
    """
    from app.agents_image.sources.batch_context import reset_product_batcher
    from app.mcp_client import client as mcp_client
    from app.rag.hybrid import invalidate_retriever

    mcp_client._hub = None
    invalidate_retriever()
    reset_product_batcher()


@pytest.fixture(autouse=True)
def _reset_singletons():
    from app.auth import reset_login_throttle
    from app.cache.redis_factory import reset_redis
    from app.cache.stream_queue import reset_stream_queue
    from app.cache.ttl_cache import reset_ttl_cache
    from app.llm import reset_llm
    from app.observability import latency, runtime_state, tool_stats
    from app.rag import conflict as conflict_mod
    from app.rag.loader import reset_coverage_cache

    reset_redis()
    reset_stream_queue()
    reset_ttl_cache()
    reset_llm()
    reset_login_throttle()
    reset_coverage_cache()
    conflict_mod.reset_conflict_stats()
    latency.reset()
    tool_stats.reset()
    runtime_state.reset()
    _reset_process_singletons()
    yield
    reset_redis()
    reset_stream_queue()
    reset_ttl_cache()
    reset_llm()
    reset_login_throttle()
    reset_coverage_cache()
    conflict_mod.reset_conflict_stats()
    latency.reset()
    tool_stats.reset()
    runtime_state.reset()
    _reset_process_singletons()
