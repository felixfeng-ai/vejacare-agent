"""Reranker 精排层。

召回阶段（向量 top-20 + BM25 top-20）追求高召回，必然带噪声；
精排阶段用交叉编码器逐条打分，把真正相关的顶到前面。

RERANK_PROVIDER=none 时退化为「不精排」，直接用 RRF 融合序——
这是完全可接受的默认值：混合检索本身已经把准确率拉起来了，reranker 是锦上添花。
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from app.config import Settings, get_settings
from app.graph.state import KbDoc

logger = logging.getLogger(__name__)


class Reranker(Protocol):
    provider: str

    async def rerank(self, query: str, docs: list[KbDoc], top_n: int) -> list[KbDoc]: ...


class NoopReranker:
    """不精排：保留上游（RRF）顺序，只做截断。"""

    provider = "none"

    async def rerank(self, query: str, docs: list[KbDoc], top_n: int) -> list[KbDoc]:
        return docs[:top_n]


class _HttpReranker:
    """DashScope / Cohere 等 HTTP 精排服务的公共部分。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.rerank_api_key}",
            "Content-Type": "application/json",
        }

    def _endpoint(self) -> str:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def _body(self, query: str, documents: list[str], top_n: int) -> dict:
        return {
            "model": self.settings.rerank_model,
            "query": query,
            "documents": documents,
            "top_n": top_n,
        }

    def _parse(self, payload: dict) -> list[dict]:  # pragma: no cover - 子类实现
        raise NotImplementedError

    async def rerank(self, query: str, docs: list[KbDoc], top_n: int) -> list[KbDoc]:
        if not docs:
            return []
        if not self.settings.rerank_api_key:
            logger.warning("RERANK_PROVIDER=%s 但未配置 RERANK_API_KEY，跳过精排", self.provider)
            return docs[:top_n]

        try:
            resp = await self.client.post(
                self._endpoint(),
                headers=self._headers(),
                json=self._body(query, [d.get("text", "") for d in docs], top_n),
            )
            resp.raise_for_status()
            results = self._parse(resp.json())
        except Exception as exc:  # noqa: BLE001 — 精排是增强项，失败不能拖垮检索
            logger.error("精排失败，回退到融合序：%s", exc)
            return docs[:top_n]

        reranked: list[KbDoc] = []
        for item in results:
            idx = int(item.get("index", -1))
            if 0 <= idx < len(docs):
                merged = dict(docs[idx])
                merged["rerank_score"] = float(item.get("relevance_score", 0.0))
                reranked.append(merged)  # type: ignore[arg-type]

        return reranked[:top_n] or docs[:top_n]


class DashScopeReranker(_HttpReranker):
    provider = "dashscope"

    def _endpoint(self) -> str:
        return f"{self.settings.rerank_base_url.rstrip('/')}/services/rerank/text-rerank/text-rerank"

    def _body(self, query: str, documents: list[str], top_n: int) -> dict:
        return {
            "model": self.settings.rerank_model,
            "input": {"query": query, "documents": documents},
            "parameters": {"top_n": top_n, "return_documents": False},
        }

    def _parse(self, payload: dict) -> list[dict]:
        return payload.get("output", {}).get("results", [])


class CohereReranker(_HttpReranker):
    provider = "cohere"

    def _endpoint(self) -> str:
        base = self.settings.rerank_base_url.rstrip("/") or "https://api.cohere.com"
        return f"{base}/v2/rerank"

    def _parse(self, payload: dict) -> list[dict]:
        return payload.get("results", [])


class LocalReranker:
    """本地 bge-reranker。需要额外装 sentence-transformers，没装就自动退化为不精排。"""

    provider = "local"

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or "BAAI/bge-reranker-base"
        self._model = None
        self._fallback = NoopReranker()

    def _load(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder  # 可选依赖

            self._model = CrossEncoder(self.model_name)
        return self._model

    async def rerank(self, query: str, docs: list[KbDoc], top_n: int) -> list[KbDoc]:
        try:
            model = self._load()
        except ImportError:
            logger.warning("未安装 sentence-transformers，本地精排退化为不精排")
            return await self._fallback.rerank(query, docs, top_n)

        import asyncio

        pairs = [(query, d.get("text", "")) for d in docs]
        # CrossEncoder 是同步阻塞的，丢到线程池里跑，别卡住事件循环
        scores = await asyncio.to_thread(model.predict, pairs)

        merged = [dict(d, rerank_score=float(s)) for d, s in zip(docs, scores)]
        merged.sort(key=lambda d: d["rerank_score"], reverse=True)  # type: ignore[arg-type,return-value]
        return merged[:top_n]  # type: ignore[return-value]


_reranker: Reranker | None = None


def build_reranker(settings: Settings | None = None) -> Reranker:
    s = settings or get_settings()
    provider = s.rerank_provider.lower()

    if provider in {"", "none", "off"}:
        return NoopReranker()
    if provider == "dashscope":
        return DashScopeReranker(s)
    if provider == "cohere":
        return CohereReranker(s)
    if provider == "local":
        return LocalReranker(s.rerank_model)

    logger.warning("未知的 RERANK_PROVIDER=%s，退化为不精排", provider)
    return NoopReranker()


def get_reranker() -> Reranker:
    global _reranker
    if _reranker is None:
        _reranker = build_reranker()
    return _reranker


def reset_reranker() -> None:
    global _reranker
    _reranker = None
