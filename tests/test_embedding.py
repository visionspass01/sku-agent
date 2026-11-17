"""腾讯云 TokenHub Embedding 客户端：请求格式、重试/限流、维度校验、降级。

全部用 httpx.MockTransport，不联网、不消耗真实额度。
"""

from __future__ import annotations

import httpx

from app.rag import embedding as emb


def _provider(handler, **kwargs):
    provider = emb.TencentEmbedding(
        api_key="sk-test",
        model="kinfra-text-embedding-0.6b",
        base_url="https://mock.tokenhub/v1",
        timeout=2,
        max_retries=1,
        batch_size=2,
        concurrency=2,
        transport=httpx.MockTransport(handler),
    )
    return provider


def _ok_response(request: httpx.Request) -> httpx.Response:
    body = __import__("json").loads(request.content)
    n = len(body["input"])
    dim = 1024 if body["model"] == "kinfra-text-embedding-0.6b" else 8
    vectors = [[0.001 * (i + 1)] * dim for i in range(n)]
    return httpx.Response(200, json={
        "object": "list",
        "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)],
        "usage": {"prompt_tokens": 10 * n},
    })


def test_tencent_request_shape_and_parse():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = __import__("json").loads(request.content)
        return _ok_response(request)

    provider = _provider(handler)
    vectors = provider.embed(["断码怎么处理", "清仓折扣"])
    assert len(vectors) == 2 and len(vectors[0]) == 1024
    assert seen["url"] == "https://mock.tokenhub/v1/embeddings"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "kinfra-text-embedding-0.6b"
    assert seen["body"]["input"] == ["断码怎么处理", "清仓折扣"]
    assert seen["body"]["encoding_format"] == "float"
    assert "dimensions" not in seen["body"], "TokenHub 不支持自定义维度，不能传"
    # usage.prompt_tokens 要计入 embedding 来源的用量（看板成本口径）
    from app.llm import get_llm

    src = get_llm().source_stats.get("embedding") or {}
    assert src.get("input_tokens") == 20


def test_tencent_batches_long_input():
    calls = {"n": 0, "sizes": []}

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        calls["n"] += 1
        calls["sizes"].append(len(body["input"]))
        return _ok_response(request)

    provider = _provider(handler, batch_size=2)
    provider.embed(["a", "b", "c", "d", "e"])
    assert calls["sizes"] == [2, 2, 1], calls


def test_tencent_429_retry_after_then_success(monkeypatch):
    state = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={
                "error": {"code": 429001, "message_zh": "请求速率超过阈值"}})
        return _ok_response(request)

    provider = _provider(handler, max_retries=2)
    monkeypatch.setattr(emb.time, "sleep", lambda s: sleeps.append(s))
    vectors = provider.embed(["断码"])
    assert len(vectors) == 1 and state["n"] == 2
    assert sleeps == [0.0], sleeps


def test_tencent_401_does_not_retry():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(401, json={"error": {"code": 401002, "message_zh": "API Key 不存在"}})

    provider = _provider(handler, max_retries=3)
    try:
        provider.embed(["断码"])
        raise AssertionError("应当抛 EmbeddingError")
    except emb.EmbeddingError as exc:
        assert "鉴权" in str(exc)
    assert state["n"] == 1, "401 不应重试"


def test_tencent_dimension_mismatch_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": [{"embedding": [0.1] * 8}], "usage": {"prompt_tokens": 3}})

    provider = _provider(handler)
    try:
        provider.embed(["断码"])
        raise AssertionError("维度不符应抛错")
    except emb.EmbeddingError as exc:
        assert "维度" in str(exc)


def test_unknown_model_rejected():
    try:
        emb.TencentEmbedding(api_key="k", model="not-a-model")
        raise AssertionError("未知模型应抛错")
    except emb.EmbeddingError as exc:
        assert "未知的 embedding 模型" in str(exc)


def test_hash_provider_dim_and_health():
    provider = emb.HashEmbedding()
    v = provider.embed_one("断码补全 SOP")
    assert len(v) == 512
    assert provider.health()["backend"] == "hash"


def test_build_provider_falls_back_to_hash_without_key(_settings, monkeypatch):
    monkeypatch.setattr(_settings, "embedding_backend", "tencent")
    monkeypatch.setattr(_settings, "tencent_embedding_api_key", "")
    emb.reset_provider()
    provider = emb.build_provider(_settings)
    assert isinstance(provider, emb.HashEmbedding)
    status = emb.embedding_status()
    assert status["backend"] == "hash"
    assert status["degraded"] is True
    assert "TENCENT_EMBEDDING_API_KEY" in (status.get("degraded_reason") or "")
    emb.reset_provider()


def test_tencent_embedding_probe():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(request)

    provider = _provider(handler)
    result = provider.probe()
    assert result["ok"] is True
