"""Embedding 提供方：腾讯云 TokenHub（真实语义向量） + 本地哈希（离线兜底）。

为什么要有抽象：`vectorizer.py` 的哈希向量是"离线可复现"的降级方案；真实语义检索要
embedding 模型。两种向量的维度不同（512 vs 1024/2560），不能混用同一个索引，所以：

- `hash` 后端：512 维，写进 SQLite `kb_chunk.vec`，**任何时候都保留**（离线兜底 + 测试）；
- `tencent` 后端：1024 维（0.6b）或 2560 维（4b），只在 Qdrant 索引里使用；
- 查询侧：向量化走同一后端；后端失败 → 该次查询降级到哈希 + 内存检索（不 500）。

腾讯云 TokenHub（OpenAI 兼容协议）：
- `POST {base_url}/embeddings`，`Authorization: Bearer <API_KEY>`；
- 参数：`model` / `input`（单条或数组，单条 ≤2000 字符、建议单批 ≤128 条）/ `encoding_format=float`；
- 维度由模型决定，**不支持 `dimensions` 参数**；
- 限流 429 会带 `Retry-After`（秒）；401/403 不重试；5xx 可重试。
文档：https://cloud.tencent.com/document/product/1823/133515
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Protocol

import httpx

from app.config import get_settings
from app.observability import latency
from app.rag.vectorizer import DIM as HASH_DIM
from app.rag.vectorizer import HashingVectorizer

logger = logging.getLogger("sku-agent.embedding")

# 模型 → 维度（TokenHub 文档：维度由模型固定，不能自定义）
MODEL_DIMS = {
    "kinfra-text-embedding-0.6b": 1024,
    "kinfra-text-embedding-4b": 2560,
}

DEFAULT_TENCENT_MODEL = "kinfra-text-embedding-0.6b"
DEFAULT_TENCENT_BASE_URL = "https://tokenhub.tencentmaas.com/v1"
MAX_TEXTS_PER_REQUEST = 128
MAX_CHARS_PER_TEXT = 2000


class EmbeddingError(RuntimeError):
    pass


class EmbeddingProvider(Protocol):
    backend: str
    model: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]: ...

    def health(self) -> dict: ...


class HashEmbedding:
    """本地确定性哈希向量（离线/兜底）：512 维，毫秒级，零网络。"""

    backend = "hash"
    model = "hash-ngram-512"
    dim = HASH_DIM

    def __init__(self) -> None:
        self._vectorizer = HashingVectorizer()

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vectorizer.embed(t or "").tolist() for t in texts]

    def embed_one(self, text: str) -> list[float]:
        return self._vectorizer.embed(text or "").tolist()

    def health(self) -> dict:
        return {"backend": self.backend, "model": self.model, "dim": self.dim,
                "ok": True, "note": "本地哈希 n-gram 向量（离线兜底，不需要网络）"}


class TencentEmbedding:
    """腾讯云 TokenHub 文本向量（OpenAI Embeddings 兼容）。

    同步实现：RAG 检索链路本身是同步的（LangGraph 节点内调用），用同步 httpx.Client
    更简单；重试/限流/超时都在这里兜住，失败抛 `EmbeddingError` 由上层降级。
    """

    backend = "tencent"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_TENCENT_MODEL,
        base_url: str = DEFAULT_TENCENT_BASE_URL,
        timeout: float = 8.0,
        max_retries: int = 2,
        batch_size: int = 64,
        concurrency: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise EmbeddingError("缺少 TENCENT_EMBEDDING_API_KEY（TokenHub 控制台创建）")
        self.model = model or DEFAULT_TENCENT_MODEL
        self.dim = MODEL_DIMS.get(self.model, 0)
        if not self.dim:
            raise EmbeddingError(
                f"未知的 embedding 模型 {self.model}；支持：{', '.join(MODEL_DIMS)}"
            )
        self._base_url = (base_url or DEFAULT_TENCENT_BASE_URL).rstrip("/")
        self._api_key = api_key
        self._timeout = float(timeout)
        self._max_retries = max(0, int(max_retries))
        self._batch_size = max(1, min(int(batch_size), MAX_TEXTS_PER_REQUEST))
        self._semaphore = threading.Semaphore(max(1, int(concurrency)))
        self._client = httpx.Client(
            timeout=self._timeout,
            trust_env=False,  # 不吃系统代理（本机配了代理软件时 127.0.0.1 也会被绕）
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            transport=transport,  # 测试注入 httpx.MockTransport；生产为默认网络传输
        )
        self._last_error = ""
        self._calls = 0
        self._tokens = 0

    # ------------------------------------------------------------ 核心
    def embed(self, texts: list[str]) -> list[list[float]]:
        cleaned = [self._prepare(t) for t in texts]
        out: list[list[float]] = []
        for start in range(0, len(cleaned), self._batch_size):
            batch = cleaned[start:start + self._batch_size]
            out.extend(self._embed_batch(batch))
        return out

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    @staticmethod
    def _prepare(text: str) -> str:
        text = (text or "").strip()
        if len(text) > MAX_CHARS_PER_TEXT:
            text = text[:MAX_CHARS_PER_TEXT]
        return text or " "

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": batch, "encoding_format": "float"}
        started = time.perf_counter()
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                with self._semaphore:
                    resp = self._client.post(f"{self._base_url}/embeddings", json=payload)
                if resp.status_code == 429:
                    retry_after = _retry_after_seconds(resp)
                    last_exc = EmbeddingError(f"TokenHub 限流 429（Retry-After={retry_after}s）")
                    time.sleep(retry_after if attempt < self._max_retries else 0)
                    continue
                if resp.status_code in (401, 403):
                    last_exc = EmbeddingError(
                        f"TokenHub 鉴权/权限失败 HTTP {resp.status_code}：{_error_text(resp)}"
                    )
                    break  # 鉴权类错误重试无意义
                if resp.status_code >= 500:
                    last_exc = EmbeddingError(f"TokenHub 服务端错误 HTTP {resp.status_code}")
                    time.sleep(0.4 * (attempt + 1))
                    continue
                if resp.status_code >= 400:
                    raise EmbeddingError(f"TokenHub 请求非法 HTTP {resp.status_code}：{_error_text(resp)}")
                data = resp.json()
                vectors = [item.get("embedding") or [] for item in (data.get("data") or [])]
                if len(vectors) != len(batch) or any(len(v) != self.dim for v in vectors):
                    raise EmbeddingError(
                        f"返回维度不符：期望 {len(batch)}×{self.dim}，实际 "
                        f"{len(vectors)}×{len(vectors[0]) if vectors else 0}"
                    )
                usage = data.get("usage") or {}
                tokens = int(usage.get("prompt_tokens") or 0)
                self._record_usage(tokens, elapsed_ms=(time.perf_counter() - started) * 1000)
                return vectors
            except httpx.HTTPError as exc:
                last_exc = EmbeddingError(f"TokenHub 网络错误：{type(exc).__name__}: {exc}")
                time.sleep(0.4 * (attempt + 1))
        self._last_error = str(last_exc or "unknown")
        latency.record("embedding", (time.perf_counter() - started) * 1000)
        raise EmbeddingError(f"TokenHub embedding 调用失败（已重试 {self._max_retries} 次）：{last_exc}")

    def _record_usage(self, tokens: int, elapsed_ms: float) -> None:
        self._calls += 1
        self._tokens += int(tokens or 0)
        latency.record("embedding", elapsed_ms)
        if tokens:
            try:
                from app.llm import record_usage  # 延迟导入，避免循环依赖

                record_usage(calls=0, input_tokens=tokens, output_tokens=0, source="embedding")
            except Exception:  # noqa: BLE001 —— 统计失败不影响检索
                pass

    def probe(self) -> dict:
        """启动探活：发一条极短文本，确认 key/地域/配额可用（失败只记录不阻塞启动）。"""
        try:
            self.embed_one("探活")
            return {"ok": True, "note": "TokenHub embedding 探活成功"}
        except EmbeddingError as exc:
            self._last_error = str(exc)
            return {"ok": False, "note": str(exc)}

    def health(self) -> dict:
        return {
            "backend": self.backend,
            "model": self.model,
            "dim": self.dim,
            "ok": not self._last_error,
            "calls": self._calls,
            "input_tokens": self._tokens,
            "error": self._last_error,
            "note": "腾讯云 TokenHub（OpenAI 兼容 /v1/embeddings）；维度由模型固定，不支持 dimensions",
        }

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


def _retry_after_seconds(resp: httpx.Response) -> float:
    raw = resp.headers.get("Retry-After") or ""
    try:
        return min(10.0, max(0.0, float(str(raw).strip())))
    except (TypeError, ValueError):
        return 1.0


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error") or {}
        return str(err.get("message_zh") or err.get("message") or err.get("code") or body)[:200]
    except Exception:  # noqa: BLE001
        return (resp.text or "")[:200]


# ---------------------------------------------------------------- 工厂
_provider: EmbeddingProvider | None = None
_provider_error: str = ""


def embedding_dim_for(model: str | None) -> int:
    return MODEL_DIMS.get(model or DEFAULT_TENCENT_MODEL, 0)


def build_provider(settings=None) -> EmbeddingProvider:
    """按配置构建 provider；tencent 不可用时**降级为 hash**（记录原因）。"""
    settings = settings or get_settings()
    backend = (settings.embedding_backend or "hash").strip().lower()
    if backend != "tencent":
        return HashEmbedding()
    try:
        return TencentEmbedding(
            api_key=settings.tencent_embedding_api_key,
            model=settings.tencent_embedding_model or DEFAULT_TENCENT_MODEL,
            base_url=settings.tencent_embedding_base_url or DEFAULT_TENCENT_BASE_URL,
            timeout=settings.embedding_timeout_seconds,
            max_retries=settings.embedding_max_retries,
            batch_size=settings.embedding_batch_size,
            concurrency=settings.embedding_concurrency,
        )
    except EmbeddingError as exc:
        global _provider_error
        _provider_error = str(exc)
        logger.warning("embedding 后端 tencent 不可用，已降级为 hash：%s", exc)
        return HashEmbedding()


def get_embedding_provider() -> EmbeddingProvider:
    global _provider
    if _provider is None:
        _provider = build_provider()
    return _provider


def embedding_status() -> dict:
    """health 用：当前 provider + 是否降级 + 原因。"""
    provider = get_embedding_provider()
    status = provider.health()
    status["degraded"] = bool(_provider_error) or status.get("backend") == "hash" and (
        get_settings().embedding_backend or "hash"
    ).lower() == "tencent"
    if _provider_error:
        status["degraded_reason"] = _provider_error
    return status


def reset_provider() -> None:
    global _provider, _provider_error
    if isinstance(_provider, TencentEmbedding):
        _provider.close()
    _provider = None
    _provider_error = ""


