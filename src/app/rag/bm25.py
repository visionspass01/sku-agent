"""中文分词 + BM25 关键词召回（RAG 链路第 4 步的一半）。

三个要点：
1. **字段加权**：标题、小节标题、正文分别算 BM25，再按 2.0 / 1.5 / 1.0 加权求和。
   标题里的「清仓」「断码」是最强意图信号，与正文里偶然出现的同名词不该等权。
2. **倒排索引自实现**：`rank_bm25` 的 `get_scores` 是纯 Python 全量循环——
   1200 篇（≈5700 块）时单次查询要 200ms+，是规模化的主要瓶颈。
   这里建 `token → [(doc, tf)]` 倒排（numpy 存储），只对**命中词**的文档打分，
   公式与 BM25Okapi 一致（k1=1.5, b=0.75, epsilon 处理负 idf），排名结果基本等价。
3. **可选中日分词**：装了 jieba 就用词级切分 + 2-gram 兜底；没装则纯 2-gram
   （演示环境零额外依赖，行为可复现）。
"""

from __future__ import annotations

import logging
import math
import re

import numpy as np

logger = logging.getLogger("sku-agent.rag.bm25")

# 字段权重：标题 > 小节标题 > 正文
FIELD_WEIGHTS = (("title", 2.0), ("section", 1.5), ("body", 1.0))
K1 = 1.5
B = 0.75

_JIEBA = None
_JIEBA_TRIED = False


def _jieba():
    """惰性加载 jieba（可选依赖）。没有就返回 None，调用方回退 2-gram。"""
    global _JIEBA, _JIEBA_TRIED
    if _JIEBA_TRIED:
        return _JIEBA
    _JIEBA_TRIED = True
    try:
        import jieba  # type: ignore[import-not-found]

        jieba.setLogLevel(logging.WARNING)
        _JIEBA = jieba
        logger.info("BM25 使用 jieba 词级分词（中文召回更精确）")
    except Exception:  # noqa: BLE001 —— 没装就是没装，静默回退 2-gram
        _JIEBA = None
    return _JIEBA


def tokenize_zh(text: str) -> list[str]:
    """中文分词：有 jieba 用词级 + 2-gram 兜底；否则纯 2-gram（英文/数字按词）。"""
    tokens: list[str] = []
    text = text or ""
    for seg in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", text):
        if re.fullmatch(r"[A-Za-z0-9_]+", seg):
            tokens.append(seg.lower())
            continue
        if len(seg) == 1:
            tokens.append(seg)
            continue
        # 2-gram：保证「断码」「清仓」这类高频词一定被切出来
        for i in range(len(seg) - 1):
            tokens.append(seg[i:i + 2])
        jieba = _jieba()
        if jieba is not None:
            tokens.extend(w for w in jieba.lcut(seg) if len(w) > 1)
    return tokens


class _FieldIndex:
    """单字段倒排索引：token → (doc_idx[], tf[])，配合 BM25 打分。"""

    __slots__ = ("postings", "doc_len", "avg_len", "idf", "n_docs", "avg_idf")

    def __init__(self, corpus: list[list[str]]) -> None:
        self.n_docs = len(corpus)
        self.doc_len = np.array([len(tokens) for tokens in corpus], dtype=np.float32)
        total = float(self.doc_len.sum())
        self.avg_len = float(total / self.n_docs) if self.n_docs and total > 0 else 0.0
        postings: dict[str, dict[int, int]] = {}
        for doc_idx, tokens in enumerate(corpus):
            for token in tokens:
                bucket = postings.setdefault(token, {})
                bucket[doc_idx] = bucket.get(doc_idx, 0) + 1
        self.postings: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for token, bucket in postings.items():
            docs = np.fromiter(bucket.keys(), dtype=np.int32, count=len(bucket))
            tfs = np.fromiter(bucket.values(), dtype=np.float32, count=len(bucket))
            self.postings[token] = (docs, tfs)
        # idf 用与 BM25Okapi 相同的平滑形式，并对负值做 epsilon 归零
        self.idf: dict[str, float] = {}
        for token, (docs, _tf) in self.postings.items():
            df = len(docs)
            value = math.log((self.n_docs - df + 0.5) / (df + 0.5) + 1.0)
            self.idf[token] = value
        positives = [v for v in self.idf.values() if v > 0]
        self.avg_idf = float(sum(positives) / len(positives)) if positives else 0.0

    def score(self, tokens: list[str], into: np.ndarray) -> None:
        """把本字段的 BM25 分累加进 `into`（只触碰命中词的文档）。"""
        if not tokens or not self.postings:
            return
        epsilon = self.avg_idf * 0.25
        norm = K1 * (1.0 - B)
        for token in tokens:
            entry = self.postings.get(token)
            if entry is None:
                continue
            idf = self.idf[token]
            if idf <= 0:
                # 与 BM25Okapi 一致的 epsilon 下限：高频词不产生负分
                idf = epsilon
            docs, tfs = entry
            lens = self.doc_len[docs]
            denom = tfs + norm + K1 * B * (lens / self.avg_len if self.avg_len else 1.0)
            np.add.at(into, docs, idf * tfs * (K1 + 1.0) / denom)


class KeywordIndex:
    """按字段加权的 BM25（倒排索引实现）。`docs` 可以是字符串或 {field: text} 字典。"""

    def __init__(self, docs: list) -> None:
        self._docs = docs
        node_docs = docs or []
        first = node_docs[0] if node_docs else ""
        if isinstance(first, dict):
            self._fields = list(FIELD_WEIGHTS)
            self._indexes = {
                field: _FieldIndex([tokenize_zh(str((d or {}).get(field) or "")) for d in node_docs])
                for field, _weight in FIELD_WEIGHTS
            }
        else:
            self._fields = [("body", 1.0)]
            self._indexes = {"body": _FieldIndex([tokenize_zh(str(d or "")) for d in node_docs])}

    def scores(self, query: str) -> list[float]:
        tokens = tokenize_zh(query)
        if not self._docs:
            return []
        total = np.zeros(len(self._docs), dtype=np.float64)
        if not tokens:
            return [0.0] * len(self._docs)
        for field, weight in self._fields:
            index = self._indexes.get(field)
            if index is None:
                continue
            buf = np.zeros(len(self._docs), dtype=np.float64)
            index.score(tokens, buf)
            total += weight * buf
        return [float(x) for x in total]

    def raw_score(self, query: str) -> float:
        """该查询在语料上的最高 BM25 分（用于「无答案」判定：0 = 一个词都没命中）。"""
        scores = self.scores(query)
        return max(scores) if scores else 0.0
