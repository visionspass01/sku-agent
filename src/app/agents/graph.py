"""LangGraph 主图装配：Supervisor → 并发采集（collector）→ Skill 子图 → 预案生成 → 校验回路。

设计对应：Supervisor 决定采集子集（ERP 必选，竞品/SOP 可选），collector 节点内并发执行；
Skill 子图（补货测算/库存研判）状态隔离可复用；校验 Agent 检查预案合规与 SOP；
checkpointer 支持同一分析 thread 的多轮追问（复用上一轮采集数据）。
"""

from __future__ import annotations

import asyncio
import operator
import time
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.reasoner import reasoner_node
from app.agents.supervisor import supervisor_node
from app.agents.verifier import verifier_node
from app.agents.workers import erp_worker, market_worker, rag_worker
from app.config import get_settings
from app.skills.inventory_judge import build_inventory_judge_skill
from app.skills.replenishment import build_replenishment_skill

MAX_REVISIONS = 2


def _merge_dict(a: dict | None, b: dict | None) -> dict:
    out = dict(a or {})
    out.update(b or {})
    return out


class InspectionState(TypedDict, total=False):
    sku_id: str
    run_id: str
    as_of: str
    data: Annotated[dict, _merge_dict]
    supervisor: dict
    rag_query: str
    judgment: dict
    replenish: dict
    plan: dict | None
    verification: dict | None
    attempts: int
    status: str
    # 降级是**粘性**的：任何一个节点判定降级（竞品/知识库采集失败、LLM 走了模板）之后，
    # 后续节点不能把它"改回 False"。早先多个节点都写同一字段、后写覆盖先写，
    # 竞品失败会被 reasoner 的正常返回悄悄抹平（P2-6）。
    degraded: Annotated[bool, operator.or_]
    trace: Annotated[list, operator.add]
    # 分析 thread 多轮：mode="followup" 时从 refine 节点进入（跳过采集，复用上一轮数据）
    mode: str
    followup: str


_judge_skill = build_inventory_judge_skill()
_replenish_skill = build_replenishment_skill()

_WORKER_FNS = {"erp": erp_worker, "market": market_worker, "rag": rag_worker}


async def collector_node(state: InspectionState) -> dict:
    """按 Supervisor 的任务列表**并发**采集（erp 必选）。

    为什么不再三路 Send fan-out：LangGraph 的 join 节点要求所有上游分支都被触发，
    于是"按需派发"只能退化成"All fan-out + 节点内跳过"——trace 上看不出路由是否真的生效。
    改成单节点内 gather 之后，Supervisor 未派发的 source（当前只有竞品 market）就真的
    不会发起调用，MCP 调用次数可以直接证明（见 tests/test_graph_routing.py）。

    逐源隔离（P2-6）：每个源单独限时、单独兜异常。早先 `gather` 不带
    `return_exceptions`，一个源抛错就带走整轮——已采到的 ERP 数据也一起丢，
    与"竞品/知识库是可降级依赖"的叙事矛盾。现在：
    - 竞品/知识库失败 → 整轮标 degraded 继续（verifier 会因引用不足给 degraded 结论）；
    - ERP 失败 → 仍然失败（缺库存/销量基准不该硬出结论），但报错要写明是哪个源、为什么。
    """
    planned = set((state.get("supervisor") or {}).get("tasks") or ["erp", "market", "rag"])
    planned &= set(_WORKER_FNS)
    # ERP 必选：merge 节点依赖 ERP 数据；RAG 必选：verifier 要求引用 ≥2 条 SOP chunk。
    # 竞品（market）是唯一可被 Supervisor 真正裁掉的 source。
    planned.update({"erp", "rag"})
    order = [name for name in ("erp", "market", "rag") if name in planned]
    payload = {
        "sku_id": state["sku_id"],
        "run_id": state.get("run_id", ""),
        "as_of": state.get("as_of", ""),
        "rag_query": state.get("rag_query", ""),
        "tasks": order,
    }
    settings = get_settings()
    timeout = float(getattr(settings, "inspect_collect_timeout_seconds", 45.0))

    async def _run_source(name: str):
        """跑一个源：返回 (name, result, error)；错误不影响其它源。"""
        try:
            result = await asyncio.wait_for(_WORKER_FNS[name](payload), timeout=timeout)
            return name, result, None
        except asyncio.TimeoutError:
            return name, None, ("timeout", f"采集超时（>{timeout:.0f}s）")
        except Exception as exc:  # noqa: BLE001 —— 单源失败降级，不拖垮整轮
            return name, None, ("failed", f"{type(exc).__name__}: {exc}")

    results = await asyncio.gather(*(_run_source(name) for name in order))
    merged: dict = {"data": {}, "trace": []}
    failures: dict[str, str] = {}
    for name, result, error in results:
        if error is not None:
            kind, reason = error
            failures[name] = reason
            merged["trace"].append({
                "node": f"{name}_worker",
                "status": kind,
                "ms": 0,
                "detail": reason,
            })
            merged["data"][name] = {"error": reason, "degraded": True}
            continue
        for key, value in (result or {}).items():
            if key == "data":
                merged["data"].update(value or {})
            elif key == "trace":
                merged["trace"].extend(value or [])
            else:
                merged[key] = value
    if failures.get("erp"):
        raise RuntimeError(
            f"ERP 采集失败（{failures['erp']}）：缺少库存/销量基准，无法研判"
            f"（失败源={sorted(failures)}）"
        )
    if failures:
        merged["degraded"] = True
    merged["trace"].append({
        "node": "collector",
        "status": "degraded" if failures else "ok",
        "ms": 0,
        "detail": (f"sources={order}（Supervisor 未派发的 source 不发起调用）"
                   + (f"，失败源={sorted(failures)}（已按降级继续）" if failures else "")),
    })
    return merged


async def merge_node(state: InspectionState) -> dict:
    started = time.monotonic()
    erp = (state.get("data") or {}).get("erp") or {}
    sku = erp.get("sku") or {}
    features = erp.get("features") or {}

    judgment_task = _judge_skill.ainvoke({
        "sku": sku,
        "features": features,
        "sibling_sizes": erp.get("sibling_sizes") or [],
        "activities": erp.get("activities") or [],
        "as_of": state.get("as_of", ""),
    })
    replenish_task = _replenish_skill.ainvoke({
        "sku": sku,
        "features": features,
        "in_transit": erp.get("in_transit") or [],
        "supplier": erp.get("supplier") or {},
    })
    judgment_out, replenish_out = await asyncio.gather(judgment_task, replenish_task)
    return {
        "judgment": judgment_out.get("judgment") or {},
        "replenish": replenish_out.get("replenish") or {},
        "trace": [{
            "node": "merge",
            "status": "ok",
            "ms": int((time.monotonic() - started) * 1000),
            "detail": f"flags={(judgment_out.get('judgment') or {}).get('risk_flags')}",
        }],
    }


def _route_after_verify(state: InspectionState) -> str:
    verdict = state.get("verification") or {}
    if verdict.get("passed"):
        return "finalize"
    if int(state.get("attempts") or 0) < MAX_REVISIONS:
        return "reasoner"
    return "finalize"


async def refine_node(state: InspectionState) -> dict:
    """多轮追问入口：不重新采集，只在已有数据上补一轮研判（复盘"那调拨呢"这类问题）。

    由 `Command(goto="refine", update={"followup": ...})` 触发，因此这里只记录 trace，
    真正的"带上追问指令"由 reasoner 读取 state["followup"] 完成。

    **复审 F3**：追问按"新一轮问题"处理，所以顺手把两样**上一轮的产物**清掉——
    - `attempts` 归零：否则首轮用掉的 revision 预算会让本轮校验失败后直接 finalize，
      追问轮等于 0 次重写机会（首轮还有 1 次）；
    - `verification` 清空：否则 `reasoner._plan_prompt` 会把上一轮的 issues 当作
      "上一轮校验未通过"注入进来，模型会去修上一轮的问题而不是回答这次的追问。
    """
    instruction = (state.get("followup") or "").strip()
    return {
        "attempts": 0,
        "verification": None,
        "trace": [{
            "node": "refine",
            "status": "ok",
            "ms": 0,
            "detail": (f"followup={instruction[:40]}（复用上一轮采集数据，未重新取数；"
                       "重写预算重置、上一轮校验 issues 已清空）"),
        }],
    }


async def finalize_node(state: InspectionState) -> dict:
    verdict = state.get("verification") or {}
    passed = bool(verdict.get("passed"))
    status = "ok" if passed else "degraded"
    if passed and state.get("degraded"):
        status = "degraded"  # 预案本身来自降级模板时保持 degraded 可见
    return {
        "status": status,
        "trace": [{
            "node": "finalize",
            "status": status,
            "ms": 0,
            "detail": f"passed={passed}, attempts={state.get('attempts')}",
        }],
    }


def build_inspection_graph(checkpointer=None):
    graph = StateGraph(InspectionState)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("collector", collector_node)
    graph.add_node("merge", merge_node)
    graph.add_node("refine", refine_node)
    graph.add_node("reasoner", reasoner_node)
    graph.add_node("verifier", verifier_node)
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "supervisor")
    graph.add_edge("supervisor", "collector")
    graph.add_edge("collector", "merge")
    graph.add_edge("merge", "reasoner")
    # 追问路径：refine → reasoner → verifier → finalize（不经过采集节点）
    graph.add_edge("refine", "reasoner")
    graph.add_edge("reasoner", "verifier")
    graph.add_conditional_edges("verifier", _route_after_verify)
    graph.add_edge("finalize", END)
    return graph.compile(name="sku_inspection_graph", checkpointer=checkpointer)
