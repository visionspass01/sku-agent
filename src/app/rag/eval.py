"""RAG 检索质量离线评测（Recall@K / MRR / nDCG@5 / 无答案）。

为什么要有：`scripts/eval_accuracy.py` 评的是"筛查/研判结论"，回答不了"检索召回好不好"。
换 embedding、换融合方式、加重排前后，必须有一套固定题集做对比，否则全是主观感觉。

设计要点：
- 正样本 `data/eval/rag_golden.jsonl`：`{id, query, expected_doc_ids[], department?, type?}`，
  期望到 **doc 级**（chunk 级太脆，同一结论可能被切到相邻块）；
- 负样本 `data/eval/rag_negatives.jsonl`：`{id, query, expect_no_answer: true}`，
  知识库里确实没有答案 —— 用来衡量「会不会硬塞一条不相关的 SOP」。
  没有负样本的检索评测只能证明"召回得到"，证明不了"什么时候该说不知道"。
- 指标：Recall@1/3/5/10、MRR、nDCG@5（单相关文档，gain=1，按 log2(rank+1) 折损）、
  `no_answer_rate`（负样本上正确弃答的比例）。
- 全离线：默认走本地哈希向量 + BM25（`VECTOR_STORE=memory`），不依赖网络。
"""

from __future__ import annotations

import json
import math
import pathlib
from dataclasses import dataclass, field

EVAL_DIR = pathlib.Path(__file__).resolve().parents[3] / "data" / "eval"  # src/app/rag/eval.py -> 仓库根
GOLDEN_FILE = EVAL_DIR / "rag_golden.jsonl"
NEGATIVES_FILE = EVAL_DIR / "rag_negatives.jsonl"
CONFLICTS_FILE = EVAL_DIR / "rag_conflicts.jsonl"


@dataclass
class QueryResult:
    query_id: str
    query: str
    expected: list[str]
    ranked_doc_ids: list[str]
    hit_rank: int | None  # 1-based，None=未命中
    scores: list[float] = field(default_factory=list)
    no_answer: bool = False
    expect_no_answer: bool = False
    department: str = ""
    type: str = ""

    @property
    def ok(self) -> bool:
        """负样本看"是否正确弃答"，正样本看"是否命中"。"""
        if self.expect_no_answer:
            return self.no_answer
        return self.hit_rank is not None

    def to_dict(self) -> dict:
        return {
            "id": self.query_id,
            "query": self.query,
            "expected": self.expected,
            "ranked_doc_ids": self.ranked_doc_ids,
            "hit_rank": self.hit_rank,
            "no_answer": self.no_answer,
            "expect_no_answer": self.expect_no_answer,
            "department": self.department,
            "type": self.type,
            "ok": self.ok,
            "top_score": round(self.scores[0], 6) if self.scores else 0.0,
        }


def load_golden(path: pathlib.Path | None = None) -> list[dict]:
    file = path or GOLDEN_FILE
    rows: list[dict] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))
    return rows


def load_negatives(path: pathlib.Path | None = None) -> list[dict]:
    file = path or NEGATIVES_FILE
    if not file.exists():
        return []
    rows: list[dict] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        item = json.loads(line)
        item["expect_no_answer"] = True
        item.setdefault("expected_doc_ids", [])
        rows.append(item)
    return rows


def _doc_of(chunk_id: str) -> str:
    return (chunk_id or "").split("#")[0]


def evaluate(
    rows: list[dict],
    search,
    topk: int = 5,
    rewrite=None,
) -> dict:
    """跑一遍评测。

    search(query, topk, rewritten=...) -> list[(chunk_id, doc_id, score)]，
    由调用方注入检索实现（生产用 HybridRetriever/pipeline，测试可注入固定桩）。
    """
    results: list[QueryResult] = []
    for row in rows:
        query = row["query"]
        expected = list(row.get("expected_doc_ids") or [])
        expect_no_answer = bool(row.get("expect_no_answer"))
        rewritten = rewrite(query) if rewrite else None
        hits = search(query, topk, rewritten=rewritten) if rewrite else search(query, topk)
        if isinstance(hits, dict):                       # pipeline 返回 SearchResult.to_dict()
            doc_ids = [h["doc_id"] for h in hits.get("hits") or []]
            scores = [float(h.get("score") or 0.0) for h in hits.get("hits") or []]
            no_answer = bool(hits.get("no_answer"))
        else:
            doc_ids = [doc for _chunk, doc, _score in hits]
            scores = [float(score) for _chunk, _doc, score in hits]
            no_answer = not hits
        hit_rank = None
        for idx, doc in enumerate(doc_ids, start=1):
            if doc in expected:
                hit_rank = idx
                break
        results.append(QueryResult(
            query_id=row.get("id") or query[:12], query=query,
            expected=expected, ranked_doc_ids=doc_ids, hit_rank=hit_rank, scores=scores,
            no_answer=no_answer, expect_no_answer=expect_no_answer,
            department=str(row.get("department") or ""), type=str(row.get("type") or ""),
        ))

    positives = [r for r in results if not r.expect_no_answer]
    negatives = [r for r in results if r.expect_no_answer]
    total = max(1, len(positives))
    recall_at = {
        f"recall@{k}": round(sum(1 for r in positives if r.hit_rank and r.hit_rank <= k) / total, 4)
        for k in (1, 3, 5, 10)
    }
    mrr = round(sum(1.0 / r.hit_rank for r in positives if r.hit_rank) / total, 4)
    ndcg = 0.0
    for r in positives:
        if r.hit_rank:
            ndcg += 1.0 / math.log2(r.hit_rank + 1)
    ndcg = round(ndcg / total, 4)
    no_answer_rate = (round(sum(1 for r in negatives if r.no_answer) / len(negatives), 4)
                      if negatives else None)
    return {
        "count": len(results),
        "positives": len(positives),
        "negatives": len(negatives),
        "topk": topk,
        "recall_at": recall_at,
        "mrr": mrr,
        "ndcg@5": ndcg,
        # 负样本上"正确弃答"的比例；没有负样本时为 None（不要假装是 1.0）
        "no_answer_rate": no_answer_rate,
        "by_department": _breakdown(positives, key="department"),
        "by_type": _breakdown(positives, key="type"),
        "missed": [
            {"id": r.query_id, "query": r.query, "expected": r.expected, "got": r.ranked_doc_ids}
            for r in positives if not r.hit_rank
        ],
        "false_answers": [
            {"id": r.query_id, "query": r.query, "got": r.ranked_doc_ids[:3]}
            for r in negatives if not r.ok
        ],
        "rows": [r.to_dict() for r in results],
    }


def _breakdown(rows: list[QueryResult], *, key: str) -> dict:
    """按维度拆指标：总分会被一两类题拉高/拉低，分组才看得出「哪类问题召不回」。"""
    groups: dict[str, list[QueryResult]] = {}
    for row in rows:
        label = getattr(row, key) or "未标注"
        groups.setdefault(label, []).append(row)
    out: dict[str, dict] = {}
    for label, items in sorted(groups.items()):
        n = max(1, len(items))
        out[label] = {
            "count": len(items),
            "recall@1": round(sum(1 for r in items if r.hit_rank == 1) / n, 4),
            "recall@5": round(sum(1 for r in items if r.hit_rank and r.hit_rank <= 5) / n, 4),
            "mrr": round(sum(1.0 / r.hit_rank for r in items if r.hit_rank) / n, 4),
        }
    return out


def eval_metric_summary(recall_at_1: float) -> str:
    """给测试/脚本用的粗分档（避免到处写魔法数字阈值）。"""
    if recall_at_1 >= 0.8:
        return "ok"
    if recall_at_1 >= 0.5:
        return "weak"
    return "bad"


def retriever_search(server_session, topk: int, *, mode: str = "rrf", rerank_backend: str = "none"):
    """用当前进程内检索器实现（可切换融合方式与重排后端做消融）。"""
    from app.rag.hybrid import get_retriever, rewrite_query

    retriever = get_retriever(server_session)

    def _search(query: str, k: int, rewritten: str | None = None):
        rewritten = rewritten or rewrite_query(query)
        if rerank_backend == "none":
            cands, _diag = retriever.candidates(query, pool=k, rewritten=rewritten, mode=mode)
            return [(c["chunk_id"], c["doc_id"], c["score"]) for c in cands[:k]]
        import asyncio

        from app.rag import rerank as rerank_mod

        cands, _diag = retriever.candidates(query, pool=max(k, 30), rewritten=rewritten, mode=mode)
        result = asyncio.run(rerank_mod.rerank(query, cands, topk=k, backend=rerank_backend))
        if result.degraded:
            _search.degraded = getattr(_search, "degraded", 0) + 1
        return [(c["chunk_id"], c["doc_id"], c.get("rerank_score") or c["score"])
                for c in result.hits]

    _search.degraded = 0

    return _search


def run_eval(topk: int = 5, golden: list[dict] | None = None,
             negatives: list[dict] | None = None, *, mode: str = "rrf",
             rerank_backend: str = "none") -> dict:
    """便捷入口：从当前配置的数据库装载检索器并评测（不调网络，重排后端默认关闭）。"""
    from app.db.engine import session_scope
    from app.rag.hybrid import rewrite_query

    rows = list(golden if golden is not None else load_golden())
    rows.extend(negatives if negatives is not None else load_negatives())
    with session_scope() as session:
        search = retriever_search(session, topk, mode=mode, rerank_backend=rerank_backend)
        report = evaluate(rows, search, topk=topk, rewrite=rewrite_query)
        report["rerank_degraded"] = getattr(search, "degraded", 0)
    report["mode"] = mode
    report["rerank_backend"] = rerank_backend
    return report


# ---------------------------------------------------------------- 口径冲突评测
def load_conflicts(path: pathlib.Path | None = None) -> list[dict]:
    """口径冲突题集：`{id, query, department?, expect_status, expect_conflict_keys, expect_winners}`。

    正例给 `expect_status/expect_conflict_keys/expect_winners`；**过触发负例**用
    `expect_no_conflict: true`（只要求"没有报出冲突"，不写死状态）。
    为什么单独一套：召回率衡量「找得到」，冲突题衡量「找到两份互相矛盾的制度后，
    会不会挑对一条」，而负例衡量「不该报的时候有没有闭嘴」——三者会互相拉扯。
    """
    file = path or CONFLICTS_FILE
    if not file.exists():
        return []
    rows: list[dict] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))
    return rows


def evaluate_conflicts(rows: list[dict], payloads: list[dict]) -> dict:
    """对比「检测到的冲突」与题目期望：状态、冲突 key、胜出口径是否都对。"""
    from app.common.conflicts import normalize_value

    cases: list[dict] = []
    for row, payload in zip(rows, payloads):
        payload = payload or {}
        found = {c.get("key"): c for c in payload.get("conflicts") or []}
        misses: list[str] = []
        want_status = row.get("expect_status")
        status = str(payload.get("conflict_check") or "")
        if want_status and status != want_status:
            misses.append(f"状态期望 {want_status}，实际 {status}")
        if row.get("expect_no_conflict") or row.get("expect_no_winner"):
            # 过触发负例 / 不裁决负例：不写死具体状态（none / unavailable 都算"没报"），
            # 只要没有冲突条目。写死状态会随语料微调而脆。
            if found:
                misses.append(f"不该报冲突，实际报了 {', '.join(sorted(found))}")
            if status == "ok":
                misses.append("状态不该是 ok")
        notice = str(payload.get("scope_notice") or "")
        want_notice = row.get("expect_notice_contains")
        if want_notice and want_notice not in notice:
            misses.append(f"范围说明应含「{want_notice}」，实际：{notice or '（空）'}")
        if row.get("expect_no_notice") and notice:
            misses.append(f"不该有范围说明，实际：{notice}")
        overrides = payload.get("department_overrides") or []
        for key, docs in (row.get("expect_override_docs") or {}).items():
            got = [entry.get("doc_id") for entry in overrides if entry.get("key") == key]
            if sorted(got) != sorted(docs):
                misses.append(f"{key}: 部门例外期望 {docs}，实际 {got}")
        if row.get("expect_no_override") and overrides:
            misses.append(f"不该有部门例外，实际 {[o.get('doc_id') for o in overrides]}")
        if "expect_uncovered" in row:
            got_uncovered = sorted(payload.get("uncovered_departments") or [])
            want_uncovered = sorted(row["expect_uncovered"])
            if got_uncovered != want_uncovered:
                misses.append(f"未覆盖部门期望 {want_uncovered}，实际 {got_uncovered}")
        for key in row.get("expect_conflict_keys") or []:
            if key not in found:
                misses.append(f"未检出冲突：{key}")
        for key, want in (row.get("expect_winners") or {}).items():
            got = found.get(key)
            if not got:
                misses.append(f"{key}: 缺冲突记录，无法确认胜出口径")
                continue
            winner = got.get("winner") or {}
            if want.get("doc_id") and winner.get("doc_id") != want["doc_id"]:
                misses.append(f"{key}: 期望 {want['doc_id']}，实际 {winner.get('doc_id')}")
            if want.get("value") is not None and \
                    normalize_value(winner.get("value")) != normalize_value(want["value"]):
                misses.append(f"{key}: 期望取值 {want['value']}，实际 {winner.get('display')}")
        # 出现了题目没预期的冲突：可能语料元数据填错了（如两份 SOP 对同一口径写了不同值）
        unexpected = sorted(set(found) - set(row.get("expect_conflict_keys") or []))
        if unexpected:
            misses.append(f"额外冲突：{', '.join(unexpected)}")
        cases.append({
            "id": row.get("id") or row["query"][:12], "query": row["query"],
            "status": status, "conflicts": sorted(found),
            "overrides": sorted({o.get("doc_id") for o in overrides}),
            "ok": not misses, "misses": misses,
        })

    total = max(1, len(cases))
    passed = sum(1 for c in cases if c["ok"])
    return {
        "count": len(cases), "passed": passed, "pass_rate": round(passed / total, 4),
        "ok": passed == len(cases), "cases": cases,
    }


def run_conflict_eval(topk: int = 5) -> dict:
    """跑冲突题集（走生产同一条 `search_kb` 链路，含缓存/重排/无答案判定）。

    `department` 为空的行按**管理员**视角跑，并同样按问题部门面收敛——这样第 3 条
    （admin 问男装）才会和生产路径一致地收敛到「男装 + 通用」，而不是全量召回。
    """
    import asyncio

    from app.rag.facets import query_departments
    from app.rag.pipeline import search_kb
    from app.security.scope import kb_department_filter

    rows = load_conflicts()

    async def _run() -> list[dict]:
        out: list[dict] = []
        for row in rows:
            dept = row.get("department")
            if dept:
                filters: dict | None = {"department": [dept, "通用"]}
            else:
                filters = kb_department_filter(None, query_departments(row["query"])) or None
            result = await search_kb(row["query"], filters=filters, topk=topk, use_cache=False)
            out.append(result.to_dict())
        return out

    payloads = asyncio.run(_run()) if rows else []
    return evaluate_conflicts(rows, payloads)
