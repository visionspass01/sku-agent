"""看板/接口用的指标汇总（按账号权限计算数据域）。

- 管理员：SKU 总数 / 异常 SKU 为全部门汇总，并返回分部门明细（点击卡片查看）；
- 部门用户：只统计本部门数据，不返回其他部门明细；
- 缓存命中率为进程级全局指标；
- LLM 调用按「发起调用的 SKU 所属部门」归因（对话按账号部门归因），支持分部门明细；
- 队列按消息 payload 的 department 统计分部门 length / pending。
"""

from __future__ import annotations

import time

from sqlalchemy import func, select

from app.auth import AuthUser
from app.cache.redis_factory import redis_mode
from app.cache.stream_queue import get_stream_queue
from app.cache.ttl_cache import get_ttl_cache
from app.config import categories_for, department_label, department_of
from app.db import repos
from app.db.engine import session_scope
from app.db.models import (
    InspectionResult,
    InspectionRun,
    Sku,
    StatHour,
)
from app.llm import get_llm
from app.observability import latency, runtime_state, tool_stats
from app.observability import trace as trace_mod


def _visible_departments(user: AuthUser | None) -> set[str] | None:
    """返回当前账号可见的部门集合；None 表示全部。"""
    if user is None or user.is_admin:
        return None
    return {user.department} if user.department else set()


def _filter_dept_map(mapping: dict, visible: set[str] | None) -> dict:
    if visible is None:
        return dict(mapping)
    # 管理员/部门之外的「管理员」归因桶对部门用户不可见
    return {k: v for k, v in mapping.items() if k in visible}


def binary_confusion(rows: list[dict], *, truth_key: str = "truth_pos",
                     pred_key: str = "pred_pos") -> dict:
    """二分类混淆矩阵（给评测脚本用，纯函数）。

    `truth` / `pred` 为 `None` 的样本记进 `unjudged`、**不进矩阵**：
    评测里最容易被高估的指标就来自"拿默认值补空预测"——比如 Agent 研判跑失败时
    预案为空，若默认按"中风险"算，这一轮会被记成 TP/FP，precision 直接虚高（复审 N7）。
    """
    tp = fp = fn = tn = unjudged = 0
    for row in rows or []:
        truth = row.get(truth_key)
        pred = row.get(pred_key)
        if truth is None or pred is None:
            unjudged += 1
            continue
        if truth and pred:
            tp += 1
        elif pred:
            fp += 1
        elif truth:
            fn += 1
        else:
            tn += 1
    judged = tp + fp + fn + tn
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "judged": judged, "unjudged": unjudged,
        "accuracy": round((tp + tn) / judged, 3) if judged else 0.0,
        "precision": round(precision, 3) if judged else 0.0,
        "recall": round(recall, 3) if judged else 0.0,
        "f1": round(f1, 3) if judged else 0.0,
    }


def metrics_snapshot(user: AuthUser | None = None) -> dict:
    categories = None if (user is None or user.is_admin) else categories_for(user.department)
    visible = _visible_departments(user)

    with session_scope() as session:
        sku_count = repos.count_skus(session, categories)
        anomaly_count = repos.count_anomalies(session, categories)
        stat_rows = int(session.scalar(select(func.count()).select_from(StatHour)) or 0)
        result_count = int(session.scalar(select(func.count()).select_from(InspectionResult)) or 0)
        run_count = int(session.scalar(select(func.count()).select_from(InspectionRun)) or 0)

        if user is not None and not user.is_admin:
            departments = {user.department: {
                "sku_count": sku_count,
                "anomaly_count": anomaly_count,
            }}
        else:
            departments = repos.breakdown_by_department(session)

        # 队列按部门统计需要 sku → department 映射
        sku_dept = {s.sku_id: department_of(s.category) for s in session.query(Sku)}

    def resolver(sku_id: str):
        return sku_dept.get(sku_id)

    cache = get_ttl_cache()
    llm = get_llm()
    queue = get_stream_queue()

    llm_departments = _filter_dept_map(llm.department_stats, visible)
    queue_stats = queue.stats(department_resolver=resolver)
    queue_stats["departments"] = _filter_dept_map(queue_stats.get("departments") or {}, visible)
    queue_stats["chat_streams"] = runtime_state.active_chat_streams()
    queue_stats["note"] = "巡检走 Redis Stream 队列；对话是同步 ReAct（SSE 直推），不计入队列，仅统计进行中的 SSE 连接数"

    if user is None:
        scope = {"role": "anonymous", "department": None, "label": "未登录（只读浏览）"}
    elif user.is_admin:
        scope = {"role": "admin", "department": None, "label": "全部门汇总"}
    else:
        scope = {
            "role": "user", "department": user.department,
            "label": f"{department_label(user.department)}（本部门）",
        }

    return {
        "scope": scope,
        "sku_count": sku_count,
        "stat_rows": stat_rows,
        "anomaly_count": anomaly_count,
        "inspection_result_count": result_count,
        "run_count": run_count,
        "departments": departments,
        "cache": cache.snapshot(),
        "llm": {**llm.stats, "departments": llm_departments, "sources": llm.source_stats,
                "cost": llm.cost()},
        "queue": queue_stats,
        "redis_mode": redis_mode(),
        # 工具调用可靠性（per server.tool：成功率/重试/超时/P95）：回答"哪个工具在失败"
        "tools": tool_stats.snapshot(),
        # 延迟分位：进程级指标（与数据域无关），看板「缓存命中率」卡片展示 overall P50/P95
        "latency": latency.snapshot(),
        # 档案主文档合并（后台 fire-and-forget）的积压与失败率 + SLO 判定
        "memory": memory_health(),
        # MCP 连接池/熔断 与 中台熔断/契约：回答"这个工具为什么立刻失败"
        "mcp": _safe_call(_mcp_health),
        "platform": _platform_health(),
        # 检索栈：后端 + 降级原因（换 embedding/向量库后第一眼要看这里）
        "rag": _rag_health(),
        # 按 Agent 聚合的轮次/延迟/成本/降级（per-agent 看板）
        "agents": _agent_health(user, visible),
        # 全链路 trace：当前轮次的 trace_id（前端可复制去中台审计对账）
        "trace": trace_mod.trace_status(),
    }


def _safe_call(fn) -> dict:  # noqa: ANN001
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 —— 指标面板不能因为某个组件挂了就整体 500
        return {"error": f"{type(exc).__name__}: {exc}"}


def memory_health() -> dict:
    """L3 档案：合并指标（含积压 SLO）+ 去重索引状态。"""
    from app.db.engine import analysis_dedupe_status
    from app.memory import analysis_merge, merge_stats

    pending = _safe_call(lambda: analysis_merge.list_pending(limit=50))
    stats = merge_stats.snapshot(pending if isinstance(pending, list) else None)
    return {"merge": stats, "dedupe": _safe_call(analysis_dedupe_status)}


def _mcp_health() -> dict:
    from app.mcp_client.client import get_mcp_hub

    return get_mcp_hub().health()


def _platform_health() -> dict:
    from app.tools import platform_contract, platform_tools

    client = platform_tools.get_platform_client()
    return {
        "breaker": _safe_call(client.breaker_state),
        "contract": platform_contract.stats(),
        "capabilities": platform_tools.capability_status(),
    }


def _rag_health() -> dict:
    from app.rag import conflict
    from app.rag.embedding import embedding_status
    from app.rag.vector_store import store_status

    return {"vector_store": _safe_call(store_status),
            "embedding": _safe_call(embedding_status),
            # 口径冲突计数：每次检索都会累计，用来回答「这套机制最近有没有真的在工作」
            "conflicts": _safe_call(conflict.conflict_stats)}


_AGENT_CACHE: dict = {"at": 0.0, "payload": {}}
_AGENT_CACHE_SECONDS = 30.0


def _agent_health(user: AuthUser | None, visible: set[str] | None) -> dict:
    """per-agent 聚合（30s 缓存：看板每 3s 轮询一次，不能每次都全表扫 chat_turn_log）。"""
    now = time.monotonic()
    key = "all" if visible is None else "+".join(sorted(visible))
    cached = _AGENT_CACHE.get("payload") or {}
    if _AGENT_CACHE["at"] and now - _AGENT_CACHE["at"] < _AGENT_CACHE_SECONDS and key in cached:
        return cached[key]
    from app.agents.chat_store import agent_turn_stats

    departments = None if visible is None else sorted(visible)
    with session_scope() as session:
        stats = _safe_call(lambda: agent_turn_stats(session, hours=24, departments=departments))
    cached = dict(cached)
    cached[key] = stats
    _AGENT_CACHE["payload"] = cached
    _AGENT_CACHE["at"] = now
    return stats


def reset_agent_cache() -> None:
    _AGENT_CACHE["at"] = 0.0
    _AGENT_CACHE["payload"] = {}
