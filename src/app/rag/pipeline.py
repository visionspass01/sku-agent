"""RAG 统一入口：一次检索 = 改写 → 混合召回（RRF）→ 重排 → 多样性 → 引用。

为什么要有这层（评审 3.4-11）：
早先 `/api/rag/search` 与 `rag/skill.py` 各写一套缓存 + 各调一次检索器，
同一轮里可能把同样的检索算两遍；加了重排之后更会分叉（一个排一个不排）。
现在只有 `search_kb()` 一条路径：缓存、重排预算、无答案判定、引用打包都在这里，
调用方只关心 `SearchResult`。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging

from app.cache.ttl_cache import get_ttl_cache
from app.config import get_settings
from app.db.engine import session_scope
from app.observability import latency
from app.rag import conflict
from app.rag import rerank as rerank_mod
from app.rag.facets import is_comparative, query_departments
from app.rag.hybrid import Hit, get_retriever, hit_from_candidate, rewrite_query
from app.rag.synonyms import synonyms_for

logger = logging.getLogger("sku-agent.rag")


@dataclasses.dataclass
class SearchResult:
    query: str
    rewritten: str
    hits: list[Hit]
    no_answer: bool
    rerank: dict
    diagnostics: dict
    # 口径冲突（评审 2.4）：冲突数据与「检查到什么程度」必须一起回传，
    # 否则调用方会把 unavailable（语料没填 key_facts）误读成"检查过、没冲突"。
    conflicts: list[dict] = dataclasses.field(default_factory=list)
    conflict_check: str = "unavailable"
    conflict_note: str = ""
    # 冲突自述：比对了哪些口径、哪些只有单一来源、池内被判为无关的条数
    # （模型据此区分"比对了没有分歧"与"没做比对"，见 chat_system.md）
    conflict_diagnostics: dict = dataclasses.field(default_factory=dict)
    # 部门例外：问题没有部门面时被挡在裁决外的部门口径，只能写成"若为 X 部门则 Y"（复审 P1）
    department_overrides: list[dict] = dataclasses.field(default_factory=list)
    # 检索范围说明：问的部门不在账号可见范围 / 多部门比较时必须先告知用户（规则 C3）
    scope_notice: str = ""
    # 未单独规定口径的部门（按通用口径执行）——避免"该部门没有规定"的误解（复审 P3）
    uncovered_departments: list[str] = dataclasses.field(default_factory=list)

    @property
    def is_primary(self) -> dict[str, bool]:
        return self.diagnostics.get("is_primary") or {}

    @property
    def citations(self) -> list[dict]:
        primary = self.is_primary
        return [
            {
                "chunk_id": h.chunk_id, "doc_id": h.doc_id,
                "title": h.meta.get("title", ""), "section": h.meta.get("section", ""),
                "department": h.meta.get("department", ""),
                # 权威度与「这条是不是胜出口径」一起给出去：
                # 前端可以直接把"参考了历史案例"和"按部门细则执行"区分开显示
                "authority": h.meta.get("authority", ""),
                "version": h.meta.get("version", ""),
                "is_primary": bool(primary.get(h.chunk_id, True)),
                "snippet": (h.meta.get("body") or h.text)[:120],
                "score": round(h.score, 6),
                "rerank_score": round(h.rerank_score, 4),
                "rerank_backend": h.rerank_backend,
            }
            for h in self.hits
        ]

    def context(self, limit: int = 5) -> str:
        lines: list[str] = []
        for h in self.hits[:limit]:
            body = h.meta.get("body") or h.text
            tag = ""
            if not self.is_primary.get(h.chunk_id, True):
                tag = " · 次要口径（与更权威的规定冲突，见冲突提示）"
            lines.append(f"[{h.chunk_id}] {h.meta.get('title', '')} · "
                         f"{h.meta.get('section', '')}{tag}\n{body}")
        text = "\n\n".join(lines)
        note = conflict.note_text(self.conflicts, self.department_overrides, self.scope_notice)
        return f"{note}\n\n{text}" if note else text

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "rewritten": self.rewritten,
            "count": len(self.hits),
            "no_answer": self.no_answer,
            "rerank": self.rerank,
            "diagnostics": self.diagnostics,
            "hits": [h.to_dict() for h in self.hits],
            "citations": self.citations,
            "conflicts": self.conflicts,
            "conflict_check": self.conflict_check,
            "conflict_note": self.conflict_note,
            "conflict_diagnostics": self.conflict_diagnostics,
            "department_overrides": self.department_overrides,
            "scope_notice": self.scope_notice,
            "uncovered_departments": self.uncovered_departments,
        }


def _cache_key(query: str, filters: dict | None, as_of: dt.date | None, topk: int,
               backend: str) -> str:
    rendered = sorted((filters or {}).items(), key=lambda kv: kv[0])
    # v9：比较意图（哪个部门/各部门/一样吗）不再单方胜出 + 未覆盖部门说明。
    # 语义变了就必须换 key，否则部署瞬间命中的旧条目会带着"单方胜出"的答案继续服务。
    return f"rag:v9:{backend}:{topk}:{as_of}:{rendered}:{query}"


async def search_kb(query: str, *, filters: dict | None = None, as_of: dt.date | None = None,
                    topk: int = 5, backend: str | None = None, use_cache: bool = True,
                    rewriter=rewrite_query) -> SearchResult:
    """统一检索入口（异步：重排可能是模型调用）。"""
    settings = get_settings()
    query = (query or "").strip()
    as_of = as_of or dt.date.today()
    backend = (backend or rerank_mod.active_backend()).lower()
    rewritten = rewriter(query, synonyms_for()) if rewriter else query

    cache = get_ttl_cache()
    key = _cache_key(f"{query}|{rewritten}", filters, as_of, topk, backend)
    if use_cache:
        cached = await cache.aget(key)
        if cached:
            cached = dict(cached)
            cached["diagnostics"] = {**(cached.get("diagnostics") or {}), "cached": True}
            return _from_cache(cached)

    started = __import__("time").perf_counter()
    candidates, diag = await __import__("asyncio").to_thread(
        _recall, query, filters, as_of, rewritten, topk,
    )
    if not candidates:
        result = SearchResult(query=query, rewritten=rewritten, hits=[],
                              no_answer=diag.get("reason") == "no_answer",
                              rerank={"backend": backend, "degraded": False, "reason": ""},
                              diagnostics={**diag, "cached": False,
                                           "elapsed_ms": _ms(started)})
        if use_cache and not result.no_answer:
            pass          # 空结果不缓存：语料更新后应立刻能命中
        elif use_cache:
            await cache.aset(key, result.to_dict(), int(settings.cache_ttl_rag))
        return result

    reranked = await rerank_mod.rerank(query, candidates, topk=topk, backend=backend)
    hits = [hit_from_candidate(item, backend=reranked.backend,
                               degraded=reranked.degraded) for item in reranked.hits]
    # 冲突检测跑在**候选池**上（不是 top-k）：落败文档的片段经常排在第 6~20 名，
    # 只在 top-k 上比对就会把"通用 60 天 vs 女装 45 天"这种真冲突整条丢掉（复审 P1-1）。
    # 相关性由 detect() 用 top_k + 触发词做闸门，避免把无关口径的冲突当噪声塞给模型。
    # 池内没有任何口径事实时（语料没声明 key_facts / 大语料里没标注）直接跳过，
    # 不为此构造 30 个 Hit —— 规模基准里这条路径是纯开销。
    if any((item.get("meta") or {}).get("key_facts") for item in candidates):
        pool_hits = [hit_from_candidate(item) for item in candidates]
        # query 传原话（排除词只认它）、expanded 传改写结果（只用于正向触发词匹配）；
        # facets 由 filters 派生（部门账号/批量巡检天然带部门面，管理员问通用问题才是 None）；
        # query_facets 是**问句部门面**——与账号可见面不一致时要给范围说明（规则 C3）；
        # comparative 是"跨部门比较"意图（哪个部门/各部门/一样吗），没有点名部门也要认（复审 P2-1）
        query_facets = query_departments(query)
        report = conflict.detect(pool_hits, top_hits=hits, query=query,
                                 expanded=rewritten, filters=filters,
                                 query_facets=query_facets,
                                 comparative=is_comparative(query))
    else:
        # 池内没有任何口径事实：跳过检测，但**必须计数**（否则 health 看不出"有多少次没比对"）
        conflict.mark_skipped("no_key_facts_in_pool")
        report = {"status": conflict.STATUS_UNAVAILABLE, "conflicts": [],
                  "is_primary": {}, "compared_keys": [], "single_source_keys": [],
                  "suppressed": 0, "skipped_reason": "no_key_facts_in_pool",
                  "department_overrides": [], "facets": {"departments": [], "asked": [],
                                                        "missing": [], "comparative": False},
                  "uncovered_departments": [], "scope_notice": "",
                  "note": "本次召回的候选片段均未声明可比对的口径，冲突检查未启用"}
    result = SearchResult(
        query=query, rewritten=rewritten, hits=hits, no_answer=False,
        rerank=reranked.meta(),
        diagnostics={**diag, "cached": False, "elapsed_ms": _ms(started),
                     "is_primary": report["is_primary"]},
        conflicts=report["conflicts"], conflict_check=report["status"],
        conflict_note=report["note"],
        conflict_diagnostics={
            "compared_keys": report.get("compared_keys") or [],
            "single_source_keys": report.get("single_source_keys") or [],
            "suppressed": int(report.get("suppressed") or 0),
            "candidates": len(candidates),
            "skipped_reason": report.get("skipped_reason") or "",
            "facets": report.get("facets") or {"departments": [], "asked": [], "missing": []},
            "overrides": len(report.get("department_overrides") or []),
            "uncovered_departments": report.get("uncovered_departments") or [],
        },
        department_overrides=report.get("department_overrides") or [],
        scope_notice=report.get("scope_notice") or "",
        uncovered_departments=list(report.get("uncovered_departments") or []),
    )
    latency.record("rag_total", (__import__("time").perf_counter() - started) * 1000)
    if use_cache and hits:
        await cache.aset(key, result.to_dict(), int(settings.cache_ttl_rag))
    return result


def search_kb_sync(query: str, *, filters: dict | None = None, as_of: dt.date | None = None,
                   topk: int = 5, backend: str | None = None) -> SearchResult:
    """同步入口（脚本/离线评测/同步工具调用用）。"""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(search_kb(query, filters=filters, as_of=as_of, topk=topk,
                                     backend=backend or "none"))
    raise RuntimeError("事件循环中请用 await search_kb(...)")


def _recall(query: str, filters: dict | None, as_of: dt.date | None,
            rewritten: str, topk: int) -> tuple[list[dict], dict]:
    settings = get_settings()
    with session_scope() as session:
        retriever = get_retriever(session)
        pool = max(int(getattr(settings, "rag_rerank_max_candidates", 30)), topk)
        return retriever.candidates(query, filters=filters, as_of=as_of,
                                    rewritten=rewritten, pool=pool,
                                    rrf_k=int(getattr(settings, "rag_rrf_k", 60)))


def _from_cache(payload: dict) -> SearchResult:
    hits = []
    for item in payload.get("hits") or []:
        hits.append(Hit(
            chunk_id=item["chunk_id"], doc_id=item["doc_id"], text=item["text"],
            score=float(item.get("score") or 0.0),
            vector_score=float(item.get("vector_score") or 0.0),
            keyword_score=float(item.get("keyword_score") or 0.0),
            meta=item.get("meta") or {},
            rerank_score=float(item.get("rerank_score") or 0.0),
            rerank_backend=str(item.get("rerank_backend") or ""),
            rerank_degraded=bool(item.get("rerank_degraded")),
        ))
    return SearchResult(
        query=str(payload.get("query") or ""), rewritten=str(payload.get("rewritten") or ""),
        hits=hits, no_answer=bool(payload.get("no_answer")),
        rerank=dict(payload.get("rerank") or {}),
        diagnostics=dict(payload.get("diagnostics") or {}),
        conflicts=list(payload.get("conflicts") or []),
        conflict_check=str(payload.get("conflict_check") or conflict.STATUS_UNAVAILABLE),
        conflict_note=str(payload.get("conflict_note") or ""),
        conflict_diagnostics=dict(payload.get("conflict_diagnostics") or {}),
        department_overrides=list(payload.get("department_overrides") or []),
        scope_notice=str(payload.get("scope_notice") or ""),
    )


def _ms(started: float) -> int:
    return int((__import__("time").perf_counter() - started) * 1000)


def status() -> dict:
    from app.rag import vector_store
    from app.rag.embedding import embedding_status

    backend = rerank_mod.active_backend()
    ok, reason = rerank_mod.is_available(backend)
    return {
        "rerank": {"backend": backend, "available": ok, "reason": reason},
        "store": vector_store.store_status(),
        "embedding": embedding_status(),
        "synonyms": {"source": synonyms_for(source_only=True)},
    }
