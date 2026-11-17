"""RAG 检索评测：指标数学 + 真实语料基线（离线）。

阈值说明：语料只有 12 篇 SOP / 58 块，且题面与 SOP 关键词重叠度高，doc 级 Recall@5
容易到 100%——真正有区分度的是 Recall@1 / MRR。这里锁定的是"改动前后回归"：
换 embedding、换向量库、改融合权重后必须跑这套题集，数字不能掉。
"""

from __future__ import annotations

import json


def test_golden_set_is_valid():
    from app.rag.eval import load_golden

    rows = load_golden()
    assert len(rows) >= 30, len(rows)
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "题目 id 不能重复"
    for row in rows:
        assert row["query"].strip()
        assert row["expected_doc_ids"], f"{row['id']} 缺 expected_doc_ids"
    # 期望的 doc 必须真实存在于知识库，否则评测本身是错的
    from app.config import KB_DIR
    from app.rag.loader import load_documents

    disk_ids = {d.doc_id for d in load_documents(KB_DIR)}
    for row in rows:
        assert set(row["expected_doc_ids"]) <= disk_ids, row["id"]


def test_metrics_math_with_stub_search():
    from app.rag.eval import evaluate

    rows = [
        {"id": "q1", "query": "q1", "expected_doc_ids": ["doc-a"]},
        {"id": "q2", "query": "q2", "expected_doc_ids": ["doc-b"]},
        {"id": "q3", "query": "q3", "expected_doc_ids": ["doc-c"]},
    ]

    def search(query, topk, rewritten=None):
        if query == "q1":      # 第一条就命中
            return [("doc-a#0", "doc-a", 0.9), ("doc-b#0", "doc-b", 0.5)]
        if query == "q2":      # 第二名命中
            return [("doc-x#0", "doc-x", 0.9), ("doc-b#0", "doc-b", 0.6)]
        return [("doc-y#0", "doc-y", 0.4)]  # 完全未命中

    report = evaluate(rows, search, topk=5)
    assert report["recall_at"]["recall@1"] == round(1 / 3, 4)
    assert report["recall_at"]["recall@3"] == round(2 / 3, 4)
    assert report["recall_at"]["recall@5"] == round(2 / 3, 4)
    # MRR = (1 + 1/2 + 0) / 3
    assert report["mrr"] == round((1 + 0.5) / 3, 4)
    # nDCG@5 = (1/log2(2) + 1/log2(3) + 0) / 3
    import math

    assert report["ndcg@5"] == round((1.0 + 1 / math.log2(3)) / 3, 4)
    assert [m["id"] for m in report["missed"]] == ["q3"]


def test_baseline_retrieval_regression(seeded):
    """真实语料基线：改动检索链路后数字不能掉（阈值取实测值的保守下界）。"""
    from app.rag.eval import run_eval

    report = run_eval(topk=5)
    recall = report["recall_at"]
    assert report["count"] >= 30
    assert recall["recall@5"] == 1.0, f"Recall@5 掉了：{report['missed']}"
    assert recall["recall@3"] >= 0.95, recall
    assert recall["recall@1"] >= 0.80, recall
    assert report["mrr"] >= 0.85, report["mrr"]
    assert report["ndcg@5"] >= 0.90, report["ndcg@5"]


def test_report_is_json_serialisable(seeded):
    from app.rag.eval import run_eval

    report = run_eval(topk=5)
    text = json.dumps(report, ensure_ascii=False)
    assert "recall_at" in text and "rows" in text
