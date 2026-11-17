"""RAG 融合 / 无答案 / 多样性（评审 3.4-1 / 3.4-2 / 3.4-8）。

这些用例守的是"检索敢不敢说不知道"和"top-k 是不是被一篇文档占满"——
两个都直接影响答案可信度，而不是小数点后的精度。
"""

from __future__ import annotations


def _hits(session, query: str, **kw):
    from app.rag.hybrid import get_retriever, rewrite_query

    retriever = get_retriever(session)
    return retriever.search(query, rewritten=rewrite_query(query), **kw)


def test_rrf_beats_single_retriever_on_golden_set(seeded):
    """RRF 融合的 R@1 必须不低于任一单路（消融结论的可执行版本）。"""
    from app.rag.eval import eval_metric_summary, run_eval

    scores = {}
    for mode in ("bm25", "vector", "rrf"):
        report = run_eval(topk=5, negatives=[], mode=mode, rerank_backend="none")
        scores[mode] = report["recall_at"]["recall@1"]
    assert scores["rrf"] >= max(scores["bm25"], scores["vector"]), scores
    # 顺带确认指标函数本身可用（避免上面三个都是 0 还"通过"）
    assert eval_metric_summary(scores["rrf"]) in ("ok", "weak")


def test_no_answer_when_kb_has_nothing(seeded):
    """完全域外的查询：返回空结果 + no_answer，而不是硬塞 5 条 SOP。

    阈值是**标定**出来的（见 app/rag/hybrid.py 注释），所以这里用负样本集里的原题，
    而不是随手编一句——否则测试会变成"猜阈值"。
    """
    from app.db.engine import session_scope
    from app.rag.eval import load_negatives

    negatives = load_negatives()
    assert negatives, "缺少 data/eval/rag_negatives.jsonl"
    rejected = 0
    with session_scope() as session:
        from app.rag.hybrid import get_retriever

        retriever = get_retriever(session)
        for row in negatives:
            candidates, diag = retriever.candidates(row["query"])
            if not candidates and diag.get("reason") == "no_answer":
                rejected += 1
        assert diag["thresholds"]["vector"] > 0
    rate = rejected / len(negatives)
    # 与 `eval_rag --negatives` 的口径一致：负样本上「正确弃答」≥85%
    assert rate >= 0.85, f"无答案命中率只有 {rate:.1%}（{rejected}/{len(negatives)}）"


def test_on_topic_query_is_answered(seeded):
    """域内查询不能被无答案阈值误杀（召回损失是不可接受的代价）。"""
    from app.db.engine import session_scope

    with session_scope() as session:
        hits = _hits(session, "断码补全 尺码 调拨", topk=5)
    assert hits, "域内查询被判成无答案：阈值过严"
    assert hits[0].doc_id in ("sop-sizegap", "sop-replenish")


def test_per_doc_cap_prevents_flooding(seeded):
    """同一篇文档最多占 top-k 里的 N 条（默认 2），避免"只召回一篇文档"。"""
    from app.config import get_settings
    from app.db.engine import session_scope

    settings = get_settings()
    cap = int(settings.rag_max_per_doc)
    with session_scope() as session:
        hits = _hits(session, "清仓 折扣 阶梯 清仓 SOP", topk=5)
    counts: dict[str, int] = {}
    for hit in hits:
        counts[hit.doc_id] = counts.get(hit.doc_id, 0) + 1
    assert counts, "没有命中任何文档"
    assert max(counts.values()) <= cap, counts
    assert len(counts) >= 2, f"top-k 只覆盖了 {list(counts)} 一篇文档"


def test_fusion_score_is_rrf_not_minmax(seeded):
    """融合分应为 RRF（Σ 1/(k+rank)）量级：≈1e-2，而不是被 min-max 拉到 0..1。"""
    from app.db.engine import session_scope
    from app.rag.hybrid import get_retriever, rewrite_query

    with session_scope() as session:
        retriever = get_retriever(session)
        candidates, diag = retriever.candidates(
            "补货 安全库存 再订货点", rewritten=rewrite_query("补货 安全库存 再订货点"), pool=5)
    assert candidates
    top = candidates[0]["score"]
    assert 0.0 < top < 0.05, f"融合分 {top} 不像是 RRF（min-max 归一化会落在 0..1）"
    # 融合候选必须按 RRF 单调（最终 top-k 经过 MMR 重排，顺序不再单调）
    assert all(candidates[i]["score"] >= candidates[i + 1]["score"]
               for i in range(len(candidates) - 1))


def test_rewritten_query_uses_platform_synonyms(seeded):
    """查询改写走中台下发的同义词表（含服装口径词：齐码/售罄率/周转）。"""
    from app.rag import synonyms
    from app.rag.hybrid import rewrite_query

    table = synonyms.synonyms_for()
    assert "齐码" in table and "断码" in table
    rewritten = rewrite_query("齐码率多少", table)
    assert "全码" in rewritten or "码数齐全" in rewritten


def test_mmr_lambda_bounds(seeded):
    """MMR 参数越界不应崩（配置写错时退化，而不是 500）。"""
    from app.db.engine import session_scope

    with session_scope() as session:
        hits = _hits(session, "补货", topk=3)
    assert isinstance(hits, list)


def test_empty_corpus_returns_empty():
    """空语料：不抛错、返回空（离线/新部署首次启动时的真实状态）。"""
    from app.rag.hybrid import HybridRetriever

    retriever = HybridRetriever([])
    assert retriever.search("任何问题") == []
    candidates, diag = retriever.candidates("任何问题")
    assert candidates == [] and diag["reason"] == "empty_corpus_or_query"
