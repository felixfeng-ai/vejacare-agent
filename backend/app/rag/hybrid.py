"""混合检索：向量召回 + BM25 召回 → RRF 融合 → Reranker 精排。

为什么是这套组合拳（面试常问）：
- 纯向量：能匹配"退款要多久"和"退款到账时间"，但对订单号、"DDP" 这类专有名词不敏感
- 纯 BM25：字面准，但用户换个说法就废（"钱什么时候退回来" 匹配不到 "退款时效"）
- 两者召回集合求并 + RRF 融合，取长补短；RRF 只看排名不看分数，天然免去两路分数量纲不一致的问题
- 融合后还有 40 条候选，再上交叉编码器精排到 5 条，把噪声压掉

多轮对话里用户会说"那它到哪了"，这种省略句直接检索必然跑偏，
所以前面加一步 query 改写，把它补全成独立可检索的问句。
"""

from __future__ import annotations

import logging

from app.config import Settings, get_settings
from app.graph.state import INTENT_LABELS, KbDoc, Slots
from app.llm import LLMClient, get_llm
from app.rag.bm25 import get_bm25_index
from app.rag.store import ChunkRepository, get_store
from app.rag.embedder import get_embedder
from app.rag.rerank import get_reranker

logger = logging.getLogger(__name__)

#: 意图 → 检索时的扩展词，弥补用户口语和政策文档书面语之间的鸿沟
INTENT_EXPANSION: dict[str, str] = {
    "logistics": "物流 包裹 时效 轨迹 清关 派送",
    "return_refund": "退货 退款 换货 运费 到账 无理由",
    "customs_duty": "关税 税费 清关 DDP DDU 申报",
    "size_fit": "尺码 尺码表 身高 体重 版型 建议",
    "payment": "支付 付款 扣款 失败 银行卡 重复扣款",
    "coupon": "优惠券 折扣码 叠加 使用规则 过期",
    "order_change": "订单 修改 取消 地址 合并 发货前",
    "human_agent": "",
    "chitchat": "",
}

QUERY_REWRITE_SYSTEM = """你是检索查询改写器。把用户在**多轮对话**里的最后一句话，改写成一句**独立可检索**的完整问句。

要求：
- 补全指代词（它/这个/那个 → 具体指什么）
- 保留所有数字、订单号、专有名词，一个都不能改
- 只输出改写后的问句本身，不要引号、不要解释
- 如果最后一句话本身已经完整，原样返回"""


class HybridRetriever:
    def __init__(self, settings: Settings | None = None, llm: LLMClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.llm = llm or get_llm()
        self.repo = ChunkRepository()

    # ------------------------------------------------------------ query 构造

    async def rewrite_query(self, question: str, history: list[str]) -> str:
        """多轮指代消解。无历史时不做任何事，省一次 LLM 调用。"""
        if not history or self.settings.is_mock_llm:
            # 离线模式下退化为「上一轮用户话 + 本轮」的朴素拼接
            return f"{history[-1]} {question}".strip() if history else question

        prompt = "上文对话：\n" + "\n".join(f"- {h}" for h in history[-4:]) + f"\n\n最后一句话：{question}"
        try:
            rewritten = await self.llm.text(
                task="query_rewrite",
                system=QUERY_REWRITE_SYSTEM,
                user=prompt,
                hints={"question": question},
            )
            return rewritten.strip() or question
        except Exception as exc:  # noqa: BLE001 — 改写失败就用原句，不影响主链路
            logger.warning("query 改写失败，回退原句：%s", exc)
            return question

    def expand_query(self, query: str, intent: str, slots: Slots) -> str:
        """把意图扩展词与已抽到的槽位拼进检索 query。"""
        parts = [query, INTENT_EXPANSION.get(intent, "")]
        parts.extend(str(v) for v in slots.values() if v)
        return " ".join(p for p in parts if p).strip()

    # ------------------------------------------------------------ 检索主流程

    async def retrieve(
        self,
        *,
        question: str,
        intent: str,
        slots: Slots | None = None,
        history: list[str] | None = None,
    ) -> list[KbDoc]:
        slots = slots or Slots()
        history = history or []

        if len(self.repo) == 0:
            self.repo.load()
        if len(self.repo) == 0:
            logger.warning("知识库为空，请先执行 `python -m scripts.ingest`")
            return []

        rewritten = await self.rewrite_query(question, history)
        query = self.expand_query(rewritten, intent, slots)

        top_k = self.settings.retrieve_top_k
        vector_hits = await self._vector_search(query, top_k)
        # BM25 返回 (id, score) 列表，这里统一转成 {id: 排名}，和向量路对齐后才能做 RRF
        bm25_hits = {
            chunk_id: rank
            for rank, (chunk_id, _score) in enumerate(
                get_bm25_index().search(query, top_k), start=1
            )
        }

        fused = self._rrf_fuse(vector_hits, bm25_hits)
        if not fused:
            return []

        candidates: list[KbDoc] = []
        for chunk_id, fused_score in fused:
            chunk = self.repo.get(chunk_id)
            if chunk is None:
                continue
            candidates.append(
                KbDoc(
                    id=chunk.id,
                    title=chunk.title,
                    source=chunk.source,
                    text=chunk.text,
                    snippet=_make_snippet(chunk.text),
                    score=fused_score,
                    vector_rank=vector_hits.get(chunk_id),
                    bm25_rank=bm25_hits.get(chunk_id),
                )
            )

        reranked = await get_reranker().rerank(
            rewritten, candidates, self.settings.rerank_top_n
        )
        return reranked

    async def _vector_search(self, query: str, top_k: int) -> dict[str, int]:
        """返回 {chunk_id: 排名}，排名从 1 开始。"""
        try:
            vector = await get_embedder().embed_query(query)
            store = await get_store()
            hits = await store.search(vector, top_k)
        except Exception as exc:  # noqa: BLE001 — 向量路挂了还有 BM25 路，不能整体失败
            logger.error("向量检索失败，本轮退化为纯 BM25：%s", exc)
            return {}

        return {chunk_id: rank for rank, (chunk_id, _score) in enumerate(hits, start=1)}

    @staticmethod
    def _rrf_fuse(
        vector_hits: dict[str, int], bm25_hits: dict[str, int], k: int | None = None
    ) -> list[tuple[str, float]]:
        """Reciprocal Rank Fusion。只看排名，天然免去两路分数量纲不一致的问题。"""
        rrf_k = k or get_settings().rrf_k
        scores: dict[str, float] = {}

        for ranks in (vector_hits, bm25_hits):
            for chunk_id, rank in ranks.items():
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (rrf_k + rank)

        return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


def _make_snippet(text: str, limit: int = 90) -> str:
    """给前端展示用的短摘要：去掉标题前缀，截到 limit 字。"""
    body = text
    if body.startswith("[") and "]" in body:
        body = body.split("]", 1)[1].strip()
    body = body.replace("\n", " ").strip()
    return body[:limit] + ("…" if len(body) > limit else "")


def intent_label(intent: str) -> str:
    return INTENT_LABELS.get(intent, "其他")


_retriever: HybridRetriever | None = None


def get_retriever() -> HybridRetriever:
    global _retriever
    if _retriever is None:
        _retriever = HybridRetriever()
    return _retriever


def reset_retriever() -> None:
    global _retriever
    _retriever = None
