"""本地确定性向量化（RAG 链路第 3 步）。

DeepSeek 没有 Embeddings API，演示采用「字符 n-gram 哈希 + TF 归一化 + L2 归一化」
的确定性向量：无需外网模型、可复现、跨进程一致（md5 哈希而非 Python hash）。
生产环境可替换为 BGE-M3 / text-embedding-3 等 Embedding 服务，接口保持不变。
"""

from __future__ import annotations

import hashlib
import re

import numpy as np

DIM = 512

_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+")


def _tokens(text: str) -> list[str]:
    """中文按连续片段取 1~3 字 n-gram；英文/数字按词切分。"""
    out: list[str] = []
    for seg in _TOKEN_RE.findall(text or ""):
        if re.fullmatch(r"[A-Za-z0-9_]+", seg):
            out.append(seg.lower())
        else:
            n = len(seg)
            for size in (1, 2, 3):
                for i in range(0, max(0, n - size + 1)):
                    out.append(seg[i:i + size])
    return out


def _bucket(token: str) -> int:
    h = hashlib.md5(token.encode("utf-8")).hexdigest()
    return int(h[:8], 16) % DIM


class HashingVectorizer:
    """哈希字符 n-gram TF 向量（稀疏转稠密，L2 归一化）。"""

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        tokens = _tokens(text)
        if not tokens:
            return vec
        counts: dict[str, int] = {}
        for t in tokens:
            counts[t] = counts.get(t, 0) + 1
        max_tf = max(counts.values())
        for t, c in counts.items():
            # 亚线性 TF
            vec[_bucket(t)] += (1.0 + np.log(c)) / (1.0 + np.log(max_tf))
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        return vec

    @staticmethod
    def to_bytes(vec: np.ndarray) -> bytes:
        return np.asarray(vec, dtype=np.float32).tobytes()

    @staticmethod
    def from_bytes(raw: bytes, dim: int = DIM) -> np.ndarray:
        arr = np.frombuffer(raw, dtype=np.float32)
        if arr.shape[0] != dim:
            # 容错：长度不一致时截断/补零
            fixed = np.zeros(dim, dtype=np.float32)
            fixed[: min(dim, arr.shape[0])] = arr[: min(dim, arr.shape[0])]
            return fixed
        return arr


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom <= 0:
        return 0.0
    return float(np.dot(a, b) / denom)
