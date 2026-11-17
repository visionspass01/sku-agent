"""Rerank（评审 3.1 / 3.4-7）。

四个后端 + 三条纪律：预算内、可回退、回退要**诚实标注**（`rerank_degraded`）。
这里不需要真实网络：LLM 后端用打桩的 `get_llm()`，cross 后端用打桩的模型。
"""

from __future__ import annotations

import asyncio
import json

import pytest


def _candidates(n: int = 6) -> list[dict]:
    return [
        {"chunk_id": f"d{i}#aa", "doc_id": f"d{i}", "text": f"候选片段 {i} 补货 断码",
         "meta": {"title": f"文档{i}", "section": "第一节", "version": "v1"},
         "score": 0.03 - i * 0.001, "vector_score": 0.5 - i * 0.01,
         "keyword_score": 30.0 - i, "_tokens": {"补货", "断码"}}
        for i in range(n)
    ]


def test_backend_selection_and_availability(monkeypatch, _settings):
    from app.rag import rerank

    monkeypatch.setattr(_settings, "rag_rerank_backend", "heuristic")
    assert rerank.active_backend() == "heuristic"

    monkeypatch.setattr(_settings, "rag_rerank_backend", "不存在的后端")
    assert rerank.active_backend() == "heuristic"          # 非法值回退，而不是崩

    monkeypatch.setattr(_settings, "deepseek_api_key", "")
    ok, reason = rerank.is_available("llm")
    assert ok is False and "DEEPSEEK_API_KEY" in reason
    assert rerank.is_available("heuristic") == (True, "")


def test_none_backend_keeps_fusion_order(_settings):
    from app.rag import rerank

    cands = _candidates()
    result = asyncio.run(rerank.rerank("补货", cands, topk=3, backend="none"))
    assert [c["chunk_id"] for c in result.hits] == [c["chunk_id"] for c in cands[:3]]
    assert result.backend == "none" and result.degraded is False


def test_heuristic_does_not_override_fusion_order(_settings):
    """启发式只做同量级微调：归一化后 base 占主导，不能把第 1 名挤下去。"""
    from app.rag import rerank

    cands = _candidates()
    result = asyncio.run(rerank.rerank("补货 断码", cands, topk=3, backend="heuristic"))
    assert result.hits[0]["chunk_id"] == cands[0]["chunk_id"]
    assert result.degraded is False
    assert all("rerank_score" in c for c in result.hits)


def test_missing_dependency_degrades_honestly(monkeypatch, _settings):
    """cross 后端缺依赖：回退融合序，并且**标记 degraded**（不假装排过）。"""
    from app.rag import rerank

    monkeypatch.setattr(_settings, "rag_rerank_backend", "cross")
    monkeypatch.setattr(rerank, "is_available", lambda backend: (False, "未安装 sentence-transformers"))
    cands = _candidates()
    result = asyncio.run(rerank.rerank("补货", cands, topk=3, backend="cross"))
    assert result.degraded is True
    assert "sentence-transformers" in result.reason
    assert [c["chunk_id"] for c in result.hits] == [c["chunk_id"] for c in cands[:3]]


def test_timeout_falls_back_within_budget(monkeypatch, _settings):
    """超预算立即回退：一轮对话不该为重排多等。"""
    from app.rag import rerank

    monkeypatch.setattr(_settings, "rag_rerank_timeout_ms", 20)
    monkeypatch.setattr(_settings, "deepseek_api_key", "sk-test")

    async def _slow(query, candidates):
        await asyncio.sleep(0.5)
        return []

    monkeypatch.setattr(rerank, "_llm_rerank", _slow)
    cands = _candidates()
    started = __import__("time").perf_counter()
    result = asyncio.run(rerank.rerank("补货", cands, topk=3, backend="llm"))
    elapsed = __import__("time").perf_counter() - started
    assert result.degraded is True and "timeout" in result.reason
    assert elapsed < 0.4, f"超时没有生效（{elapsed:.2f}s）"


def test_llm_rerank_applies_model_order(monkeypatch, _settings):
    """LLM 排序生效：模型给的顺序覆盖融合顺序，并带上 rerank_score。"""
    from app.rag import rerank

    monkeypatch.setattr(_settings, "deepseek_api_key", "sk-test")
    monkeypatch.setattr(_settings, "rag_rerank_timeout_ms", 5000)

    async def _fake(query, candidates):
        # 模型认为最后一个最相关
        return rerank.parse_llm_ranking(
            json.dumps({"ranking": [
                {"id": str(len(candidates)), "score": 0.95, "reason": "直接回答"},
                {"id": "1", "score": 0.2, "reason": "泛泛而谈"},
            ]}, ensure_ascii=False),
            candidates,
        )

    monkeypatch.setattr(rerank, "_llm_rerank", _fake)
    cands = _candidates()
    result = asyncio.run(rerank.rerank("补货", cands, topk=2, backend="llm"))
    assert result.degraded is False
    assert result.hits[0]["chunk_id"] == cands[-1]["chunk_id"]
    assert result.hits[0]["rerank_score"] == pytest.approx(0.95)
    assert result.hits[0]["rerank_reason"] == "直接回答"


def test_parse_llm_ranking_handles_garbage():
    from app.rag import rerank

    cands = _candidates(3)
    assert rerank.parse_llm_ranking("不是 json", cands) is None
    assert rerank.parse_llm_ranking('{"no_ranking": 1}', cands) is None
    # 空数组 = 模型认为全都不相关：返回空列表（由上层决定是否弃答）
    assert rerank.parse_llm_ranking('{"ranking": []}', cands) == []


def test_rerank_meta_is_serializable(_settings):
    from app.rag import rerank

    result = asyncio.run(rerank.rerank("补货", _candidates(), topk=3, backend="heuristic"))
    payload = json.dumps(result.meta(), ensure_ascii=False)
    assert "backend" in payload and "degraded" in payload


def test_candidate_cap_limits_cost(monkeypatch, _settings):
    """候选数受 RAG_RERANK_MAX_CANDIDATES 限制：重排成本与语料规模无关。"""
    from app.rag import rerank

    monkeypatch.setattr(_settings, "rag_rerank_max_candidates", 3)
    monkeypatch.setattr(_settings, "rag_rerank_backend", "heuristic")
    seen = {"n": 0}
    real = rerank.heuristic_rerank

    def _counting(query_tokens, scored, **kw):
        seen["n"] = len(scored)
        return real(query_tokens, scored, **kw)

    monkeypatch.setattr(rerank, "heuristic_rerank", _counting)
    asyncio.run(rerank.rerank("补货", _candidates(20), topk=5, backend="heuristic"))
    assert seen["n"] == 3
