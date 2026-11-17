"""能力目录（Capability Catalog）：中台与 Agent 之间「有哪些能力」的**唯一真相**。

为什么要有它（评审 C2 / 2.1-2 / 2.1-3）：
Agent 侧原先手写一张 `TOOL_GROUPS`（22 个工具、8 个分组），中台完全不知道这套东西存在。
加一个能力要么两边都改、要么只改一边——只改一边的表现是「工具在、但中台没有对应接口」，
或者「中台有接口、Agent 不知道怎么用」，两边靠人肉对轴。

现在：
- 中台用本模块声明能力（id / 分组 / 工具名 / 需要的 scope / 部门作用域种类 / 落在哪些端点 /
  传输方式），通过 `GET /api/v1/metadata/capabilities` 下发；
- Agent 用**提交到仓库的快照**（`src/app/tools/capabilities.snapshot.json`）离线启动，
  启动时与中台对一次版本，不一致就在 `/api/health.capabilities` 里显式标注；
- `tests/test_capabilities_sync.py` 保证「快照 == 本模块」，改漏一边即失败。

`transport` 取值：
- `rest`  ：Agent 直接用 PlatformClient 调中台 REST；
- `mcp`   ：经 MCP server 适配（当前 order 域仍在迁移中；迁移完成后会改成 rest）；
- `local` ：不碰业务数据，留在 Agent 侧（技能/计划/组织记忆）。

`scope_kind` 与 Agent 侧 `TOOL_SCOPES` 的取值一一对应（见 `app/security/scope.py`）：
`sku` / `order` / `sku_list` / `department` / `global` / `local` / `org` / `style`。
"""

from __future__ import annotations

from data_platform.contract import CONTRACT_VERSION

# 预留分组：声明了但当前没有任何工具 → Agent 侧必须**报错**而不是静默放行全部工具
# （早先 `TOOL_GROUPS["image"] = []` 会让 `tools: ["image"]` 的 Agent 拿到全部 22 个工具）
RESERVED_GROUPS: dict[str, str] = {
    "image": "生图 Agent 的工具组（尚未实现；声明该分组会立即失败，而不是静默降级）",
}

# 能力明细。字段顺序即对外契约，改动请先读 data_platform/contract.py 的版本策略。
CAPABILITIES: list[dict] = [
    # ---- 库存与销量（read）----
    {"id": "sku.inventory", "group": "read", "tool": "get_inventory",
     "name": "可用库存", "scopes": ["read"], "scope_kind": "sku", "transport": "rest",
     "endpoints": ["GET /api/v1/skus/{sku_id}/inventory"]},
    {"id": "sku.sell_through", "group": "read", "tool": "get_sell_through",
     "name": "预计售罄天数", "scopes": ["read"], "scope_kind": "sku", "transport": "rest",
     "endpoints": ["GET /api/v1/skus/{sku_id}/sell-through"]},
    {"id": "sku.metrics", "group": "read", "tool": "get_sku_metrics",
     "name": "SKU 治理指标", "scopes": ["read"], "scope_kind": "sku", "transport": "rest",
     "endpoints": ["GET /api/v1/skus/{sku_id}/metrics"]},
    {"id": "sku.in_transit", "group": "read", "tool": "get_in_transit",
     "name": "在途采购", "scopes": ["read"], "scope_kind": "sku", "transport": "rest",
     "endpoints": ["GET /api/v1/skus/{sku_id}/in-transit"]},
    {"id": "sku.reservations", "group": "read", "tool": "get_reservations",
     "name": "CRM 预订", "scopes": ["read"], "scope_kind": "sku", "transport": "rest",
     "endpoints": ["GET /api/v1/skus/{sku_id}/reservations"]},
    {"id": "sku.orders", "group": "read", "tool": "get_orders",
     "name": "SKU 近 N 天订单", "scopes": ["read"], "scope_kind": "sku", "transport": "rest",
     "endpoints": ["GET /api/v1/skus/{sku_id}/orders"]},
    {"id": "sku.sales", "group": "read", "tool": "get_sales",
     "name": "SKU 销量", "scopes": ["read"], "scope_kind": "sku", "transport": "rest",
     "endpoints": ["GET /api/v1/skus/{sku_id}/sales"]},
    {"id": "meta.metric_definitions", "group": "read", "tool": "get_metric_definitions",
     "name": "指标口径元数据", "scopes": ["read"], "scope_kind": "global", "transport": "rest",
     "endpoints": ["GET /api/v1/metadata/metrics"]},
    {"id": "metric.sales_rate", "group": "read", "tool": "get_sales_rate",
     "name": "动销率", "scopes": ["read"], "scope_kind": "department", "transport": "rest",
     "endpoints": ["GET /api/v1/metrics/sales-rate"]},
    {"id": "sku.transfer_suggestion", "group": "read", "tool": "create_transfer_suggestion",
     "name": "跨仓调拨建议", "scopes": ["read"], "scope_kind": "sku", "transport": "rest",
     "endpoints": ["POST /api/v1/transfer-suggestions"]},

    # ---- 服装行业口径：款级 / 尺码结构（SKU 级指标答不了"断码/齐码/追单"）----
    {"id": "style.metrics", "group": "style", "tool": "get_style_metrics",
     "name": "款级指标（含尺码曲线/生命周期）", "scopes": ["read"], "scope_kind": "style",
     "transport": "rest",
     "endpoints": ["GET /api/v1/styles/{style_id}/metrics"]},
    {"id": "style.size_curve", "group": "style", "tool": "get_size_curve",
     "name": "尺码曲线", "scopes": ["read"], "scope_kind": "style", "transport": "rest",
     "endpoints": ["GET /api/v1/metrics/size-curve"]},
    {"id": "style.size_gap", "group": "style", "tool": "get_size_gap",
     "name": "断码款清单", "scopes": ["read"], "scope_kind": "department", "transport": "rest",
     "endpoints": ["GET /api/v1/metrics/size-gap"]},
    {"id": "style.apparel_overview", "group": "style", "tool": "get_apparel_overview",
     "name": "服装经营概览（齐码率/断码率/周转/售罄）", "scopes": ["read"],
     "scope_kind": "department", "transport": "rest",
     "endpoints": ["GET /api/v1/metrics/apparel-overview"]},

    # ---- 写动作 ----
    {"id": "erp.replenish_order", "group": "write", "tool": "create_replenishment_order",
     "name": "创建补货草稿单", "scopes": ["write"], "scope_kind": "sku", "transport": "rest",
     "endpoints": ["POST /api/v1/erp/replenish-orders"]},
    {"id": "order.after_sales", "group": "write", "tool": "create_after_sales",
     "name": "发起售后工单草稿", "scopes": ["write"], "scope_kind": "order", "transport": "mcp",
     "endpoints": ["POST /api/v1/after-sales"]},

    # ---- 审批（只读：查列表不改状态）----
    {"id": "approval.list", "group": "approval", "tool": "list_pending_approvals",
     "name": "待审批列表", "scopes": ["read"], "scope_kind": "sku_list", "transport": "rest",
     "endpoints": ["GET /api/v1/approvals"]},

    # ---- 订单 / 物流（MCP 适配中）----
    {"id": "order.status", "group": "order", "tool": "get_order_status",
     "name": "订单状态", "scopes": ["read"], "scope_kind": "order", "transport": "mcp",
     "endpoints": ["GET /api/v1/orders/{order_id}"]},
    {"id": "order.list", "group": "order", "tool": "list_orders",
     "name": "SKU 订单列表", "scopes": ["read"], "scope_kind": "sku", "transport": "mcp",
     "endpoints": ["GET /api/v1/skus/{sku_id}/orders"]},
    {"id": "order.logistics", "group": "order", "tool": "get_logistics",
     "name": "物流轨迹", "scopes": ["read"], "scope_kind": "order", "transport": "mcp",
     "endpoints": ["GET /api/v1/orders/{order_id}/logistics"]},

    # ---- Agent 本地能力（不碰业务数据）----
    {"id": "skill.list", "group": "skill", "tool": "list_skills",
     "name": "技能清单", "scopes": [], "scope_kind": "local", "transport": "local",
     "endpoints": []},
    {"id": "skill.load", "group": "skill", "tool": "load_skill",
     "name": "加载技能全文", "scopes": [], "scope_kind": "local", "transport": "local",
     "endpoints": []},
    {"id": "kb.search", "group": "kb", "tool": "search_knowledge",
     "name": "知识库检索（SOP/规则/案例）", "scopes": [], "scope_kind": "kb",
     "transport": "local", "endpoints": []},
    {"id": "meta.plan_query", "group": "meta", "tool": "plan_query",
     "name": "查询计划", "scopes": [], "scope_kind": "local", "transport": "local",
     "endpoints": []},
    {"id": "memory.search", "group": "memory", "tool": "search_analyses",
     "name": "检索历史档案", "scopes": [], "scope_kind": "org", "transport": "local",
     "endpoints": []},
    {"id": "memory.read", "group": "memory", "tool": "read_analysis",
     "name": "读取档案全文", "scopes": [], "scope_kind": "org", "transport": "local",
     "endpoints": []},
    {"id": "memory.save", "group": "memory", "tool": "save_analysis",
     "name": "沉淀分析档案", "scopes": [], "scope_kind": "org", "transport": "local",
     "endpoints": []},
]


def groups() -> dict[str, list[str]]:
    """分组 → 工具名列表（Agent 侧 `TOOL_GROUPS` 的数据源）。"""
    out: dict[str, list[str]] = {}
    for item in CAPABILITIES:
        out.setdefault(item["group"], []).append(item["tool"])
    return {key: sorted(value) for key, value in sorted(out.items())}


def scope_kinds() -> dict[str, str]:
    """工具名 → 部门作用域种类（Agent 侧 `TOOL_SCOPES` 的数据源）。"""
    return {item["tool"]: item["scope_kind"] for item in CAPABILITIES}


def catalog() -> dict:
    """对外下发的目录快照（`GET /api/v1/metadata/capabilities`）。"""
    return {
        "version": CONTRACT_VERSION,
        "capabilities": CAPABILITIES,
        "groups": groups(),
        "reserved_groups": RESERVED_GROUPS,
        "note": ("能力以中台为准；Agent 用提交的快照离线启动，启动时与本接口对版本，"
                 "不一致会出现在 /api/health.capabilities"),
    }


def validate() -> list[str]:
    """目录自检：ID/工具名唯一、分组非空、必填字段齐全。返回问题列表（空=通过）。"""
    problems: list[str] = []
    seen_ids: set[str] = set()
    seen_tools: set[str] = set()
    for item in CAPABILITIES:
        for field in ("id", "group", "tool", "name", "scope_kind", "transport"):
            if not item.get(field):
                problems.append(f"能力缺少字段 {field}：{item}")
        if item["id"] in seen_ids:
            problems.append(f"能力 id 重复：{item['id']}")
        if item["tool"] in seen_tools:
            problems.append(f"工具名重复：{item['tool']}")
        seen_ids.add(item["id"])
        seen_tools.add(item["tool"])
        if item["group"] in RESERVED_GROUPS:
            problems.append(f"能力 {item['id']} 使用了预留分组 {item['group']}")
    for group, tools in groups().items():
        if not tools:
            problems.append(f"分组 {group} 为空：空分组会让 tools:[group] 静默退化为全部工具")
    return problems
