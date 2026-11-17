"""SQL 可移植性 + Redis 降级 + 幂等初始化：切 MySQL/Redis 时的回归防线。

三个容易踩的坑：
1. `func.iif` / `func.strftime` 是 SQLite 专有函数，MySQL 直接报错；
2. Redis 配了但挂了，缓存不应把对话/检索拖成 500；
3. 容器重启不能重复灌 300 SKU 演示数据。
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_no_dialect_specific_functions_past_the_guard():
    """`snapshot.py` 里 strftime/date_format 是双方言分支，其余不得出现 SQLite 专有函数。"""
    offenders: list[str] = []
    pattern = re.compile(r"func\.(iif|julianday|printf|unicode|instr|total)\b")
    for path in list((ROOT / "src/app").rglob("*.py")) + list((ROOT / "src/data_platform").rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"发现 SQLite 专有函数（MySQL 会报错）：{offenders}"


def test_snapshot_keeps_dual_dialect_branch():
    text = (ROOT / "src/app/pipeline/snapshot.py").read_text(encoding="utf-8")
    assert 'dialect == "mysql"' in text and "date_format" in text and "strftime" in text


def test_dbt_sales_uses_portable_case():
    text = (ROOT / "src/data_platform/dbt_like/models.py").read_text(encoding="utf-8")
    assert "case(" in text and "func.iif" not in text


def test_ttl_cache_degrades_when_redis_is_down():
    """Redis 抛异常时按 miss 处理（不缓存、不 500），并记录降级原因。"""
    from app.cache.ttl_cache import TTLCache

    class BrokenRedis:
        def get(self, key):
            from redis.exceptions import ConnectionError as RedisConnectionError

            raise RedisConnectionError("connection refused")

        def set(self, *args, **kwargs):
            from redis.exceptions import ConnectionError as RedisConnectionError

            raise RedisConnectionError("connection refused")

    cache = TTLCache(client=BrokenRedis())
    assert cache.get("k") is None
    cache.set("k", {"a": 1}, 60)
    calls = {"n": 0}

    def producer():
        calls["n"] += 1
        return {"v": 42}

    assert cache.get_or_set("k2", 60, producer) == {"v": 42}
    assert calls["n"] == 1
    snap = cache.snapshot()
    assert snap["degraded"] is True
    assert "Redis" in snap["degraded_reason"] or "connection" in snap["degraded_reason"].lower()


def test_health_cache_label_reflects_degradation(app_client):
    from app.cache.ttl_cache import get_ttl_cache

    cache = get_ttl_cache()
    cache.degraded = True
    cache.degraded_reason = "test"
    data = app_client.get("/api/health").json()
    assert data["cache"].endswith("(degraded)"), data["cache"]


def test_ensure_init_is_idempotent(seeded):
    """已有数据时 ensure_init 只跳过，不重复灌数。"""
    import scripts.ensure_init as ensure_init

    result = ensure_init.ensure_agent()
    assert result["action"] in ("skip", "kb_built"), result


def test_ensure_init_seeds_empty_db(tmp_path, monkeypatch):
    """空库（表存在但没数据）时 ensure_init 会灌数并建索引。"""
    import sys

    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))
    from sqlalchemy import create_engine

    import scripts.ensure_init as ensure_init
    from app.config import Settings, get_settings, set_settings
    from app.db.engine import get_engine, init_schema, set_engine

    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'empty.db').as_posix()}",
        seed_skus=6, seed_days=2, seed=7, redis_url="", mcp_transport="direct",
    )
    prev_settings = get_settings()
    prev_engine = get_engine()
    set_settings(settings)
    engine = create_engine(settings.database_url)
    set_engine(engine)
    init_schema(engine)
    try:
        result = ensure_init.ensure_agent()
        assert result["action"] == "seeded", result
        assert result["skus"] > 0 and result["kb_chunks"] > 0
        again = ensure_init.ensure_agent()
        assert again["action"] == "skip", again
    finally:
        # 关键：恢复到 fixture 的临时库，避免把后续用例切到默认 data/sku_demo.db
        set_engine(prev_engine)
        set_settings(prev_settings)
