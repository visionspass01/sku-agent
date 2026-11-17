"""Rerank（重排）：真重排 + 预算 + 诚实降级（评审 3.1 / 3.4-7）。

为什么需要真 rerank：
当前「混合检索」的两路（BM25 与 512 维哈希向量）本质都是**词法**信号，彼此信息重叠。
1200 篇文档时 top-5 里会挤满同款近重复块，靠再调权重收效很小；
把候选（30 条）按语义相关性重排一遍，是唯一还有明显信息增量的一步。

四种后端（`RAG_RERANK_BACKEND`）：
- `none`      ：不重排（消融实验的对照组）；
- `heuristic` ：现逻辑（词覆盖 / 标题命中 / 版本新鲜度）——离线默认，零成本；
- `llm`       ：LLM listwise 打分（生产默认）：一次调用重排 ≤30 条候选，输出 JSON 排序；
- `cross`     ：cross-encoder（`sentence-transformers`，需 `pip install .[rerank]`）。

三条纪律（比"精度更高"更重要）：
1. **预算**：`RAG_RERANK_TIMEOUT_MS` 到点就回退融合序，不拖着一轮对话等重排；
2. **诚实**：回退时 `rerank_degraded=True` 一路传到 citations/run_detail，不假装排过；
3. **可控**：`RAG_RERANK_MAX_CANDIDATES` 限候选数，重排成本与语料规模无关。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from app.config import get_settings

logger = logging.getLogger("sku-agent.rag.rerank")

BACKENDS = ("none", "heuristic", "llm", "cross")


class RerankResult:
    """一次重排的结果：命中的新顺序 + 后端自述（谁排的、有没有降级）。"""

    __slots__ = ("hits", "backend", "degraded", "reason", "elapsed_ms")

    def __init__(self, hits: list, backend: str, *, degraded: bool = False,
                 reason: str = "", elapsed_ms: float = 0.0) -> None:
        self.hits = hits
        self.backend = backend
        self.degraded = degraded
        self.reason = reason
        self.elapsed_ms = elapsed_ms

    def meta(self) -> dict:
        return {"backend": self.backend, "degraded": self.degraded,
                "reason": self.reason, "elapsed_ms": round(self.elapsed_ms, 1)}


def active_backend() -> str:
    backend = str(getattr(get_settings(), "rag_rerank_backend", "heuristic") or "heuristic").lower()
    return backend if backend in BACKENDS else "heuristic"


def is_available(backend: str) -> tuple[bool, str]:
    """后端是否可用（缺依赖/缺 key 时给出原因，而不是运行时才炸）。"""
    if backend in ("none", "heuristic"):
        return True, ""
    if backend == "llm":
        settings = get_settings()
        if not getattr(settings, "deepseek_api_key", ""):
            return False, "未配置 DEEPSEEK_API_KEY"
        return True, ""
    if backend == "cross":
        try:
            import sentence_transformers  # noqa: F401
        except ImportError:
            return False, "未安装 sentence-transformers（pip install -e .[rerank]）"
        return True, ""
    return False, f"未知 rerank 后端：{backend}"


# ---------------------------------------------------------------- heuristic
def heuristic_rerank(query_tokens: set[str], scored: list[dict], *, version_boost) -> list[dict]:
    """启发式加分：词覆盖 + 小节标题命中 + 文档版本新鲜度。

    关键点：**先把融合分归一化到 [0,1] 再加分**。
    融合分是 RRF（~1e-2 量级），而 `0.15*coverage` 是 ~1e-1 量级——直接相加会让
    「词覆盖」完全压过「两路召回的名次」，实测把 R@1 从 89.5% 拉到 68.4%。
    归一化之后启发式只做**同量级的微调**（tie-breaker），不会推翻融合排序。
    """
    if not scored:
        return []
    max_base = max(float(item.get("score") or 0.0) for item in scored) or 1.0
    for item in scored:
        text_tokens = item.get("_tokens") or set()
        coverage = len(query_tokens & text_tokens) / max(1, len(query_tokens))
        section = str((item.get("meta") or {}).get("section") or "")
        heading = 0.03 if any(tok and len(tok) >= 2 and tok in section for tok in query_tokens) else 0.0
        base = float(item.get("score") or 0.0) / max_base
        item["rerank_score"] = (base + 0.10 * coverage + heading
                                + float(version_boost((item.get("meta") or {}).get("version", "v1"))))
    return sorted(scored, key=lambda h: h["rerank_score"], reverse=True)


# ---------------------------------------------------------------- LLM listwise
_RERANK_SYSTEM = """你是检索结果重排器。给定用户问题和若干候选片段，按「能否直接回答该问题」排序。
只输出 JSON：{"ranking":[{"id":"<片段id>","score":0~1,"reason":"不超过12字"}, ...]}。
规则：
- 覆盖问题关键约束（部门/季节/平台/数量阈值/时效）的片段排前面；
- 与本问题无关、或只是同领域泛泛而谈的片段，score 给 0.1 以下；
- 一个都不相关时返回空数组；
- 不要编造片段里没有的信息，不要输出 JSON 之外的任何文字。"""


def _llm_prompt(query: str, candidates: list[dict], max_chars: int = 200) -> str:
    lines = [f"用户问题：{query}", "", "候选片段："]
    for idx, item in enumerate(candidates, start=1):
        meta = item.get("meta") or {}
        text = re.sub(r"\s+", " ", str(item.get("text") or ""))[:max_chars]
        lines.append(f"[{idx}] 《{meta.get('title', '')}》{meta.get('section', '')}：{text}")
    lines.append("")
    lines.append(f"请对以上 {len(candidates)} 个片段打分排序，只输出 JSON。")
    return "\n".join(lines)


def parse_llm_ranking(payload: str, candidates: list[dict]) -> list[dict] | None:
    """解析 LLM 排序：id 支持 1-based 序号或真实 chunk_id。解析失败返回 None（回退融合序）。"""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    ranking = (data or {}).get("ranking")
    if not isinstance(ranking, list):
        return None
    by_index = {str(i): item for i, item in enumerate(candidates, start=1)}
    by_chunk = {str(item.get("chunk_id")): item for item in candidates}
    ordered: list[dict] = []
    seen: set[str] = set()
    for row in ranking:
        if not isinstance(row, dict):
            continue
        key = str(row.get("id") or "").strip()
        item = by_index.get(key) or by_chunk.get(key)
        chunk_id = str((item or {}).get("chunk_id") or "")
        if item is None or chunk_id in seen:
            continue
        seen.add(chunk_id)
        item = dict(item)
        try:
            item["rerank_score"] = float(row.get("score") or 0.0)
        except (TypeError, ValueError):
            item["rerank_score"] = 0.0
        item["rerank_reason"] = str(row.get("reason") or "")[:24]
        ordered.append(item)
    if not ordered:
        return []
    # LLM 漏掉的候选排在后面（不放前面：它没被判定为相关）
    for item in candidates:
        if str(item.get("chunk_id")) not in seen:
            item = dict(item)
            item["rerank_score"] = 0.0
            item["rerank_reason"] = "未被模型选中"
            ordered.append(item)
    return ordered


# ---------------------------------------------------------------- cross-encoder
_CROSS_MODEL: dict = {"name": "", "model": None}


def _cross_model(name: str):
    cached = _CROSS_MODEL
    if cached["model"] is not None and cached["name"] == name:
        return cached["model"]
    from sentence_transformers import CrossEncoder

    model = CrossEncoder(name)
    _CROSS_MODEL.update({"name": name, "model": model})
    return model


def cross_rerank(query: str, candidates: list[dict], model_name: str) -> list[dict]:
    model = _cross_model(model_name)
    pairs = [(query, str(item.get("text") or "")) for item in candidates]
    scores = model.predict(pairs)
    out = []
    for item, score in zip(candidates, scores):
        item = dict(item)
        item["rerank_score"] = float(score)
        out.append(item)
    return sorted(out, key=lambda h: h["rerank_score"], reverse=True)


# ---------------------------------------------------------------- 统一入口
async def rerank(query: str, candidates: list[dict], *, topk: int,
                 backend: str | None = None) -> RerankResult:
    """对候选重排并截断到 topk。任何异常/超时都回退融合序并标记 degraded。"""
    settings = get_settings()
    backend = (backend or active_backend()).lower()
    started = time.perf_counter()
    if not candidates:
        return RerankResult([], backend, elapsed_ms=0.0)

    ok, reason = is_available(backend)
    if backend == "none":
        return RerankResult(candidates[:topk], "none", elapsed_ms=0.0)
    if not ok:
        logger.warning("rerank 后端不可用，回退融合序：%s（%s）", backend, reason)
        return RerankResult(candidates[:topk], backend, degraded=True, reason=reason,
                            elapsed_ms=(time.perf_counter() - started) * 1000)

    max_candidates = max(1, int(getattr(settings, "rag_rerank_max_candidates", 30)))
    pool = candidates[:max_candidates]
    timeout = max(0.05, float(getattr(settings, "rag_rerank_timeout_ms", 400)) / 1000.0)

    try:
        if backend == "heuristic":
            from app.rag.hybrid import _version_boost

            query_tokens = {tok for tok in str(query or "") if tok.strip()}
            ordered = heuristic_rerank(query_tokens, [dict(c) for c in pool],
                                       version_boost=_version_boost)
        elif backend == "llm":
            ordered = await asyncio.wait_for(_llm_rerank(query, pool), timeout=timeout)
            if ordered is None:
                raise RuntimeError("LLM 重排输出无法解析")
        else:                                    # cross
            ordered = await asyncio.wait_for(
                asyncio.to_thread(cross_rerank, query, pool,
                                  str(getattr(settings, "rag_rerank_model", "BAAI/bge-reranker-base"))),
                timeout=timeout,
            )
    except asyncio.TimeoutError:
        logger.warning("rerank 超时（>%sms），回退融合序", int(timeout * 1000))
        return RerankResult(candidates[:topk], backend, degraded=True,
                            reason=f"timeout>{int(timeout * 1000)}ms",
                            elapsed_ms=(time.perf_counter() - started) * 1000)
    except Exception as exc:  # noqa: BLE001 —— 重排失败只降级，不阻断检索
        logger.warning("rerank 失败，回退融合序：%s: %s", type(exc).__name__, exc)
        return RerankResult(candidates[:topk], backend, degraded=True,
                            reason=f"{type(exc).__name__}: {exc}"[:120],
                            elapsed_ms=(time.perf_counter() - started) * 1000)

    return RerankResult(ordered[:topk], backend,
                        elapsed_ms=(time.perf_counter() - started) * 1000)


async def _llm_rerank(query: str, candidates: list[dict]) -> list[dict] | None:
    from app.llm import get_llm

    payload = await get_llm().invoke_text(_RERANK_SYSTEM, _llm_prompt(query, candidates),
                                          max_tokens=800)
    return parse_llm_ranking(payload, candidates)
