"""部门数据作用域：**唯一实现**。

为什么单独成模块（评审缺陷 A1）：
在这之前，同一件事——「admin 看全量、部门账号只看本品类」——有 4 份实现：
`auth.scope_categories`、`api.chat_routes._org_scope`、`agents.chat_agent._org_scope`、
`tools.platform_tools._scope_departments`。而**执行点只有「带 sku_id 的工具」这一种**：
每个工具函数自己写一行 `if (err := _scope_error(user, sku_id))`。
于是凡是入参里没有 sku_id 的工具就完全没有作用域校验，`list_pending_approvals`
会把别的部门的待审批单返回给部门账号（HTTP 路由 `/api/actions/pending` 有过滤，
Agent 工具没有——同一份规则两处实现，只有一处正确）。

现在的约定：
- 作用域判定只写在**这里**；
- 工具层通过能力目录的 `scope_kind` 声明（`platform_tools.TOOL_SCOPES`），
  由 `build_platform_tools` 统一套守卫（新增能力不声明就装配失败，见 `_assert_tool_scopes`）；
- 路由层复用这里的函数，不再各自实现一遍；
- **sku_id → category 走 `app/tools/catalog`（平台元数据目录）**，而不是 Agent 自己的 `sku` 表：
  业务数据只有中台一份真相，Agent 不再持有私有副本（评审 耦合点1）。
- **order_id → sku_id 走 order MCP**（与取数同一条通道），不再直读 `mock_order` 表。
  结果按订单号缓存 10 分钟（MCP 自己也有 600s TTL，实际几乎零额外开销）。
"""

from __future__ import annotations

import logging
import time

from app.auth import AuthUser, scope_categories
from app.config import COMMON_SOP_DEPARTMENT
from app.rag.facets import ordered_departments as _ordered_departments
from app.tools import catalog

logger = logging.getLogger("sku-agent.scope")

# 作用域种类（与能力目录 capabilities.py 的 scope_kind 一一对应）
SCOPE_KINDS = frozenset({"sku", "order", "sku_list", "department", "style", "kb", "global", "local", "org"})

_ORDER_TTL_SECONDS = 600.0
_ORDER_SKU: dict[str, tuple[float, str | None]] = {}


def reset_order_cache() -> None:
    """测试用：清掉 order → sku 缓存。"""
    _ORDER_SKU.clear()


def ordered_departments(departments) -> list[str]:  # noqa: ANN001 —— 任意可迭代
    """按部门配置的**声明顺序**排列（唯一实现在 `app.rag.facets`，这里只做转发）。"""
    return _ordered_departments(departments)


def kb_department_filter(user: AuthUser | None,
                        departments: "frozenset[str] | list[str] | tuple[str, ...] | None" = None
                        ) -> dict:
    """知识库部门可见性：**唯一实现**，路由与对话工具都必须复用。

    规则：admin / 匿名 → `{}`（全量，不加过滤）；部门账号 → `{"department": [本部门, "通用"]}`。

    为什么必须收口（复审 P1-1）：`/api/rag/search` 对管理员返回全量，而对话工具
    `search_knowledge` 早先自己算了一遍 dept —— admin 被降级成「只看通用」。
    于是同一个问题两条路径答案不同：工具侧只召回通用《清仓 SOP》→ 口径冲突检查
    返回 `none`（看起来"已核对、没冲突"）→ 模型自信地答 60 天；REST 侧却能召回
    女装细则并按权威度裁决成 45 天。**冲突机制没错，错在可见性少算了一层。**

    `departments`（复审 P1：部门面）：管理员问句里出现具体部门时把检索收敛到
    「该部门 + 通用」。这既是相关性优化，也是裁决的前提——不解这一层，
    管理员问"男装清仓触发天数"会拿女装细则 45 天去压通用 60 天。
    """
    if user is None or user.is_admin:
        wanted = ordered_departments(departments)
        if wanted:
            return {"department": [*wanted, COMMON_SOP_DEPARTMENT]}
        return {}
    dept = (user.department or "").strip()
    return {"department": [dept, COMMON_SOP_DEPARTMENT] if dept else [COMMON_SOP_DEPARTMENT]}


def kb_scope_label(user: AuthUser | None,
                  departments: "frozenset[str] | list[str] | tuple[str, ...] | None" = None) -> str:
    """知识库检索范围的**人话自述**：路由与工具共用同一句话（复审 P2-2）。

    早先两处各拼一遍：工具里 `''.join(["女装", "通用"])` 拼出 "女装通用部门内部 SOP"，
    REST 里是 "女装部门内部 SOP + 通用 SOP" —— 同一件事两个说法，模型还会把
    "女装通用部门" 念给用户。文案收口到这里，改一处就都变。

    管理员按问题部门收敛时（`departments` 非空）与部门账号用同一句式，便于审计对齐。
    """
    dept_names = ordered_departments(departments)
    if user is None or user.is_admin:
        if dept_names:
            return f"{'、'.join(dept_names)}部门内部 SOP + {COMMON_SOP_DEPARTMENT} SOP（按问题部门收敛）"
        return "全部 SOP"
    dept = (user.department or "").strip()
    return (f"{dept}部门内部 SOP + {COMMON_SOP_DEPARTMENT} SOP"
            if dept else f"{COMMON_SOP_DEPARTMENT} SOP")


def visible_categories(user: AuthUser | None) -> list[str] | None:
    """admin / 匿名 → None（全量）；部门用户 → 该部门品类集合（女装含童装）。"""
    return scope_categories(user)


def sku_category_map(sku_ids) -> dict[str, str]:  # noqa: ANN001 —— 接受任意可迭代
    """一次查询拿到 sku_id → category（列表类返回体做作用域过滤用）。"""
    ids = [sid for sid in dict.fromkeys(sku_ids or []) if sid]
    if not ids:
        return {}
    out: dict[str, str] = {}
    for sku_id in ids:
        category = catalog.sku_category(sku_id)
        if category:
            out[sku_id] = category
    return out


def ensure_sku_visible(user: AuthUser | None, sku_id: str | None) -> dict | None:
    """SKU 是否可见：返回 business error dict 或 None（None = 允许）。"""
    if user is None or user.is_admin:
        return None
    if not sku_id:
        return {"error": "缺少 sku_id：无法校验数据权限", "error_kind": "business"}
    category = catalog.sku_category(sku_id)
    if category is None:
        # 目录里没有 → 视为不可见（宁严不宽）：既可能是真的不存在，也可能是跨部门
        return {"error": f"未知 SKU：{sku_id}（或不在当前账号可见目录内）",
                "error_kind": "business"}
    cats = visible_categories(user)
    if cats is not None and category not in cats:
        return {"error": f"无权限：当前账号（{user.department}部门）不能访问 {sku_id} 的数据",
                "error_kind": "business"}
    return None


async def _order_sku(order_id: str) -> str | None:
    """order_id → sku_id（走 order MCP，带 10 分钟缓存）。

    **复审 F4**：只缓存"确定性结论"——拿到订单（有 SKU）或中台明确说"订单不存在"。
    链路故障（异常、超时、中台不可用）不写缓存：早先它会把一次抖动缓存成
    "订单不存在" 10 分钟，部门账号这段时间里查该订单都会得到误报。
    （方向仍然是 fail-closed：拿不到 SKU 就当看不见，只是不再把故障固化。）
    """
    cached = _ORDER_SKU.get(order_id)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _ORDER_TTL_SECONDS:
        return cached[1]
    sku_id: str | None = None
    try:
        from app.mcp_client.client import get_mcp_hub

        data = await get_mcp_hub().call("order", "get_order_status", {"order_id": order_id})
    except Exception as exc:  # noqa: BLE001 —— 取不到就是"不确定"：按不可见处理，且不缓存
        logger.debug("订单 %s 反查 SKU 失败（不缓存，下次重试）：%s", order_id, exc)
        return None
    if not isinstance(data, dict) or data.get("error"):
        logger.debug("订单 %s 反查 SKU 返回异常结果（不缓存）：%s", order_id, data)
        return None
    sku_id = str(((data.get("order") or {}).get("sku_id")) or "") or None
    _ORDER_SKU[order_id] = (now, sku_id)   # 有 SKU 或明确 found=False 都是确定性结论
    return sku_id


async def ensure_order_visible(user: AuthUser | None, order_id: str | None) -> dict | None:
    """订单是否可见：订单 → SKU → 部门作用域（订单工具的统一入口，异步）。"""
    if user is None or user.is_admin:
        return None
    if not order_id:
        return {"error": "缺少 order_id：无法校验数据权限", "error_kind": "business"}
    sku_id = await _order_sku(order_id)
    if sku_id is None:
        return {"error": f"订单不存在：{order_id}", "error_kind": "business"}
    return ensure_sku_visible(user, sku_id)


def ensure_style_visible(user: AuthUser | None, style_id: str | None) -> dict | None:
    """款是否可见：款 → 品类 → 部门作用域（款级指标/尺码结构类工具的入口）。

    为什么不能只按 sku_id 判：`get_size_gap` / `get_apparel_overview` 的入参是部门维度，
    `get_style_metrics` 的入参是款——款里可能同时有多个 SKU，必须按款所属品类判。
    """
    if user is None or user.is_admin:
        return None
    if not style_id:
        return {"error": "缺少 style_id：无法校验数据权限", "error_kind": "business"}
    category = catalog.style_category(style_id)
    if category is None:
        return {"error": f"未知款式：{style_id}（或不在当前账号可见目录内）",
                "error_kind": "business"}
    cats = visible_categories(user)
    if cats is not None and category not in cats:
        return {"error": f"无权限：当前账号（{user.department}部门）不能访问 {style_id} 的数据",
                "error_kind": "business"}
    return None


def filter_visible_items(user: AuthUser | None, items: list[dict],
                         key: str = "sku_id") -> tuple[list[dict], int]:
    """按部门作用域过滤「列表类」返回体：返回 (保留项, 被过滤掉的条数)。

    作用域内的 SKU 查不到品类时按「不可见」处理（宁严不宽）。
    """
    cats = visible_categories(user)
    rows = [item for item in (items or []) if isinstance(item, dict)]
    if cats is None:
        return rows, 0
    cat_map = sku_category_map([row.get(key) for row in rows])
    kept = [row for row in rows if cat_map.get(row.get(key)) in cats]
    return kept, len(rows) - len(kept)
