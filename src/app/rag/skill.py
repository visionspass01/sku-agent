"""RAG Skill 子图（LangGraph SubGraph，状态隔离、可插拔复用）。

链路：查询改写 → 混合召回（RRF）→ **重排** → 多样性 → TopK 注入 Prompt → 引用溯源。

与旧实现的差别（评审 3.4-7 / 3.4-11）：
- 之前 `rerank` 节点只是把已有 score 排序一遍（**空转**）——名字叫重排、实际没重排。
  现在它真的调 `app.rag.pipeline.search_kb()`，由 `RAG_RERANK_BACKEND` 决定后端，
  超时/失败会 `rerank_degraded=True` 传下去，不假装排过。
- 缓存与无答案判定都在 pipeline 里（唯一实现），子图只负责编排。
"""

from __future__ import annotations

import datetime as dt
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.rag.hybrid import rewrite_query
from app.rag.pipeline import search_kb
from app.rag.synonyms import synonyms_for


class RagState(TypedDict, total=False):
    query: str
    filters: dict
    as_of: str  # YYYY-MM-DD
    topk: int
    backend: str
    rewritten: str
    hits: list[dict]
    context: str
    citations: list[dict]
    no_answer: bool
    rerank: dict
    # 口径冲突：批量巡检的 reasoner 拿到的 context 里已经带 `## 冲突提示`，
    # 这里同时把结构化结果放回 state，供审计/前端复用。
    conflicts: list[dict]
    conflict_check: str
    conflict_diagnostics: dict
    scope_notice: str


def _node_rewrite(state: RagState) -> dict:
    return {"rewritten": rewrite_query(state.get("query", ""), synonyms_for())}


async def _node_search(state: RagState) -> dict:
    as_of = dt.date.fromisoformat(state["as_of"]) if state.get("as_of") else None
    result = await search_kb(
        state.get("query", ""),
        filters=state.get("filters") or {},
        as_of=as_of,
        topk=int(state.get("topk", 5)),
        backend=state.get("backend") or None,
    )
    return {"hits": [h.to_dict() for h in result.hits], "no_answer": result.no_answer,
            "rerank": result.rerank, "citations": result.citations,
            "context": result.context(), "conflicts": result.conflicts,
            "conflict_check": result.conflict_check,
            "conflict_diagnostics": result.conflict_diagnostics,
            "scope_notice": result.scope_notice}


def _node_package(state: RagState) -> dict:
    if state.get("no_answer"):
        return {"context": "", "citations": []}
    return {}


def build_rag_skill():
    """子图：rewrite → search（含召回/重排/多样性）→ package。"""
    graph = StateGraph(RagState)
    graph.add_node("rewrite", _node_rewrite)
    graph.add_node("search", _node_search)
    graph.add_node("package", _node_package)
    graph.add_edge(START, "rewrite")
    graph.add_edge("rewrite", "search")
    graph.add_edge("search", "package")
    graph.add_edge("package", END)
    return graph.compile(name="rag_skill")
