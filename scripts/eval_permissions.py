"""权限作用域评测：22 个工具 × 3 种账号，逐条验证「该拒绝的拒绝、该过滤的过滤」。

为什么单测之外还要评测：单测覆盖的是「某个工具某条分支」，而这里给的是**期望矩阵**——
新增工具忘了声明作用域、或某类入参（无 sku_id）漏校验，都会在这里变成红。
离线运行（MockTransport + 临时 SQLite），可作 CI 门槛。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\eval_permissions.py --min-pass 1.0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app.config import Settings, set_settings  # noqa: E402
from app.db.engine import init_schema, make_engine, session_scope, set_engine  # noqa: E402
from app.db.models import MockOrder, Sku  # noqa: E402

OWN = "ST-92-C1-M"      # 男装
FOREIGN = "ST-90-C1-M"  # 女装


def _bootstrap(tmp: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{(tmp / 'perm.db').as_posix()}",
        analyses_dir=str(tmp / "analyses"),
        analysis_merge_mode="keep-both",
    )
    set_settings(settings)
    set_engine(make_engine(settings))
    init_schema()
    import datetime as dt

    with session_scope() as session:
        for sku_id, category in ((OWN, "男装"), (FOREIGN, "女装")):
            session.add(Sku(sku_id=sku_id, style_id=sku_id[:5], style_name="评测款",
                            category=category, season="秋冬", color="黑", size="M",
                            warehouse_id="WH-HZ", platform="淘系", price=299, list_price=299,
                            stock=100, return_rate=0.05, supplier_id="SUP-1", status="在售",
                            list_date=dt.date(2026, 10, 1)))
        session.add(MockOrder(order_id="ORD-MEN", sku_id=OWN, qty=1, amount=299.0,
                             status="已发货", channel="淘系",
                             created_at=dt.datetime(2026, 11, 1, 9)))
        session.add(MockOrder(order_id="ORD-WOMEN", sku_id=FOREIGN, qty=1, amount=299.0,
                             status="已发货", channel="淘系",
                             created_at=dt.datetime(2026, 11, 1, 9)))


def _transport() -> httpx.MockTransport:
    """中台桩：故意返回**两个部门**的数据，用来验证过滤是否真的发生。"""
    pending = {"items": [
        {"approval_id": "appr-men", "sku_id": OWN, "qty": 300, "amount": 30000,
         "status": "pending_confirmation"},
        {"approval_id": "appr-women", "sku_id": FOREIGN, "qty": 400, "amount": 40000,
         "status": "pending_confirmation"},
    ]}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/approvals"):
            return httpx.Response(200, json=pending)
        if path.endswith("/sales-rate"):
            return httpx.Response(200, json={"department": request.url.params.get("department"),
                                             "sales_rate": 0.4})
        if path.endswith("/inventory"):
            return httpx.Response(200, json={"sku_id": OWN, "available": 500})
        if path.startswith("/api/v1/skus/"):
            return httpx.Response(200, json={"sku_id": path.split("/")[4], "available": 500})
        if path.endswith("/metadata/metrics"):
            return httpx.Response(200, json={"data_version": "v1", "prompt_context": "口径"})
        if "sales-rate" in path:
            return httpx.Response(200, json={"sales_rate": 0.4})
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


def _user(department: str | None):
    from app.auth import AuthUser

    role = "admin" if department is None else "user"
    return AuthUser(username="admin" if department is None else f"worker-{department}",
                    role=role, department=department)


async def _call(tools: dict, name: str, args: dict) -> dict:
    tool = tools.get(name)
    if tool is None:
        return {"error": "工具不可用", "error_kind": "business"}
    return await tool.ainvoke(args)


def _denied(result: dict) -> bool:
    text = str(result.get("error") or "")
    return bool(text) and ("无权限" in text or "不存在" in text)


async def _checks() -> list[dict]:
    """返回 [{case, ok, detail}]。每一条都是「期望行为」而不是实现细节。"""
    from app.security.scope import SCOPE_KINDS
    from app.tools import platform_tools
    from app.tools.platform_tools import (
        ALL_TOOL_NAMES,
        TOOL_SCOPES,
        WRITE_TOOL_NAMES,
        build_platform_tools,
    )

    platform_tools.set_platform_client(platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=_transport(),
    ))
    cases: list[dict] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        cases.append({"case": name, "ok": ok, "detail": detail})

    record("声明表覆盖全部工具", set(TOOL_SCOPES) == ALL_TOOL_NAMES,
           f"missing={sorted(ALL_TOOL_NAMES - set(TOOL_SCOPES))}")
    record("作用域取值合法", set(TOOL_SCOPES.values()) <= SCOPE_KINDS,
           f"bad={sorted(set(TOOL_SCOPES.values()) - SCOPE_KINDS)}")

    worker = build_platform_tools(_user("男装"))
    admin = build_platform_tools(_user(None))
    wt = {t.name: t for t in worker}
    at = {t.name: t for t in admin}

    # 1) sku 作用域：跨部门必须拒绝，本部门必须放行（写工具按「读/写分离」另有约定）
    for name in sorted(n for n, kind in TOOL_SCOPES.items() if kind == "sku"):
        args = {"sku_id": FOREIGN}
        if name == "create_replenishment_order":
            args |= {"qty": 10, "reason": "评测"}
        denied = _denied(await _call(wt, name, args))
        record(f"sku 越权拒绝 · {name}", denied)
        own_args = dict(args, sku_id=OWN)
        if name in WRITE_TOOL_NAMES:
            # 写工具默认只给 admin：部门账号拿到的应是「无权限执行写操作」
            text = str((await _call(wt, name, own_args)).get("error") or "")
            record(f"写工具默认拒绝部门账号 · {name}", "写操作" in text, text[:40])
            record(f"写工具管理员放行 · {name}", not _denied(await _call(at, name, own_args)))
            continue
        allowed = not _denied(await _call(wt, name, own_args))
        record(f"sku 本部门放行 · {name}", allowed)

    # 2) order 作用域：订单反查 SKU 再判部门
    for name in sorted(n for n, kind in TOOL_SCOPES.items() if kind == "order"):
        args = {"order_id": "ORD-WOMEN"}
        if name == "create_after_sales":
            args |= {"reason": "评测"}
        record(f"order 越权拒绝 · {name}", _denied(await _call(wt, name, args)))

    # 3) sku_list：跨部门条目必须被过滤掉，且不静默（有 scope_dropped 标记）
    pending = await _call(wt, "list_pending_approvals", {})
    ids = [i.get("approval_id") for i in pending.get("items") or []]
    record("sku_list 过滤跨部门条目", ids == ["appr-men"], f"ids={ids}")
    record("sku_list 标注过滤数量",
           pending.get("scope_filtered") is True and pending.get("scope_dropped") == 1,
           f"filtered={pending.get('scope_filtered')} dropped={pending.get('scope_dropped')}")
    admin_pending = await _call(at, "list_pending_approvals", {})
    record("sku_list 管理员不过滤",
           len(admin_pending.get("items") or []) == 2
           and admin_pending.get("scope_filtered") is False)

    # 4) department：模型填别的部门也不生效
    rate = await _call(wt, "get_sales_rate", {"department": "女装"})
    record("department 强制本部门",
           rate.get("department_forced") is True and "男装" in str(rate.get("note") or ""),
           json.dumps({k: rate.get(k) for k in ("department", "department_forced")},
                      ensure_ascii=False))
    admin_rate = await _call(at, "get_sales_rate", {"department": "女装"})
    record("department 管理员透传", not admin_rate.get("department_forced"))

    # 5) global/local：不涉及业务数据，不该被误拒
    #    注：`org` 类档案工具（save/read/search_analyses）的**行级可见性**不在这张矩阵里
    #    （它检查的是"能不能调"，不是"调回来的是哪个部门的行"），由
    #    `scripts/eval_agent_tooling.py` 第 7 组「档案可见性」覆盖（复审 F1/F2）。
    for name in ("get_metric_definitions",):
        record(f"global 放行 · {name}", not _denied(await _call(wt, name, {})))
    for name, args in (("list_skills", {}), ("plan_query", {"question": "查库存"})):
        record(f"local 放行 · {name}", not _denied(await _call(wt, name, args)))

    platform_tools.set_platform_client(None)
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description="工具部门作用域评测")
    parser.add_argument("--min-pass", type=float, default=1.0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="eval_perm_"))
    _bootstrap(tmp)
    cases = asyncio.run(_checks())

    passed = [c for c in cases if c["ok"]]
    failed = [c for c in cases if not c["ok"]]
    for case in cases if args.verbose else failed:
        mark = "✓" if case["ok"] else "✗"
        print(f"  {mark} {case['case']} {case['detail']}".rstrip())

    rate = len(passed) / len(cases) if cases else 0.0
    print(f"\n权限作用域通过率 = {rate:.1%}（{len(passed)}/{len(cases)}）")
    if rate < args.min_pass:
        print(f"✗ 低于门槛 {args.min_pass:.0%}")
        return 1
    print(f"✓ 达标（门槛 {args.min_pass:.0%}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
