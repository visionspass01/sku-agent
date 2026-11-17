"""Worker 口径单测（复审 F4）：断码「本码 / 主销码」判定与库存口径的装配层。

为什么单独测：`erp_worker` 里 `self_broken_size`、`size_semantics` 回落、`style_broken_size_count`
都是口径关键路径，但图级用例的测试桩把 `main_size_flag` 恒设为 1、`size_gap_flag` 按库存 0 判，
所以"改回款级断码""own 行匹配写错"这两类回归不会被现有用例抓到。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents import workers


class _StubHub:
    """按工具名返回固定 payload 的 MCP Hub 桩（不连任何服务）。"""

    def __init__(self, sibling: dict) -> None:
        self.sibling = sibling
        self.calls: list[tuple[str, str]] = []

    async def call(self, server: str, tool: str, arguments: dict | None = None):
        self.calls.append((server, tool))
        if tool == "get_sku_inventory":
            return {"items": [{
                "sku_id": "ST-90-C1-M", "style_id": "ST-90", "style_name": "测试款",
                "category": "女装", "season": "秋冬", "color": "黑色", "size": "M",
                "warehouse_id": "WH-HZ", "platform": "淘系", "price": 299,
                "stock": 100, "available": 40, "return_rate": 0.06,
                "supplier_id": "SUP-02",
            }]}
        if tool == "get_sales_history":
            return {"daily": [{"qty": 2} for _ in range(7)]}
        if tool == "get_in_transit":
            return {"items": []}
        if tool == "get_warehouse_transfers":
            return {"items": []}
        if tool == "get_sibling_sizes":
            return self.sibling
        if tool == "get_supplier_info":
            return {"lead_time_days": 10, "moq": 100}
        if tool == "get_platform_activities":
            return {"items": []}
        return {}


def _run_worker(monkeypatch, sibling: dict) -> dict:
    hub = _StubHub(sibling)
    monkeypatch.setattr(workers, "get_mcp_hub", lambda: hub)
    out = asyncio.run(workers.erp_worker({"sku_id": "ST-90-C1-M", "run_id": "r1", "as_of": "2026-11-16"}))
    return out["data"]["erp"]


def _rows(**over) -> list[dict]:
    base = [
        {"sku_id": "ST-90-C1-S", "size": "S", "stock": 0, "available": 0,
         "main_size_flag": 1, "size_gap_flag": 1, "size_share": 0.3},
        {"sku_id": "ST-90-C1-M", "size": "M", "stock": 100, "available": 40,
         "main_size_flag": 1, "size_gap_flag": 0, "size_share": 0.4},
        {"sku_id": "ST-90-C1-L", "size": "L", "stock": 30, "available": 30,
         "main_size_flag": 1, "size_gap_flag": 0, "size_share": 0.3},
    ]
    if over:
        base[1] = {**base[1], **over}
    return base


def test_worker_marks_self_broken_only_for_the_own_size(monkeypatch):
    """同款别的码断了（S 码），本码（M）有货 → 本 SKU 不算断码，只记款级断码数。"""
    features = _run_worker(monkeypatch, {"items": _rows(), "size_semantics": True,
                                         "full_size_flag": False})["features"]
    assert features["size_semantics"] == "main_size"
    assert features["self_broken_size"] is False
    assert features["style_broken_size_count"] == 1
    assert features["style_broken_sizes"] == ["S"]
    assert features["main_size_count"] == 3


def test_worker_flags_self_broken_when_own_main_size_is_empty(monkeypatch):
    """断码压货：总库存 100，但本码是主销码且可用为 0 → 本 SKU 断码。"""
    rows = _rows(available=0, size_gap_flag=1)
    # 让 sibling 里本码这一行也体现可用为 0
    rows[1] = {**rows[1], "stock": 0}
    features = _run_worker(monkeypatch, {"items": rows, "size_semantics": True,
                                         "full_size_flag": False})["features"]
    assert features["self_broken_size"] is True
    assert features["style_broken_size_count"] == 2


def test_worker_legacy_rows_fall_back_and_say_so(monkeypatch):
    """老数据没有主销码标记（也没有 size_semantics 字段）→ 明确降级为 legacy。"""
    legacy_rows = [
        {"sku_id": "ST-90-C1-S", "size": "S", "stock": 0},
        {"sku_id": "ST-90-C1-M", "size": "M", "stock": 0},
    ]
    features = _run_worker(monkeypatch, {"items": legacy_rows})["features"]
    assert features["size_semantics"] == "legacy"
    assert features["self_broken_size"] is None
    assert features["style_broken_size_count"] is None
    assert features["zero_sizes"] == 2


def test_worker_days_of_supply_uses_available(monkeypatch):
    """可用 40 / 现存 100、日均 2 → 可售 20 天（按可用算，不是 50）。"""
    features = _run_worker(monkeypatch, {"items": _rows(), "size_semantics": True})["features"]
    assert features["base_daily"] == 2.0
    assert features["days_of_supply"] == 20.0
    assert features["stock_source"] == "available"


def test_worker_marks_on_hand_fallback_when_available_missing(monkeypatch):
    """上游没给 available（保留 None）时回落现存，并留下 stock_source=on_hand 供下游标注。"""
    hub = _StubHub({"items": _rows(), "size_semantics": True})

    async def _call(server, tool, arguments=None):  # noqa: ANN001
        data = await _StubHub.call(hub, server, tool, arguments)
        if tool == "get_sku_inventory":
            item = dict(data["items"][0])
            item.pop("available", None)      # 模拟老中台：没有可用库存字段
            return {"items": [item]}
        return data

    hub.call = _call  # type: ignore[assignment]
    monkeypatch.setattr(workers, "get_mcp_hub", lambda: hub)
    out = asyncio.run(workers.erp_worker({"sku_id": "ST-90-C1-M", "run_id": "r1",
                                          "as_of": "2026-11-16"}))
    features = out["data"]["erp"]["features"]
    assert features["stock_source"] == "on_hand"
    assert features["days_of_supply"] == 50.0     # 100/2，按现存兜底


def test_worker_requires_erp_inventory(monkeypatch):
    hub = _StubHub({})
    monkeypatch.setattr(workers, "get_mcp_hub", lambda: hub)

    async def _empty(server, tool, arguments=None):  # noqa: ANN001
        return {"items": []} if tool == "get_sku_inventory" else {}

    hub.call = _empty  # type: ignore[assignment]
    with pytest.raises(ValueError):
        asyncio.run(workers.erp_worker({"sku_id": "ST-90-C1-M"}))
