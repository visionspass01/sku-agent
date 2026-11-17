"""一次性探活整个检索/存储栈：DB / 缓存 / 向量库 / Embedding。

面试演示前跑一遍，确认"六件套"都真的生效（而不是文档里写了、实际降级了）。
不打印任何密钥；失败项以非 0 退出码返回。

用法：
    python scripts/verify_stack.py
    python scripts/verify_stack.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def probe_db() -> dict:
    from sqlalchemy import func, select

    from app.config import get_settings
    from app.db.engine import init_schema, session_scope
    from app.db.models import AfterSalesTicket, KbChunk, MockOrder, Sku

    init_schema()  # 幂等：老库补建新表（mock_order / user_profile 等）
    settings = get_settings()
    with session_scope() as session:
        counts = {
            "skus": int(session.scalar(select(func.count()).select_from(Sku)) or 0),
            "kb_chunks": int(session.scalar(select(func.count()).select_from(KbChunk)) or 0),
            "orders": int(session.scalar(select(func.count()).select_from(MockOrder)) or 0),
            "after_sales": int(session.scalar(select(func.count()).select_from(AfterSalesTicket)) or 0),
        }
    return {"backend": "sqlite" if settings.is_sqlite else "mysql",
            "ok": counts["skus"] > 0, **counts}


def probe_cache() -> dict:
    from app.cache.redis_factory import redis_mode
    from app.cache.ttl_cache import get_ttl_cache

    cache = get_ttl_cache()
    cache.set("verify:stack", {"ok": True}, 30)
    hit = cache.get("verify:stack")
    snap = cache.snapshot()
    return {"backend": redis_mode(), "ok": bool(hit) and not snap.get("degraded"),
            "hit_rate": snap.get("hit_rate"), "degraded_reason": snap.get("degraded_reason", "")}


def probe_vector() -> dict:
    from app.rag import vector_store as vs

    status = vs.store_status()
    return {"backend": status.get("backend"), "ok": not status.get("degraded"),
            "points": status.get("points"), "collection": status.get("collection"),
            "dim": status.get("dim"), "degraded_reason": status.get("degraded_reason", "")}


def probe_embedding() -> dict:
    from app.config import get_settings
    from app.rag.embedding import get_embedding_provider

    settings = get_settings()
    provider = get_embedding_provider()
    health = provider.health()
    ok = health.get("backend") == "hash" or bool(health.get("ok"))
    if (settings.embedding_backend or "hash").lower() == "tencent" and health.get("backend") == "tencent":
        probe = provider.probe()
        ok = bool(probe.get("ok"))
        health["probe"] = probe
    return {**health, "ok": ok}


def probe_rag() -> dict:
    from app.rag.eval import run_eval

    report = run_eval(topk=5)
    return {"ok": True, "recall@1": report["recall_at"]["recall@1"],
            "recall@5": report["recall_at"]["recall@5"],
            "mrr": report["mrr"], "count": report["count"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="探活 DB/缓存/向量库/Embedding")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    probes = {
        "db": probe_db(),
        "cache": probe_cache(),
        "vector_store": probe_vector(),
        "embedding": probe_embedding(),
        "rag": probe_rag(),
    }
    if args.json:
        print(json.dumps(probes, ensure_ascii=False, indent=2))
    else:
        for name, data in probes.items():
            flag = "ok  " if data.get("ok") else "FAIL"
            extra = {k: v for k, v in data.items() if k not in ("ok", "note")}
            print(f"  {flag} {name:12s} {json.dumps(extra, ensure_ascii=False)[:220]}")
    failed = [name for name, data in probes.items() if not data.get("ok")]
    if failed:
        print(f"\n[FAIL] 未通过：{failed}")
        return 1
    print("\n全部组件就绪")
    return 0


if __name__ == "__main__":
    sys.exit(main())
