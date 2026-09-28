"""BM25 稀疏检索。

和向量检索是互补的：向量擅长「意思相近」，BM25 擅长「字面精确命中」。
客服场景里订单号、政策条款号、SKU、"DDP" 这类专有名词，BM25 的召回质量往往高于向量。
"""

from __future__ import annotations

import logging
import threading

from rank_bm25 import BM25Okapi

from app.rag.chunker import Chunk
from app.rag.embedder import tokenize

logger = logging.getLogger(__name__)


class BM25Index:
    """基于 rank_bm25 的内存索引，线程安全重建。"""

    def __init__(self) -> None:
        self._ids: list[str] = []
        self._index: BM25Okapi | None = None
        self._lock = threading.Lock()

    def build(self, chunks: list[Chunk]) -> int:
        with self._lock:
            if not chunks:
                self._ids, self._index = [], None
                return 0

            corpus = [tokenize(chunk.text) for chunk in chunks]
            # 全空 corpus（理论上不会）会让 BM25Okapi 除零
            if not any(corpus):
                logger.warning("BM25 语料全为空，索引置空")
                self._ids, self._index = [], None
                return 0

            self._ids = [chunk.id for chunk in chunks]
            self._index = BM25Okapi(corpus)
            return len(self._ids)

    def search(self, query: str, top_k: int = 20) -> list[tuple[str, float]]:
        """返回 (chunk_id, bm25_score)，按分数降序。"""
        if self._index is None or not self._ids:
            return []

        tokens = tokenize(query)
        if not tokens:
            return []

        scores = self._index.get_scores(tokens)
        ranked = sorted(
            zip(self._ids, (float(s) for s in scores)),
            key=lambda pair: pair[1],
            reverse=True,
        )
        # 分数为 0 表示没有任何词命中，属于噪声，直接丢掉
        return [(cid, score) for cid, score in ranked[:top_k] if score > 0]

    def score_map(self, query: str) -> dict[str, float]:
        """全语料的 {chunk_id: bm25_score}，不做截断也不丢 0 分。

        精排阶段需要给一批**已经召回好的**候选打分，这些候选不一定是 BM25 的 top-k；
        而 `search()` 只回 top-k 且滤掉了 0 分，拿不到候选全集。所以单开一个口子。
        """
        if self._index is None or not self._ids:
            return {}

        tokens = tokenize(query)
        if not tokens:
            return {}

        scores = self._index.get_scores(tokens)
        return {cid: float(s) for cid, s in zip(self._ids, scores)}

    @property
    def size(self) -> int:
        return len(self._ids)


_index: BM25Index | None = None


def get_bm25_index() -> BM25Index:
    global _index
    if _index is None:
        _index = BM25Index()
    return _index


def reset_bm25_index() -> None:
    global _index
    _index = None
