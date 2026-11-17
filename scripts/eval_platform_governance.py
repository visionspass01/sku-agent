"""中台治理评测：身份 / scope / 部门 / 配额 / 审计 / 并发（离线，可作 CI 门槛）。

为什么要有这个脚本：`tests/` 验证的是"每个实现细节对不对"，这里给的是**期望矩阵**——
换一个调用方、换一个 scope、换一个部门，期望的状态码与审计决策应该是什么。
治理下沉之后，"中台到底有没有挡住越权"这件事必须能一条命令跑出来。

用法：
    PYTHONPATH=src python scripts/eval_platform_governance.py --min-pass 1.0
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

FULL = {"X-Internal-Token": "k-sku"}
MENS = {"X-Internal-Token": "k-mens"}          # 只读 + 只限男装
OPS = {"X-Internal-Token": "k-ops"}            # 读 + etl
TINY = {"X-Internal-Token": "k-tiny"}          # 配额 2/分钟
NO_DEPT = {"X-Internal-Token": "k-mens-op"}    # 有写/审批权限，但只限男装

MEN_SKU = "ST-05-C1-M"
WOMEN_SKU = "ST-01-C1-M"

KEYS = {
    "sku-agent": {"key": "k-sku", "scopes": ["read", "write", "approve"], "departments": None},
    "analysis-agent": {"key": "k-mens", "scopes": ["read"], "departments": ["男装"]},
    "mens-operator": {"key": "k-mens-op", "scopes": ["read", "write", "approve"],
                      "departments": ["男装"]},
    "ops-agent": {"key": "k-ops", "scopes": ["read", "etl"], "departments": None},
    "tiny-agent": {"key": "k-tiny", "scopes": ["read"], "departments": None, "rate_limit": 2},
}


def _setup(tmp: Path) -> None:
    """临时库 + 自举数据源（不依赖 Agent 库），保证脚本可在任何机器上跑。"""
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


def _matrix(client) -> list[dict]:  # noqa: ANN001
    cases: list[dict] = []

    def case(name: str, ok: bool, detail: str = "") -> None:
        cases.append({"case": name, "ok": bool(ok), "detail": detail})

    # ---------------- 身份
    case("无令牌 → 401", client.get(f"/api/v1/skus/{MEN_SKU}/metrics").status_code == 401)
    case("未知令牌 → 401", client.get(f"/api/v1/skus/{MEN_SKU}/metrics",
                                  headers={"X-Internal-Token": "nope"}).status_code == 401)
    case("health 公开可探活", client.get("/api/v1/health").status_code == 200)
    case("legacy 令牌全权", client.get(f"/api/v1/skus/{WOMEN_SKU}/metrics",
                                  headers={"X-Internal-Token": "demo-internal-token"}).status_code == 200)

    # ---------------- scope
    case("read 可读", client.get(f"/api/v1/skus/{MEN_SKU}/metrics", headers=MENS).status_code == 200)
    case("read 不能写", client.post("/api/v1/erp/replenish-orders", headers=MENS,
                                 json={"sku_id": MEN_SKU, "qty": 1}).status_code == 403)
    case("read 不能审批", client.post("/api/v1/approvals/x/approve", headers=MENS,
                                  json={}).status_code == 403)
    case("无 etl 不能看审计", client.get("/api/v1/audit", headers=MENS).status_code == 403)
    case("有 etl 能看审计", client.get("/api/v1/audit", headers=OPS).status_code == 200)

    # ---------------- 部门
    case("同部门可读", client.get(f"/api/v1/skus/{MEN_SKU}/inventory", headers=MENS).status_code == 200)
    case("跨部门拒绝", client.get(f"/api/v1/skus/{WOMEN_SKU}/inventory", headers=MENS).status_code == 403)
    case("未知 SKU 是 404", client.get("/api/v1/skus/ST-99-C9-X/metrics", headers=MENS).status_code == 404)
    approvals = client.get("/api/v1/approvals?status=all", headers=MENS).json()
    case("审批列表按部门过滤", approvals["scope_filtered"] is True
         and all(i["department"] == "男装" for i in approvals["items"]),
         f"dropped={approvals['scope_dropped']}")
    case("跨部门动销率拒绝",
         client.get("/api/v1/metrics/sales-rate?department=女装", headers=MENS).status_code == 403)
    case("无参动销率按可见范围聚合",
         client.get("/api/v1/metrics/sales-rate", headers=MENS).json()["effective_departments"] == ["男装"])
    case("跨部门写入拒绝", client.post("/api/v1/erp/replenish-orders", headers=NO_DEPT,
                                  json={"sku_id": WOMEN_SKU, "qty": 10}).status_code == 403)

    # ---------------- 配额（注意：/health 是公开路径，不占配额）
    first = client.get(f"/api/v1/skus/{MEN_SKU}/metrics", headers=TINY)
    second = client.get(f"/api/v1/skus/{MEN_SKU}/metrics", headers=TINY)
    third = client.get(f"/api/v1/skus/{MEN_SKU}/metrics", headers=TINY)
    case("配额内正常", first.status_code == 200 and second.status_code == 200,
         f"{first.status_code}/{second.status_code}")
    case("超配额 429 + Retry-After",
         third.status_code == 429 and int(third.headers.get("Retry-After", "0")) >= 1,
         f"status={third.status_code}")

    # ---------------- 并发：审批条件更新
    approval_id = client.post("/api/v1/erp/replenish-orders", headers=FULL,
                              json={"sku_id": MEN_SKU, "qty": 500, "reason": "评测并发"}).json()["approval_id"]
    codes: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def approve() -> None:
        barrier.wait(timeout=5)
        resp = client.post(f"/api/v1/approvals/{approval_id}/approve", headers=FULL,
                           json={"approver": "tester"})
        with lock:
            codes.append(resp.status_code)

    threads = [threading.Thread(target=approve) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    case("并发审批只有一个成功", sorted(codes) == [200, 409], f"codes={sorted(codes)}")

    # ---------------- 并发：幂等键
    key = "eval-idem-001"
    results: list[dict] = []
    lock2 = threading.Lock()
    barrier2 = threading.Barrier(2)

    def create() -> None:
        barrier2.wait(timeout=5)
        resp = client.post("/api/v1/erp/replenish-orders", headers=FULL,
                           json={"sku_id": MEN_SKU, "qty": 500, "reason": "评测幂等",
                                 "idempotency_key": key})
        with lock2:
            results.append({"status": resp.status_code, "body": resp.json()})

    threads = [threading.Thread(target=create) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    case("并发同幂等键不 500", all(r["status"] == 200 for r in results), f"{results}")
    case("并发同幂等键只建一单",
         len({r["body"]["approval_id"] for r in results}) == 1 if results else False)

    # ---------------- 审计
    resp = client.get(f"/api/v1/skus/{MEN_SKU}/inventory", headers=MENS)
    case("响应回显 X-Agent-Id", resp.headers.get("X-Agent-Id") == "analysis-agent")
    case("响应回显 X-Request-Id", bool(resp.headers.get("X-Request-Id")))
    rows = client.get("/api/v1/audit?limit=200", headers=OPS).json()
    decisions = rows["by_decision"]
    case("审计记录 allow", decisions.get("allow", 0) > 0)
    case("审计记录 unauthenticated", decisions.get("unauthenticated", 0) > 0)
    case("审计记录 denied_department", decisions.get("denied_department", 0) > 0)
    case("审计记录 denied_scope", decisions.get("denied_scope", 0) > 0)
    case("审计记录 rate_limited", decisions.get("rate_limited", 0) > 0)
    case("审计记录 conflict", decisions.get("conflict", 0) >= 1)
    case("审计可按 agent 过滤",
         all(r["agent_id"] == "analysis-agent"
             for r in client.get("/api/v1/audit?agent_id=analysis-agent", headers=OPS).json()["items"]))
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description="中台治理期望矩阵评测")
    parser.add_argument("--min-pass", type=float, default=1.0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="eval_platform_"))
    _setup(tmp)
    from fastapi.testclient import TestClient

    from data_platform.api.main import app

    with TestClient(app) as client:
        cases = _matrix(client)

    passed = [c for c in cases if c["ok"]]
    failed = [c for c in cases if not c["ok"]]
    for case in cases if args.verbose else failed:
        mark = "✓" if case["ok"] else "✗"
        print(f"  {mark} {case['case']} {case['detail']}".rstrip())

    rate = len(passed) / len(cases) if cases else 0.0
    print(f"\n中台治理通过率 = {rate:.1%}（{len(passed)}/{len(cases)}）")
    if rate < args.min_pass:
        print(f"✗ 低于门槛 {args.min_pass:.0%}")
        return 1
    print(f"✓ 达标（门槛 {args.min_pass:.0%}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
