"""Worker 节点：异步执行数据采集（ERP / 拍立淘 / RAG），并行 fan-out 后汇入主图。"""

from __future__ import annotations

import asyncio
import datetime as dt
import time

from app.config import department_of
from app.db import repos
from app.db.engine import session_scope
from app.mcp_client.client import get_mcp_hub
from app.rag.skill import build_rag_skill
from app.skills.inventory_judge import compute_feature_hints

_rag_skill = None


def _get_rag_skill():
    global _rag_skill
    if _rag_skill is None:
        _rag_skill = build_rag_skill()
    return _rag_skill


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def erp_worker(state: dict) -> dict:
    started = time.monotonic()
    hub = get_mcp_hub()
    sku_id = state["sku_id"]

    inventory = await hub.call("erp", "get_sku_inventory", {"sku_ids": [sku_id]})
    items = (inventory or {}).get("items") or []
    if not items:
        raise ValueError(f"ERP 未返回 SKU 数据: {sku_id}")
    sku = items[0]
    style_id = sku["style_id"]

    sales, in_transit, transfers, sibling, supplier, activities = await asyncio.gather(
        hub.call("erp", "get_sales_history", {"sku_id": sku_id, "days": 7}),
        hub.call("erp", "get_in_transit", {"sku_id": sku_id}),
        hub.call("erp", "get_warehouse_transfers", {"sku_id": sku_id}),
        hub.call("erp", "get_sibling_sizes", {"style_id": style_id}),
        hub.call("erp", "get_supplier_info", {"supplier_id": sku["supplier_id"]}),
        hub.call("erp", "get_platform_activities", {"platform": sku["platform"], "days": 45}),
    )

    sibling_items = (sibling or {}).get("items") or []
    features = compute_feature_hints(sku, sales.get("daily") or [])
    # 断码口径（复审 A1）：`AGENTS.md` 是「**本码**为主销码且可用库存为 0」才算断码
    # （长尾码缺货不算）。主销码来自中台 30 天尺码曲线（`main_size_flag`）；
    # 款级 `size_gap`（同款主销码断几个）只作为说明信息，不直接给本 SKU 打标签——
    # 否则"同款别的码断了"会把本 SKU（还有货）也判成断码。
    size_semantics = (sibling or {}).get("size_semantics")
    if size_semantics is None:
        size_semantics = any(s.get("main_size_flag") is not None for s in sibling_items)
    features["zero_sizes"] = sum(1 for s in sibling_items if int(s.get("stock") or 0) <= 0)
    features["size_semantics"] = "main_size" if size_semantics else "legacy"
    if size_semantics:
        own = next((s for s in sibling_items if str(s.get("sku_id") or "") == str(sku.get("sku_id") or "")), None)
        main_sizes = [s for s in sibling_items if int(s.get("main_size_flag") or 0) == 1]
        broken = [s for s in main_sizes if int(s.get("size_gap_flag") or 0) == 1]
        features["main_size_count"] = len(main_sizes)
        features["style_broken_size_count"] = len(broken)
        features["style_broken_sizes"] = [str(s.get("size") or "") for s in broken]
        features["self_broken_size"] = (
            bool(int(own.get("main_size_flag") or 0) == 1 and int(own.get("size_gap_flag") or 0) == 1)
            if own is not None else None
        )
    else:
        features["main_size_count"] = None
        features["style_broken_size_count"] = None
        features["style_broken_sizes"] = []
        features["self_broken_size"] = None
    if (sibling or {}).get("full_size_flag") is not None:
        features["full_size_flag"] = bool((sibling or {}).get("full_size_flag"))

    return {
        "data": {"erp": {
            "sku": sku,
            "features": features,
            "sales": sales,
            "in_transit": (in_transit or {}).get("items") or [],
            "transfers": (transfers or {}).get("items") or [],
            "sibling_sizes": sibling_items,
            "supplier": supplier,
            "activities": (activities or {}).get("items") or [],
        }},
        "trace": [{"node": "erp_worker", "status": "ok", "ms": _elapsed(started),
                   "detail": f"sales={len(sales.get('daily') or [])}d, "
                             f"in_transit={len((in_transit or {}).get('items') or [])}, "
                             f"transfers={len((transfers or {}).get('items') or [])}, "
                             f"siblings={len(sibling_items)}"}],
    }


async def market_worker(state: dict) -> dict:
    started = time.monotonic()
    if "market" not in (state.get("tasks") or ["market"]):
        return {"data": {"market": {"skipped": True}},
                "trace": [{"node": "market_worker", "status": "skipped", "ms": 0,
                           "detail": "Supervisor 未派发"}]}
    hub = get_mcp_hub()
    sku_id = state["sku_id"]

    with session_scope() as session:
        sku = repos.get_sku(session, sku_id)
        if sku is None:
            raise ValueError(f"SKU 不存在: {sku_id}")
        style_id = sku.style_id

    same_style, heat = await asyncio.gather(
        hub.call("pailitao", "search_same_style", {"style_id": style_id}),
        hub.call("pailitao", "get_style_heat", {"style_id": style_id}),
    )
    return {
        "data": {"market": {"same_style": same_style, "heat": heat}},
        "trace": [{"node": "market_worker", "status": "ok", "ms": _elapsed(started),
                   "detail": f"competitors={len(same_style.get('competitors') or [])}, "
                             f"found={same_style.get('found')}"}],
    }


async def rag_worker(state: dict) -> dict:
    started = time.monotonic()
    if "rag" not in (state.get("tasks") or ["rag"]):
        return {"data": {"rag": {"skipped": True, "hits": [], "citations": [], "context": ""}},
                "trace": [{"node": "rag_worker", "status": "skipped", "ms": 0,
                           "detail": "Supervisor 未派发"}]}
    with session_scope() as session:
        sku = repos.get_sku(session, state["sku_id"])
        dept = department_of(sku.category) if sku else None
        filters = {
            "department": [dept, "通用"] if dept else ["通用"],
            "category": sku.category if sku else "全部",
            "season": sku.season if sku else "全部",
            "platform": sku.platform if sku else "全部",
        }
    result = await _get_rag_skill().ainvoke({
        "query": state.get("rag_query") or f"{state['sku_id']} 库存管理 补货 断码 清仓 SOP",
        "filters": filters,
        "as_of": state.get("as_of") or dt.date.today().isoformat(),
        "topk": 5,
    })
    hits = result.get("hits") or []
    return {
        "data": {"rag": {
            "hits": hits,
            "context": result.get("context") or "",
            "citations": result.get("citations") or [],
            # 冲突裁决一起进 data：context 里已经有 `## 冲突提示`（reasoner 读的是它），
            # 但结构化结果也要留在 state 里，否则巡检 run_detail/审计就看不到"为什么取这条"。
            "conflicts": result.get("conflicts") or [],
            "conflict_check": result.get("conflict_check") or "",
            "conflict_diagnostics": result.get("conflict_diagnostics") or {},
            "scope_notice": result.get("scope_notice") or "",
        }},
        "trace": [{"node": "rag_worker", "status": "ok", "ms": _elapsed(started),
                   "detail": (f"hits={len(hits)} "
                              f"conflicts={len(result.get('conflicts') or [])}")}],
    }
