"""健康检查。

每个外部依赖单独探活——出问题时一眼能看出是哪一层挂了，
而不是只知道"服务不健康"。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from sqlalchemy import text

from app.config import get_settings
from app.db.session import get_engine
from app.rag.bm25 import get_bm25_index
from app.rag.embedder import get_embedder
from app.rag.ingest import read_manifest
from app.rag.rerank import get_reranker
from app.rag.store import get_store
from app.schemas import ok

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["health"])


async def _check_database() -> dict:
    backend = get_settings().database_url.split("://", 1)[0]
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"backend": backend, "ok": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("数据库探活失败：%s", exc)
        return {"backend": backend, "ok": False, "error": str(exc)}


async def _check_vector_store() -> dict:
    try:
        store = await get_store()
        return {
            "backend": store.backend,
            "collection": get_settings().qdrant_collection,
            "points": await store.count(),
            "ok": await store.is_ready(),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("向量库探活失败：%s", exc)
        return {"backend": "unknown", "points": 0, "ok": False, "error": str(exc)}


@router.get("/health")
async def health() -> dict:
    settings = get_settings()
    embedder = get_embedder()
    reranker = get_reranker()
    manifest = read_manifest()

    return ok(
        {
            "status": "ok",
            "app": settings.app_name,
            # 离线演示模式：LLM 与 Embedding 都不走外网，回答来自模板而非模型
            "offline_demo": settings.is_offline,
            "llm": {
                "provider": settings.llm_provider,
                "model": settings.llm_model,
                "classifier_model": settings.classifier_model,
                "ok": settings.is_mock_llm or bool(settings.llm_api_key),
                "mock": settings.is_mock_llm,
            },
            "embedding": {
                "provider": embedder.provider,
                "model": embedder.model,
                "dim": embedder.dim,
                "ok": embedder.is_ready(),
            },
            "vector_store": await _check_vector_store(),
            "reranker": {"provider": reranker.provider, "ok": True},
            "bm25": {"points": get_bm25_index().size, "ok": get_bm25_index().size > 0},
            "knowledge": {
                "chunks": (manifest.get("report") or {}).get("chunks", 0),
                "documents": (manifest.get("report") or {}).get("documents", 0),
                "last_ingest": sorted((manifest.get("fingerprints") or {}).keys()),
            },
            "escalation_policy": {
                "max_unresolved_turns": settings.escalation_max_unresolved_turns,
                "max_dissatisfaction": settings.escalation_max_dissatisfaction,
            },
            "retrieval": {
                "top_k": settings.retrieve_top_k,
                "rerank_top_n": settings.rerank_top_n,
                "chunk_size": settings.chunk_size,
                "chunk_overlap": settings.chunk_overlap,
            },
            "database": await _check_database(),
        }
    )
