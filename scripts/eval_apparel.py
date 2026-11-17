"""服装口径评测：齐码率 / 断码率 / 尺码曲线 / 周转 / 售罄 / 生命周期（离线，可作 CI 门槛）。

为什么单独一个脚本：这些指标的共同风险是「看着像对的」——
`size_share` 的分母取错、断码没区分主销码、周转用了 7 天口径……单测能覆盖实现，
但**口径是否与业务定义一致**必须用「独立复算」来证明：本脚本从 mart 明细重新算一遍，
再和接口输出比对，两边不一致就失败。

用法：
    python scripts/eval_apparel.py                  # 全量检查
    python scripts/eval_apparel.py --min-pass 1.0   # CI 门槛（低于阈值返回非 0）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

FULL = {"X-Internal-Token": "k-sku"}
MENS = {"X-Internal-Token": "k-mens"}

KEYS = {
    "sku-agent": {"key": "k-sku", "scopes": ["read", "write", "approve"], "departments": None},
    "analysis-agent": {"key": "k-mens", "scopes": ["read"], "departments": ["男装"]},
}


def _setup(tmp: Path) -> None:
    from data_platform import config
    from data_platform.api import authz, quota
    from data_platform.api import cache as pcache
    from data_platform.db import init_schema, make_engine, set_engine
    from data_platform.ingest.airflow_mock import run_dag
    from data_platform.mock_sources import generate_all

    config.API_KEYS_RAW = json.dumps(KEYS, ensure_ascii=False)
    config.RATE_LIMIT_PER_MINUTE = 600
    authz.reset_principals()
    quota.reset()

    engine = make_engine(f"sqlite:///{(tmp / 'platform.db').as_posix()}")
    set_engine(engine)
    init_schema(engine)
    pcache.reset_client()
    generate_all(snapshot_date=dt.date(2026, 11, 3), seed=7)
    result = run_dag("daily_sync")
    assert all(t["status"] == "success" for t in result["tasks"].values()), result


def _mart_snapshot() -> dict:
    """从 mart 明细取一份快照（复算的输入）。"""
    from sqlalchemy import select

    from data_platform.db import MartSkuMetrics, MartStyleMetrics, session_scope

    with session_scope() as session:
        skus = [
            {"sku_id": r.sku_id, "style_id": r.style_id, "size": r.size, "color": r.color,
             "department": r.department, "available": int(r.available or 0),
             "qty_30d": int(r.qty_30d or 0), "daily_avg_30d": float(r.daily_avg_30d or 0),
             "daily_avg_7d": float(r.daily_avg_7d or 0),
             "size_share": float(r.size_share or 0),
             "main_size_flag": int(r.main_size_flag or 0),
             "size_gap_flag": int(r.size_gap_flag or 0),
             "turnover_days": r.turnover_days,
             "sell_through_rate": float(r.sell_through_rate or 0),
             "lifecycle_stage": r.lifecycle_stage,
             "days_since_launch": r.days_since_launch}
            for r in session.scalars(select(MartSkuMetrics))
        ]
        styles = [
            {"style_id": r.style_id, "department": r.department,
             "size_count": int(r.size_count or 0), "listed_size_count": int(r.listed_size_count or 0),
             "full_size_flag": int(r.full_size_flag or 0),
             "broken_size_count": int(r.broken_size_count or 0),
             "size_gap_rate": float(r.size_gap_rate or 0),
             "total_available": int(r.total_available or 0), "qty_30d": int(r.qty_30d or 0),
             "sell_through_rate": float(r.sell_through_rate or 0),
             "turnover_days": r.turnover_days,
             "days_since_launch": r.days_since_launch,
             "lifecycle_stage": r.lifecycle_stage,
             "size_curve": r.size_curve or [],
             "category": r.category, "season": r.season}
            for r in session.scalars(select(MartStyleMetrics))
        ]
    return {"skus": skus, "styles": styles}


def _expected(snapshot: dict) -> dict:
    """独立复算：只依赖 SKU 明细，不参考任何已算好的字段。"""
    by_style: dict[str, list[dict]] = {}
    for row in snapshot["skus"]:
        by_style.setdefault(row["style_id"], []).append(row)

    out = {"styles": {}, "overview": {}}
    for style_id, rows in by_style.items():
        qty_total = sum(r["qty_30d"] for r in rows)
        available_total = sum(r["available"] for r in rows)
        daily30_total = sum(r["daily_avg_30d"] for r in rows)
        size_qty: dict[str, int] = {}
        size_available: dict[str, int] = {}
        for row in rows:
            size_qty[row["size"]] = size_qty.get(row["size"], 0) + row["qty_30d"]
            size_available[row["size"]] = size_available.get(row["size"], 0) + row["available"]
        shares = {s: (round(q / qty_total, 4) if qty_total else 0.0)
                  for s, q in size_qty.items()}
        main_sizes = {s for s, share in shares.items() if share >= 0.15}
        gaps = {s for s in main_sizes if size_available.get(s, 0) <= 0}
        sell_through = (qty_total / (qty_total + available_total)
                        if (qty_total + available_total) else 0.0)
        out["styles"][style_id] = {
            "qty_total": qty_total,
            "shares": shares,
            "main_sizes": sorted(main_sizes),
            "gaps": sorted(gaps),
            "full_size": all(size_available.get(s, 0) > 0 for s in size_qty),
            "listed_sizes": sorted(s for s, a in size_available.items() if a > 0),
            "sell_through_rate": round(sell_through, 4),
            "turnover_days": (round(available_total / daily30_total, 2)
                              if daily30_total > 0 else None),
            "size_gap_rate": (round(len(gaps) / len(size_qty), 4) if size_qty else 0.0),
        }

    total = len(out["styles"])
    full = sum(1 for s in out["styles"].values() if s["full_size"])
    broken = sum(1 for s in out["styles"].values() if s["gaps"])
    turnovers = [s["turnover_days"] for s in out["styles"].values()
                 if s["turnover_days"] is not None]
    out["overview"] = {
        "style_count": total,
        "full_size_styles": full,
        "broken_size_styles": broken,
        "full_size_rate": round(full / total, 4) if total else 0.0,
        "broken_size_rate": round(broken / total, 4) if total else 0.0,
        "avg_turnover_days": round(sum(turnovers) / len(turnovers), 2) if turnovers else None,
    }
    return out


def _matrix(client, snapshot: dict, expected: dict) -> list[dict]:  # noqa: ANN001
    cases: list[dict] = []

    def case(name: str, ok: bool, detail: str = "") -> None:
        cases.append({"name": name, "ok": bool(ok), "detail": detail})

    # 1) 尺码曲线：占比与复算一致、分母是「同款 30 天总量」
    bad_share = []
    for style_id, exp in expected["styles"].items():
        body = client.get(f"/api/v1/metrics/size-curve?style_id={style_id}", headers=FULL).json()
        for item in body.get("items") or []:
            want = exp["shares"].get(item["size"], 0.0)
            if abs(float(item["size_share"]) - want) > 1e-6:
                bad_share.append({"style": style_id, "size": item["size"],
                                  "got": item["size_share"], "want": want})
    case("尺码曲线占比与独立复算一致", not bad_share, str(bad_share[:3]))

    # 2) 主销码阈值：≥15% 才算主销
    bad_main = []
    for style_id, exp in expected["styles"].items():
        body = client.get(f"/api/v1/styles/{style_id}/metrics", headers=FULL).json()
        got = {c["size"] for c in body["size_curve"] if c["main_size_flag"]}
        if got != set(exp["main_sizes"]):
            bad_main.append({"style": style_id, "got": sorted(got), "want": exp["main_sizes"]})
    case("主销码（占比 ≥15%）判定与复算一致", not bad_main, str(bad_main[:3]))

    # 3) 断码只发生在主销码上
    bad_gap = []
    for style_id, exp in expected["styles"].items():
        body = client.get(f"/api/v1/styles/{style_id}/metrics", headers=FULL).json()
        got = sorted(body["size_gap"]["missing_sizes"])
        if got != exp["gaps"]:
            bad_gap.append({"style": style_id, "got": got, "want": exp["gaps"]})
    case("断码尺码与复算一致（且只在主销码）", not bad_gap, str(bad_gap[:3]))

    # 4) 齐码标记：所有应有尺码都可用
    bad_full = []
    for style_id, exp in expected["styles"].items():
        body = client.get(f"/api/v1/styles/{style_id}/metrics", headers=FULL).json()
        if bool(body["full_size_flag"]) != exp["full_size"]:
            bad_full.append({"style": style_id, "got": body["full_size_flag"],
                             "want": exp["full_size"]})
    case("齐码标记与复算一致", not bad_full, str(bad_full[:3]))

    # 5) 售罄率与周转天数公式
    bad_ratio = []
    for style_id, exp in expected["styles"].items():
        body = client.get(f"/api/v1/styles/{style_id}/metrics", headers=FULL).json()
        if abs(float(body["sell_through_rate"]) - exp["sell_through_rate"]) > 1e-4:
            bad_ratio.append({"style": style_id, "field": "sell_through_rate",
                              "got": body["sell_through_rate"], "want": exp["sell_through_rate"]})
        if (body["turnover_days"] is None) != (exp["turnover_days"] is None) or (
                body["turnover_days"] is not None
                and abs(float(body["turnover_days"]) - exp["turnover_days"]) > 0.02):
            bad_ratio.append({"style": style_id, "field": "turnover_days",
                              "got": body["turnover_days"], "want": exp["turnover_days"]})
    case("售罄率 / 周转天数与复算一致（口径 30 天）", not bad_ratio, str(bad_ratio[:3]))

    # 6) 部门概览与款级明细一致
    body = client.get("/api/v1/metrics/apparel-overview", headers=FULL).json()
    want = expected["overview"]
    mismatch = {k: {"got": body.get(k), "want": v} for k, v in want.items()
                if body.get(k) != v}
    case("部门概览与款级明细一致", not mismatch, str(mismatch))

    # 7) 断码清单与概览口径一致
    gap_body = client.get("/api/v1/metrics/size-gap", headers=FULL).json()
    case("断码清单条数与概览的断码款数一致",
         gap_body["count"] == want["broken_size_styles"],
         f'{gap_body["count"]} vs {want["broken_size_styles"]}')
    case("断码清单的齐码率与概览一致",
         gap_body["full_size_rate"] == want["full_size_rate"],
         f'{gap_body["full_size_rate"]} vs {want["full_size_rate"]}')

    # 8) 生命周期规则自洽
    from data_platform.dbt_like.models import lifecycle_of

    bad_life = [s["style_id"] for s in snapshot["styles"]
                if s["lifecycle_stage"] != lifecycle_of(s["days_since_launch"],
                                                        s["sell_through_rate"])]
    case("生命周期阶段与规则函数一致", not bad_life, str(bad_life[:3]))

    # 9) 部门作用域：男装 key 看不到女装款
    women = next(s["style_id"] for s in snapshot["styles"] if s["department"] == "女装")
    mens = next(s["style_id"] for s in snapshot["styles"] if s["department"] == "男装")
    case("男装 key 读女装款被 403",
         client.get(f"/api/v1/styles/{women}/metrics", headers=MENS).status_code == 403)
    case("男装 key 读男装款正常",
         client.get(f"/api/v1/styles/{mens}/metrics", headers=MENS).status_code == 200)

    # 10) 断码款在清单里必须给出具体缺失尺码
    items = gap_body["items"]
    case("断码清单包含缺失尺码明细",
         all(i["missing_sizes"] for i in items) if items else True,
         str([i["missing_sizes"] for i in items][:3]))

    return cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="服装口径评测（独立复算）")
    parser.add_argument("--min-pass", type=float, default=1.0)
    parser.add_argument("--json", default="", help="把结果写到指定 json 文件")
    args = parser.parse_args(argv)

    from fastapi.testclient import TestClient

    tmp = Path(tempfile.mkdtemp(prefix="eval_apparel_"))
    _setup(tmp)
    from data_platform.api.main import app

    snapshot = _mart_snapshot()
    expected = _expected(snapshot)
    with TestClient(app) as client:
        cases = _matrix(client, snapshot, expected)

    passed = sum(1 for c in cases if c["ok"])
    total = len(cases)
    ratio = passed / total if total else 0.0
    for item in cases:
        print(f"{'PASS' if item['ok'] else 'FAIL'}  {item['name']}"
              + (f"  -> {item['detail']}" if not item["ok"] and item["detail"] else ""))
    print(f"\n服装口径：{passed}/{total} 通过（{ratio:.1%}），"
          f"{len(snapshot['styles'])} 款 / {len(snapshot['skus'])} SKU")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"passed": passed, "total": total, "ratio": ratio, "cases": cases},
            ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ratio >= args.min_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
