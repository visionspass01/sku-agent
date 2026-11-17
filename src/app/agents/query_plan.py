"""查询理解与规划（query_plan）：先把「用户这句话要查什么」变成结构化计划，再去取数。

三层用途（与设计文档 1.6 一致）：
1. 会话内解析（必须）：意图 / 实体 / 时间窗 / 必需数据 —— 决定调哪些工具；
2. 检索改写（需要）：关键词扩展，用于 RAG 与档案检索；
3. 生成式整句改写（**默认关闭**，`query_rewrite_llm=True` 才启用）：只作为附加信息，
   不替换用户原句，避免「改写后意思变了却查不出错」。

可复现性：plan_id = 规范化 JSON 的 sha1（键排序、剔除 as_of / plan_id），
同输入跨会话稳定；再配合 as_of / metric_version / sql_fingerprints 形成最小可复现三元组。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re

from pydantic import BaseModel, Field

from app.config import DEPARTMENT_CATEGORIES, get_settings
from app.memory.session_memory import (
    COLOR_RE,
    DEPARTMENTS,
    METRIC_WORDS,
    SIZE_RE,
    SKU_RE,
    STYLE_RE,
    TIME_RE,
)

INTENTS = ("lookup", "diagnose", "restock", "transfer", "patrol", "approval", "policy_qa")

# 意图关键词（顺序即优先级：动作类先于问数类）
# 复审 P2：**不要放裸的「通过」**——"通过调拨能不能改善断码"、"通过什么方式能减少积压"
# 这类问法里的「通过」是介词，会被判成 approval（required_data 变成 approvals，
# 计划还把"必需数据：审批单"注入提示词）。只保留强搭配（审批/审核通过/驳回…）。
_INTENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("approval", ("审批", "审批通过", "审核通过", "待确认", "补货单列表", "同意补货", "驳回", "确认单")),
    ("restock", ("补货", "补多少", "补单", "备货", "下单")),
    ("transfer", ("调拨", "调货", "跨仓", "移库")),
    ("patrol", ("巡检", "批量", "全量跑", "扫一遍", "跑一轮")),
    ("policy_qa", ("sop", "口径", "规则", "流程", "制度", "怎么算", "定义", "标准")),
    ("diagnose", ("风险", "断货", "断码", "积压", "滞销", "诊断", "影响", "要不要", "该不该")),
)

# 需要「具体 SKU/款号」才能作答的意图（复审 P6）：口径解释（policy_qa）、批量巡检（patrol）、
# 审批清单（approval）都不需要，不该追问"要分析的 SKU"。部门/品类面问题（如"女装断码严重吗"）
# 有 `get_apparel_overview` 这类聚合工具可答，同样不追问。
_ENTITY_INTENTS = frozenset({"lookup", "diagnose", "restock", "transfer"})

# 意图 → 必需数据（也是 sql_fingerprints 的组成）
_REQUIRED_DATA: dict[str, tuple[str, ...]] = {
    "lookup": ("inventory", "sell_through"),
    "diagnose": ("inventory", "in_transit", "reservations", "orders", "metrics"),
    "restock": ("metrics", "inventory", "sales", "in_transit"),
    "transfer": ("inventory", "in_transit"),
    "patrol": ("screening", "metrics"),
    "approval": ("approvals",),
    "policy_qa": ("kb",),
}

_AMBIGUOUS = ("这个款", "这款", "它", "那个", "刚才", "上面", "这些", "那几个")

# 检索关键词词表（命中原文才收录，不做自由 n-gram 拼接，避免「个款/款需」这类噪音）
_EXTRA_TERMS = (
    "断货风险", "断货", "补货单", "补货", "调拨建议", "调拨", "跨仓", "待确认", "审批",
    "人工确认", "SOP", "口径", "巡检", "周报", "复盘", "供应商", "交期", "MOQ", "折扣",
    "换季", "大促", "尺码", "颜色", "款号", "动销", "库存",
)


def _canonical(payload: dict) -> str:
    clean = {k: v for k, v in payload.items() if k not in ("as_of", "plan_id", "queried_at")}
    return json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _detect_intent(message: str, skus: list[str]) -> tuple[str, str]:
    low = (message or "").lower()
    for intent, words in _INTENT_RULES:
        for word in words:
            if word in low:
                return intent, word
    return "lookup", "sku" if skus else "default"


def _parse_time(message: str) -> dict:
    match = TIME_RE.search(message or "")
    if match is None:
        return {"label": "", "start": "", "end": ""}
    label = match.group(1).replace(" ", "")
    today = dt.date.today()
    days = 7
    num = re.search(r"\d+", label)
    if num:
        n = int(num.group(0))
        if "小时" in label:
            days = max(1, n // 24)
        elif "周" in label:
            days = n * 7
        elif "月" in label:
            days = n * 30
        else:
            days = n
    return {"label": label, "start": (today - dt.timedelta(days=days)).isoformat(),
            "end": today.isoformat()}


def _working_set_values(working_set: dict | None, key: str, limit: int = 8) -> list[str]:
    return list((working_set or {}).get(key) or {})[:limit]


def _visible_departments(user) -> list[str] | None:
    """账号可见的**部门**名单（None = 不限制）。admin/匿名不限；部门账号只有本部门。"""
    if user is None or getattr(user, "is_admin", False):
        return None
    dept = (getattr(user, "department", None) or "").strip()
    return [dept] if dept else []


def department_of_token(token: str) -> str | None:
    """把问句里的部门名/品类名归到所属部门（复审 A3）。

    为什么需要它：问句里可能写的是部门名（男装），也可能是该部门的品类名（童装）；
    早先 `blocked` 直接拿"问句里的部门名"去比"账号可见的品类列表"——只有平台下发的
    映射恰好同名（demo 里男装→男装、女装→女装+童装）才成立，一旦映射是
    男装→上衣/裤装 就会把所有带部门名的问句误判成越权。这里统一先归一到部门再判定。
    """
    token = (token or "").strip()
    if not token:
        return None
    for dept, cats in DEPARTMENT_CATEGORIES.items():
        if token == dept or token in cats:
            return dept
    return None


def _detect_categories(message: str) -> list[str]:
    """问句里提到的部门/品类 token：固定词表（男装/女装/童装）优先，再补平台下发的词。

    为什么不能只查固定词表（复审 A3）：`set_department_categories` 支持平台下发
    「部门 → 品类」映射；若映射里的品类叫"上衣/裙装"，只查固定词表就完全看不到问句
    里的部门信息（`blocked` 直接失效），所以词表要包含当前生效映射里的部门名与品类名。
    """
    found: list[str] = []
    for token in DEPARTMENTS:
        if token and token in message and token not in found:
            found.append(token)
    for dept, cats in DEPARTMENT_CATEGORIES.items():
        for token in (dept, *cats):
            if token and token in message and token not in found:
                found.append(token)
    return found


def build_query_plan(message: str, *, user=None, working_set: dict | None = None,
                     metric_version: str | None = None) -> dict:
    """确定性生成查询计划（不调 LLM）。任何字段缺失都不会抛异常。"""
    settings = get_settings()
    message = message or ""
    skus = sorted({m.upper() for m in SKU_RE.findall(message)})
    styles = sorted({m.upper() for m in STYLE_RE.findall(message)})
    sku_sizes = {s.split("-")[-1].upper() for s in skus}
    sizes = [m.upper() for m in SIZE_RE.findall(message) if m.upper() not in sku_sizes]
    colors = sorted({m for m in COLOR_RE.findall(message)})
    categories = _detect_categories(message)
    metrics = [w for w in METRIC_WORDS if w in message]
    time_range = _parse_time(message)
    intent, hit = _detect_intent(message, skus)
    assumptions: list[str] = []

    resolved_from = ""
    if not skus and not styles:
        ws_skus = _working_set_values(working_set, "skus")
        ws_styles = _working_set_values(working_set, "styles")
        if any(word in message for word in _AMBIGUOUS) or (ws_skus and not message.strip()):
            if ws_skus:
                skus = ws_skus[:3]
                resolved_from = "working_set.skus"
                assumptions.append("指代回填：本轮未显式给出 SKU，按本会话工作集的最近 SKU 处理")
            elif ws_styles:
                styles = ws_styles[:3]
                resolved_from = "working_set.styles"
                assumptions.append("指代回填：按本会话工作集的最近款号处理")
    if not categories:
        ws_cats = _working_set_values(working_set, "categories")
        if ws_cats:
            categories = ws_cats[:2]
            assumptions.append("部门/品类沿用本会话工作集")
    if not time_range["label"]:
        ws_time = ((working_set or {}).get("time_range") or {}).get("label")
        if ws_time:
            time_range = {**_parse_time(ws_time), "label": ws_time, "inherited": True}
            assumptions.append(f"时间窗沿用本会话工作集：{ws_time}")
        else:
            assumptions.append("未指定时间窗：按近 7 天口径回答")
    if intent in ("lookup", "diagnose") and not metrics:
        assumptions.append("未点明指标：按该意图的默认指标集回答")

    allowed = _visible_departments(user)
    asked_departments = sorted({d for t in categories if (d := department_of_token(t))})
    blocked = bool(asked_departments and allowed is not None and
                   any(dep not in allowed for dep in asked_departments))
    if blocked:
        assumptions.append(
            f"问题涉及的部门 {asked_departments} 不在当前账号可见范围（{'、'.join(allowed or [])}），"
            "工具层会返回无权限"
        )

    required_data = list(_REQUIRED_DATA.get(intent, ()))
    confidence = 0.55
    if skus:
        confidence += 0.2 if resolved_from == "" else 0.05
    if styles and not skus:
        confidence += 0.08
    if time_range["label"]:
        confidence += 0.05 if time_range.get("inherited") else 0.06
    if metrics:
        confidence += 0.05
    if categories:
        confidence += 0.04
    if blocked:
        confidence = max(0.05, confidence - 0.25)
    if not skus and not styles:
        confidence -= 0.2
    confidence = round(min(0.98, max(0.05, confidence)), 2)

    sql_fps = [{"table": name, "fingerprint": _fingerprint(intent, name,
                                                           ",".join(skus), time_range["label"])}
               for name in required_data]

    plan_core = {
        "intent": intent,
        "entities": {"skus": skus, "styles": styles, "sizes": sizes, "colors": colors,
                     "categories": categories, "departments": categories,
                     # 复审 A3：问句涉及哪些**部门**（由部门名/品类名归一而来）
                     "asked_departments": asked_departments},
        "time_range": time_range,
        "filters": {"warehouse": None, "channel": None},
        "required_data": required_data,
        "retrieval_query": {
            "original": message,
            "keywords": _keywords(message, skus, styles, metrics, colors, sizes),
            "expanded": _expand(message, categories, intent),
        },
        "assumptions": assumptions,
        "confidence": confidence,
        "resolved_from": resolved_from,
        # 语义修正：allowed_departments 是**部门**名单（早先装的是品类，靠 demo 同名才没出事）
        "allowed_departments": allowed,
        "blocked": blocked,
        "tool_fingerprints": sql_fps,
    }
    plan_id = "qp_" + hashlib.sha1(_canonical(plan_core).encode("utf-8")).hexdigest()[:16]

    # 复审 P6：追问只在「真的缺实体」时发。早先条件是"置信度低就追问"，于是
    # 「女装断码严重吗」「安全库存怎么算」「待确认的补货单有哪些」这类**不需要 SKU**
    # 的问题也会被追问"请补充要分析的 SKU 或款号"——那一栏在界面上就是一张误导卡片。
    missing: list[str] = []
    if intent in _ENTITY_INTENTS and not skus and not styles and not categories:
        missing.append("要分析的 SKU 或款号")
    if intent == "lookup" and not metrics:
        missing.append("关注指标（库存/售罄/销量等）")
    needs_clarify = confidence < settings.query_plan_confidence_threshold and bool(missing)
    clarify = None
    if needs_clarify:
        clarify = {
            "question": "为了给准确结论，请补充：" + "、".join(missing),
            "options": (skus or [])[:3] + ["近 7 天", "近 30 天"],
            "missing": missing,
        }

    return {
        "plan_id": plan_id,
        "schema_version": "1.0",
        "as_of": dt.datetime.now().isoformat(timespec="seconds"),
        "metric_version": metric_version or settings.metric_version,
        "intent": intent,
        "confidence": confidence,
        "needs_clarify": needs_clarify,
        "clarify": clarify,
        "generative_rewrite": None,
        **plan_core,
    }


def _keywords(message: str, skus: list[str], styles: list[str], metrics: list[str],
              colors: list[str], sizes: list[str]) -> list[str]:
    """检索关键词：实体 + 命中的业务词表（不做自由 n-gram，BM25 侧会自行分词）。"""
    words: list[str] = []
    for group in (skus, styles, metrics, colors, sizes):
        for item in group:
            if item and item not in words:
                words.append(item)
    for term in _EXTRA_TERMS:
        if term in (message or "") and term not in words:
            words.append(term)
    return words[:16]


def _expand(message: str, categories: list[str], intent: str) -> list[str]:
    extra: list[str] = []
    if categories:
        extra.append(" ".join(categories))
    synonyms = {
        "sell_through": "售罄 天数 断货 风险",
        "restock": "补货 安全库存 再订货点 MOQ",
        "transfer": "调拨 跨仓 库存分布",
        "approval": "补货单 人工确认 审批",
        "patrol": "批量巡检 异常清单",
        "policy_qa": "SOP 制度 流程 阈值",
    }
    if intent in synonyms:
        extra.append(synonyms[intent])
    return extra or []


# ---------------------------------------------------------------- 生成式整句改写（默认关闭）
class _Rewrite(BaseModel):
    rewrite: str = Field(default="", description="检索用的改写问句，保留原意与实体")
    keywords: list[str] = Field(default_factory=list)


async def maybe_generative_rewrite(plan: dict, message: str) -> dict:
    """仅当 `query_rewrite_llm=True` 时调用 LLM 生成检索改写；失败/关闭则原样返回。

    改写结果只写入 `generative_rewrite` 字段，**永不替换**用户原句与 plan 结构。
    """
    settings = get_settings()
    if not settings.query_rewrite_llm or not settings.deepseek_api_key:
        return plan
    try:
        from app.llm import get_llm

        system = (
            "你是检索改写器。把用户问题改写成一条更适合关键词检索的问句：必须保留原有 SKU、"
            "款号、时间窗等实体，不得新增业务结论，不得改变意图。只输出 JSON。"
        )
        user = json.dumps({
            "question": message,
            "intent": plan.get("intent"),
            "entities": plan.get("entities"),
            "time_range": plan.get("time_range"),
        }, ensure_ascii=False)
        result = await get_llm().invoke_json(system, user, _Rewrite, max_tokens=300)
        plan = dict(plan)
        plan["generative_rewrite"] = {
            "rewrite": (result.rewrite or "")[:200],
            "keywords": [k for k in (result.keywords or []) if k][:12],
            "note": "仅用于检索，不替换原问题",
        }
    except Exception as exc:  # noqa: BLE001 —— 改写失败必须降级，不影响主流程
        plan = dict(plan)
        plan["generative_rewrite"] = {"error": f"改写失败已降级：{type(exc).__name__}"}
    return plan


def plan_brief(plan: dict) -> str:
    """把计划压成一段注入 system 的文本（只追加，不改写用户原句）。"""
    if not plan:
        return ""
    entities = plan.get("entities") or {}
    lines = [
        f"- 意图: {plan.get('intent')}（置信度 {plan.get('confidence')}）",
        f"- SKU: {', '.join(entities.get('skus') or []) or '（未指定）'}",
        f"- 款号: {', '.join(entities.get('styles') or []) or '（未指定）'}",
        f"- 尺码/颜色: {', '.join(entities.get('sizes') or []) or '-'} / "
        f"{', '.join(entities.get('colors') or []) or '-'}",
        f"- 时间窗: {(plan.get('time_range') or {}).get('label') or '默认近7天'}",
        f"- 必需数据: {', '.join(plan.get('required_data') or [])}",
        f"- 假设: {'；'.join(plan.get('assumptions') or []) or '无'}",
        f"- plan_id: {plan.get('plan_id')}（口径 {plan.get('metric_version')}）",
    ]
    return "## 本轮查询计划（query_plan，由系统预解析）\n" + "\n".join(lines)
