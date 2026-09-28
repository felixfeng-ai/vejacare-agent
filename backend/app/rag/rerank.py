"""Reranker 精排层。

召回阶段（向量 top-20 + BM25 top-20）追求高召回，必然带噪声；
精排阶段逐条打分，把真正相关的顶到前面。

三种实现：
- `local` / `dashscope` / `cohere` —— 交叉编码器，query 与 doc 拼在一起过模型，最准也最贵
- `none` —— 无外部模型：用**原始问句**的词面信号重排（`LexicalReranker`）。
  离线、无依赖、结果确定，可直接进单测与 CI
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


class LexicalReranker:
    """不用模型的精排：拿**原始问句**的词面信号重排候选。

    `provider` 仍写作 `none`（配置项不变），但语义从「不精排」纠正成了「不用模型精排」。
    这两件事过去被混为一谈，代价是 20 条候选里谁进前 5 完全交给 RRF 的名次决定，
    而 RRF 的 `1/(k+rank)` 在 k=60 时刻意压得很平——实测同一批候选里第 1 名与第 6 名
    只差 6%，截断点近乎随机，正确答案排第 6 就被无声丢掉。

    更要命的是**召回用的 query 和排序用的 query 不该是同一个**。
    召回时我们会把意图扩展词（`物流 包裹 时效 轨迹 清关 派送`）拼进去换覆盖率，
    这对召回是好事；但拿它排序，等于在给「恰好含这些泛化词的 chunk」投票——
    「包裹寄到德国一般几天」会把「轨迹长时间不更新」「派送失败」顶上来，
    真正写着德国时效的那条反被挤出前 5（实测名次 4 → 6）。
    所以这里只吃**未扩展的原始问句**（`retrieve()` 传进来的就是它）。

    打分复用 BM25 索引：候选集的 IDF 来自全语料，所以「德国」这类稀有词比
    「包裹」这类高频词权重高——这正是泛化扩展词会盖掉的那个信号。
    两路分数各自按全语料最大值归一后再加权，避免 RRF 的量纲（约 0.03）
    被 BM25 的量纲（0~20+）整个吞掉。
    """

    provider = "none"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def rerank(self, query: str, docs: list[KbDoc], top_n: int) -> list[KbDoc]:
        if len(docs) <= top_n:
            return docs[:top_n]

        from app.rag.bm25 import get_bm25_index

        lexical = get_bm25_index().score_map(query)
        if not lexical:
            return docs[:top_n]

        fused_max = max((float(d.get("score", 0.0)) for d in docs), default=0.0)
        lexical_max = max(lexical.values(), default=0.0)
        if fused_max <= 0 or lexical_max <= 0:
            return docs[:top_n]

        weight = self.settings.rerank_lexical_weight
        # sorted 是稳定排序：词面与 RRF 都打平时保持上游 RRF 的相对次序
        ordered = sorted(
            docs,
            key=lambda d: (
                float(d.get("score", 0.0)) / fused_max
                + weight * lexical.get(d["id"], 0.0) / lexical_max
            ),
            reverse=True,
        )
        return ordered[:top_n]


#: 兼容旧名字。语义已从「不精排」变为「无模型精排」，新代码请用 LexicalReranker。
NoopReranker = LexicalReranker


class _HttpReranker:
    """DashScope / Cohere 等 HTTP 精排服务的公共部分。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        #: 精排服务不可用时退回词面重排，而不是直接砍掉后 15 条
        self._fallback = LexicalReranker(settings)

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
            logger.warning(
                "RERANK_PROVIDER=%s 但未配置 RERANK_API_KEY，退回词面重排", self.provider
            )
            return await self._fallback.rerank(query, docs, top_n)

        try:
            resp = await self.client.post(
                self._endpoint(),
                headers=self._headers(),
                json=self._body(query, [d.get("text", "") for d in docs], top_n),
            )
            resp.raise_for_status()
            results = self._parse(resp.json())
        except Exception as exc:  # noqa: BLE001 — 精排是增强项，失败不能拖垮检索
            logger.error("精排失败，退回词面重排：%s", exc)
            return await self._fallback.rerank(query, docs, top_n)

        reranked: list[KbDoc] = []
        for item in results:
            idx = int(item.get("index", -1))
            if 0 <= idx < len(docs):
                merged = dict(docs[idx])
                merged["rerank_score"] = float(item.get("relevance_score", 0.0))
                reranked.append(merged)  # type: ignore[arg-type]

        if not reranked:
            return await self._fallback.rerank(query, docs, top_n)
        return reranked[:top_n]


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
        self._fallback = LexicalReranker()

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
        return LexicalReranker(s)
    if provider == "dashscope":
        return DashScopeReranker(s)
    if provider == "cohere":
        return CohereReranker(s)
    if provider == "local":
        return LocalReranker(s.rerank_model)

    logger.warning("未知的 RERANK_PROVIDER=%s，退化为无模型精排", provider)
    return LexicalReranker(s)


def get_reranker() -> Reranker:
    global _reranker
    if _reranker is None:
        _reranker = build_reranker()
    return _reranker


def reset_reranker() -> None:
    global _reranker
    _reranker = None
