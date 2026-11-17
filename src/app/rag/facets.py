"""适用面（façade）：这次提问属于哪个部门 —— 决定哪些口径**可比**。

为什么需要这一层（复审 P1 / 规则 C1-C3）：
权威度阶梯（中台治理口径 > 部门细则 > 通用规则 > 历史案例）描述的是**同一适用面内**
谁优先。它不能回答"这条规则适不适用"。把两者混在一起，就会出现：

    管理员问「男装清仓触发条件」→ 男装细则没有该口径 → 裁决变成
    「女装部门细则 45 天 vs 通用规则 60 天」→ 女装胜出 → 男装问题被答成 45 天。

**部门面是可比性闸门，不是排序权重。** 只有适用面相交的两条制度才谈得上冲突；
不相交就是并行规则（女装 45 天 / 男装按通用 60 天），谁也不是谁的胜出方。

识别不到部门面时**保守处理**：`facets=None`，只有通用/governance 文档参与裁决，
部门文档降级为"部门例外"（`department_overrides`），由回答以"若问的是 X 部门则为 Y"补充。
"""

from __future__ import annotations

from app.config import COMMON_SOP_DEPARTMENT, DEPARTMENT_CATEGORIES, department_of

# 部门/品类词表：识别不到就当作"无部门面"（保守，不会给出错误部门的裁决）。
# 童装归女装部门（映射以 `config.DEPARTMENT_CATEGORIES` 为唯一真相，中台可覆盖）。
DEPARTMENT_WORDS: dict[str, str] = {
    "男装": "男装", "男式": "男装", "男士": "男装", "男款": "男装",
    "女装": "女装", "女式": "女装", "女士": "女装", "女款": "女装",
    "童装": "童装", "儿童": "童装", "童款": "童装", "女童": "童装", "男童": "童装",
}

COMMON_DEPARTMENTS = frozenset({COMMON_SOP_DEPARTMENT, "全部", ""})

# **部门**比较意图词：用户要"跨部门比一比"，但**没有点名具体部门**时（复审 P2-1）。
# 只放"部门"语义的比较词；`区别/对比/分别/相比/一样吗` 这类**通用比较词**不算——
# 它们跟部门无关（"清仓和补货的区别""两个供应商交期对比"），算进来就会给
# 与部门无关的问题塞一句"这是跨部门比较、只按 X 部门作答"（答非所问）。
DEPARTMENT_COMPARISON_WORDS: tuple[str, ...] = (
    "哪个部门", "哪些部门", "哪个品类线", "各部门", "别的部门", "其它部门", "其他部门",
    "跨部门", "部门之间", "部门间", "各线",
)


def is_comparative(message: str | None) -> bool:
    """问句是否是"跨部门比较"意图（「哪个部门门槛更低」「各部门一样吗」）。

    只认**部门语义**的比较词；点名了多个部门的情况由 `len(asked) > 1` 另外覆盖，
    所以这里不需要通用比较词。
    """
    text = message or ""
    return any(word in text for word in DEPARTMENT_COMPARISON_WORDS)


def ordered_departments(departments) -> list[str]:  # noqa: ANN001 —— 任意可迭代
    """按部门配置的**声明顺序**排列（未知部门追加在尾部，去重）。

    为什么要排序而不是直接 `sorted()`：`sorted` 按码位排，会把「男装、女装」渲染成
    「女装、男装」，既不符合业务文档书写顺序，也会让 `scope` 文案与 notice 文案对不上。
    配置顺序才是人读的顺序。**唯一实现**（`security.scope` 与冲突提示都复用它）。
    """
    wanted = [str(d).strip() for d in (departments or []) if str(d).strip()]
    order = list(DEPARTMENT_CATEGORIES)
    known = [d for d in order if d in wanted]
    unknown = sorted({d for d in wanted if d not in order})
    return known + unknown


def query_departments(message: str | None) -> frozenset[str]:
    """从问句里识别部门面（空集 = 没识别到，等同"无部门面"）。"""
    text = message or ""
    found: set[str] = set()
    for word, department in DEPARTMENT_WORDS.items():
        if word in text:
            found.add(department_of(department) or department)
    return frozenset(found)


def facets_from_filters(filters: dict | None) -> frozenset[str] | None:
    """检索 filters → 部门面（`None` = 无部门面）。

    这样批量巡检（按 SKU 品类传 `filters={"department": [dept, "通用"]}`）和部门账号
    天然自带部门面，不需要额外参数；只有"管理员问通用问题"才是 `None`。
    """
    if not filters:
        return None
    want = filters.get("department")
    if not want:
        return None
    items = want if isinstance(want, (list, tuple, set, frozenset)) else [want]
    departments = {str(item).strip() for item in items}
    departments -= COMMON_DEPARTMENTS
    return frozenset(d for d in departments if d) or None


def is_common(department: str | None) -> bool:
    """是否是"对所有部门都适用"的文档（通用 / 全部 / 未标注）。"""
    return (department or "").strip() in COMMON_DEPARTMENTS


def claim_applicable(department: str | None, facets: frozenset[str] | None) -> bool:
    """这条口径是否落在问题适用面内（C1 / C2）。"""
    if is_common(department):
        return True
    if facets is None:            # 无部门面：部门文档只能是"例外"，不参与裁决
        return False
    return (department or "").strip() in facets
