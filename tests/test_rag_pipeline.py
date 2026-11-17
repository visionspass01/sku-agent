"""RAG 统一入口 + 对话可用的知识库工具（评审 3.4-11 / 3.4-12）。

两件事：
1. `/api/rag/search` 与 `search_knowledge` 工具走**同一条 pipeline**（同一份缓存）；
2. 对话 Agent 现在真的能检索知识库（之前 TOOL_GROUPS 里根本没有检索工具，
   知识库只被批量巡检和 skill 用到）。
"""

from __future__ import annotations

import asyncio


def test_search_kb_returns_hits_and_citations(seeded):
    from app.rag.pipeline import search_kb

    result = asyncio.run(search_kb("断码怎么补 调拨还是补货", topk=3, backend="none"))
    assert result.hits, "域内查询没有召回"
    assert result.no_answer is False
    assert result.rerank["backend"] == "none"
    citations = result.citations
    assert citations and citations[0]["chunk_id"]
    assert citations[0]["snippet"]
    assert result.context().startswith("[")


def test_search_kb_reports_no_answer(seeded):
    from app.rag.pipeline import search_kb

    result = asyncio.run(search_kb("公司食堂几点开饭", topk=3, backend="none"))
    assert result.hits == []
    assert result.no_answer is True
    assert result.citations == []


def test_search_kb_is_cached(seeded):
    from app.cache.ttl_cache import reset_ttl_cache
    from app.rag.pipeline import search_kb

    reset_ttl_cache()

    async def main():
        first = await search_kb("补货 安全库存", topk=3, backend="none")
        second = await search_kb("补货 安全库存", topk=3, backend="none")
        return first, second

    first, second = asyncio.run(main())
    assert first.diagnostics.get("cached") in (False, None)
    assert second.diagnostics.get("cached") is True


def test_search_kb_sync_helper(seeded):
    from app.rag.pipeline import search_kb_sync

    result = search_kb_sync("清仓 折扣", topk=2)
    assert isinstance(result.hits, list)


def test_pipeline_status_shape(seeded):
    from app.rag.pipeline import status

    payload = status()
    assert "rerank" in payload and "backend" in payload["rerank"]
    assert "store" in payload and "embedding" in payload
    assert payload["synonyms"]["source"] in ("platform", "fallback")


def test_search_knowledge_tool_is_assembled(seeded):
    from app.tools.platform_tools import ALL_TOOL_NAMES, TOOL_GROUPS, TOOL_SCOPES

    assert "search_knowledge" in ALL_TOOL_NAMES
    assert "kb" in TOOL_GROUPS and "search_knowledge" in TOOL_GROUPS["kb"]
    assert TOOL_SCOPES["search_knowledge"] == "kb"


def test_search_knowledge_tool_filters_by_department(seeded):
    """部门账号检索知识库时只看「本部门 + 通用」文档。"""
    from app.auth import AuthUser
    from app.tools.platform_tools import build_platform_tools

    worker1 = AuthUser(username="worker1", role="user", department="男装")
    tools = {t.name: t for t in build_platform_tools(worker1)}
    data = asyncio.run(tools["search_knowledge"].ainvoke({"query": "补货量怎么算", "topk": 3}))
    assert "hits" in data
    for hit in data["hits"]:
        assert hit["doc_id"]
    # 通用文档可见、其它部门专属文档不可见
    titles = {hit["doc_id"] for hit in data["hits"]}
    assert not any(doc_id.endswith("-women") for doc_id in titles), titles


def test_search_knowledge_tool_reports_no_answer(seeded):
    from app.auth import AuthUser
    from app.tools.platform_tools import build_platform_tools

    admin = AuthUser(username="admin", role="admin", department=None)
    tools = {t.name: t for t in build_platform_tools(admin)}
    data = asyncio.run(tools["search_knowledge"].ainvoke({"query": "K8s 的 HPA 怎么配置"}))
    assert data["no_answer"] is True
    assert data["count"] == 0
    assert "没有相关内容" in data["note"]


def test_rag_search_endpoint_matches_pipeline(app_client, login, seeded):
    """/api/rag/search 与工具同源：都返回 citations 与 no_answer 字段。"""
    headers = login("admin", "hhw123")
    body = app_client.get("/api/rag/search", params={"q": "断码 调拨"}, headers=headers).json()
    assert "hits" in body and "citations" in body and "no_answer" in body
    assert body["scope"]
    assert "rerank" in body and "diagnostics" in body


def test_rag_search_endpoint_supports_backend_override(app_client, login, seeded):
    """支持 `backend=none` 关闭重排（消融与排障用），不比默认慢。"""
    headers = login("admin", "hhw123")
    body = app_client.get("/api/rag/search",
                          params={"q": "补货", "backend": "none"}, headers=headers).json()
    assert body["rerank"]["backend"] == "none"
    assert body["rerank"]["degraded"] is False


def test_rag_skill_subgraph_uses_pipeline(seeded):
    """RAG 子图的 rerank 节点不再是空转（结果里带 rerank 后端自述）。"""
    from app.rag.skill import build_rag_skill

    graph = build_rag_skill()
    import asyncio as _asyncio

    state = _asyncio.run(graph.ainvoke({"query": "断码 补全", "filters": {"department": ["通用"]},
                                        "as_of": "2026-11-02", "topk": 3, "backend": "none"}))
    assert state["hits"]
    assert state["rerank"]["backend"] == "none"
    assert state["citations"]


def test_synonyms_status_and_refresh_offline(seeded):
    """离线时同义词用内置兜底表（refresh 失败不抛错，只记 error）。"""
    from app.rag import synonyms

    synonyms.reset()
    status = asyncio.run(synonyms.refresh(force=True))
    assert status["count"] >= 10
    assert "断码" in synonyms.synonyms_for()
