"""服装口径接入对话主链路（评审 2.1-5 / 3.5）。

三件事必须成立，否则「款级指标」只是躺在中台接口里：
1. 四个款级/尺码工具真的装配给了 Agent（能力目录 → 工具集 → manifest 分组）；
2. 部门作用域对**款**也生效（不能因为入参不是 sku_id 就漏校验）；
3. 口径文档进得了知识库（SOP 能被 RAG 检索到，而不是只有代码里有公式）。
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent

MEN_STYLE = "ST-92"      # 男装（conftest 种子数据）
WOMEN_STYLE = "ST-90"    # 女装（含断码 SKU）


def _user(username: str, department: str | None, role: str = "user"):
    from app.auth import AuthUser

    return AuthUser(username=username, role=role, department=department)


def _tools(user) -> dict:  # noqa: ANN001
    from app.tools.platform_tools import build_platform_tools

    return {tool.name: tool for tool in build_platform_tools(user)}


def test_apparel_tools_are_assembled_and_declared():
    from app.tools.platform_tools import ALL_TOOL_NAMES, TOOL_GROUPS, TOOL_SCOPES

    expected = {"get_style_metrics", "get_size_curve", "get_size_gap", "get_apparel_overview"}
    assert expected <= ALL_TOOL_NAMES
    assert set(TOOL_GROUPS.get("style") or []) == expected
    assert TOOL_SCOPES["get_style_metrics"] == "style"
    assert TOOL_SCOPES["get_size_curve"] == "style"
    # 部门维度工具：入参只能取本部门（与动销率一致）
    assert TOOL_SCOPES["get_size_gap"] == "department"
    assert TOOL_SCOPES["get_apparel_overview"] == "department"


def test_capability_snapshot_contains_style_group():
    snapshot = json.loads(
        (ROOT / "src/app/tools/capabilities.snapshot.json").read_text(encoding="utf-8"))
    assert "style" in snapshot["groups"]
    ids = {item["id"] for item in snapshot["capabilities"]}
    assert {"style.metrics", "style.size_curve", "style.size_gap",
            "style.apparel_overview"} <= ids
    # 每个能力都必须声明作用域种类（漏声明会在装配期失败）
    assert all(item.get("scope_kind") for item in snapshot["capabilities"])


def test_manifests_grant_style_group():
    for name in ("diagnostician.json", "operator.json"):
        raw = json.loads((ROOT / f"src/app/agents/registry/{name}").read_text(encoding="utf-8"))
        assert "style" in raw["tools"], f"{name} 没有 style 分组：断码问题会调不到款级工具"


def test_style_tools_call_platform_endpoints(seeded):
    """四个工具打的是中台的款级端点（fake 中台会按同一份演示库应答）。"""
    admin = _user("admin", None, role="admin")
    tools = _tools(admin)

    metrics = asyncio.run(tools["get_style_metrics"].ainvoke({"style_id": MEN_STYLE}))
    assert metrics["style_id"] == MEN_STYLE
    assert metrics["size_curve"] and metrics["size_curve"][0]["size"]
    assert "size_gap" in metrics

    curve = asyncio.run(tools["get_size_curve"].ainvoke({"style_id": MEN_STYLE}))
    assert curve["style_id"] == MEN_STYLE

    gap = asyncio.run(tools["get_size_gap"].ainvoke({}))
    assert "items" in gap and "full_size_rate" in gap

    overview = asyncio.run(tools["get_apparel_overview"].ainvoke({}))
    assert "full_size_rate" in overview and "broken_size_rate" in overview


def test_style_metrics_deny_cross_department(seeded):
    """男装账号读女装款 → business 错误（款级作用域不能漏）。"""
    worker1 = _user("worker1", "男装")
    result = asyncio.run(_tools(worker1)["get_style_metrics"].ainvoke({"style_id": WOMEN_STYLE}))
    assert "无权限" in result["error"]
    assert result.get("error_kind") == "business"

    ok = asyncio.run(_tools(worker1)["get_style_metrics"].ainvoke({"style_id": MEN_STYLE}))
    assert "error" not in ok


def test_size_gap_department_is_forced(seeded, monkeypatch):
    """部门账号查断码清单时，department 入参被强制为本部门（与 get_sales_rate 一致）。"""
    from app.tools import platform_tools

    seen: list[dict] = []
    real_client = platform_tools.get_platform_client()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path,
                     "department": request.url.params.get("department")})
        if request.url.path.endswith("/metrics/size-gap"):
            return httpx.Response(200, json={"items": [], "count": 0, "full_size_rate": 0.5})
        return httpx.Response(404, json={"detail": "not found"})

    platform_tools.set_platform_client(platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler)))
    try:
        worker1 = _user("worker1", "男装")
        data = asyncio.run(_tools(worker1)["get_size_gap"].ainvoke({"department": "女装"}))
    finally:
        platform_tools.set_platform_client(real_client)

    assert seen and seen[-1]["department"] == "男装"
    assert data.get("department_forced") is True


def test_apparel_metric_definitions_are_governed(seeded):
    """口径公式必须由中台治理下发（Agent 提示词里的口径来自这里）。"""
    from data_platform.governance import APPAREL_NOTES, METRIC_DEFINITIONS

    keys = {item["key"] for item in METRIC_DEFINITIONS}
    assert {"size_curve_share", "main_size_flag", "full_size_rate", "broken_size_rate",
            "size_gap_count", "turnover_days", "sell_through_rate", "lifecycle_stage",
            "competitor_price_band"} <= keys
    by_key = {item["key"]: item for item in METRIC_DEFINITIONS}
    assert "≥15%" in by_key["size_curve_share"]["formula"]
    assert "可用库存 ÷ 日均销量" in by_key["turnover_days"]["formula"]
    assert "近 30 天销量 ÷ (近 30 天销量 + 当前可用库存)" in by_key["sell_through_rate"]["formula"]
    assert APPAREL_NOTES and any("主销码" in note for note in APPAREL_NOTES)


def test_apparel_sop_docs_are_indexable():
    """新增 SOP 必须能被 loader 解析（front matter 合法、doc_id 唯一、正文非空）。"""
    from app.config import KB_DIR
    from app.rag.loader import load_documents

    docs = {doc.doc_id: doc for doc in load_documents(KB_DIR)}
    for doc_id in ("sop-sizecurve", "sop-lifecycle", "sop-sizegap"):
        assert doc_id in docs, f"知识库缺少 {doc_id}"
        assert docs[doc_id].body.strip(), f"{doc_id} 正文为空"
        assert docs[doc_id].version
    assert docs["sop-sizecurve"].department == "通用"
    # 断码 SOP 必须写清「主销码」口径，否则检索命中后也答不对
    assert "主销码" in docs["sop-sizegap"].body


def test_agent_baseline_documents_apparel_metrics():
    """AGENTS.md（L2 常驻提示）必须带服装口径，否则每轮都可能按款平均。"""
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "主销码" in text and "断码" in text
    assert "lifecycle_stage" in text or "生命周期" in text
    assert "齐码" in text
