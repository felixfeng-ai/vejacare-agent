"""向量存储与分块仓库。

两个东西刻意分开：
- `ChunkRepository`：分块正文的**唯一真相源**（chunks.jsonl），BM25 和结果展示都读它
- `VectorStore`：只负责「向量 → id + 分数」，Qdrant 是生产实现，LocalVectorStore 是零依赖降级

这样换向量库不需要动 BM25，也不需要动任何业务代码。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from app.config import BACKEND_DIR, Settings, get_settings
from app.rag.chunker import Chunk

logger = logging.getLogger(__name__)

INDEX_DIR = BACKEND_DIR / "data" / "index"
CHUNKS_FILE = INDEX_DIR / "chunks.jsonl"
VECTORS_FILE = INDEX_DIR / "vectors.npz"


# ---------------------------------------------------------------- 分块仓库


class ChunkRepository:
    """内存态分块表，落盘为 jsonl。数据量级（百级 chunk）不值得上数据库。"""

    def __init__(self, path: Path = CHUNKS_FILE) -> None:
        self.path = path
        self._chunks: dict[str, Chunk] = {}

    def load(self) -> int:
        self._chunks.clear()
        if not self.path.exists():
            return 0

        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                chunk = Chunk(
                    id=payload["id"],
                    text=payload["text"],
                    title=payload.get("title", ""),
                    source=payload.get("source", ""),
                    heading_path=payload.get("heading_path", ""),
                    index=payload.get("index", 0),
                    metadata=payload.get("metadata", {}),
                )
                self._chunks[chunk.id] = chunk
        return len(self._chunks)

    def save(self, chunks: list[Chunk]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as fh:
            for chunk in chunks:
                fh.write(json.dumps(chunk.to_payload(), ensure_ascii=False) + "\n")
        self._chunks = {c.id: c for c in chunks}

    def get(self, chunk_id: str) -> Chunk | None:
        return self._chunks.get(chunk_id)

    def all(self) -> list[Chunk]:
        return list(self._chunks.values())

    def __len__(self) -> int:
        return len(self._chunks)


# ---------------------------------------------------------------- 向量库协议


class VectorStore(Protocol):
    backend: str

    async def ensure_collection(self, dim: int) -> None: ...

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int: ...

    async def search(self, vector: list[float], top_k: int) -> list[tuple[str, float]]: ...

    async def count(self) -> int: ...

    async def is_ready(self) -> bool: ...


# ---------------------------------------------------------------- 本地降级实现


class LocalVectorStore:
    """numpy 暴力检索。

    分块数量在几千以内时，一次全量点积是亚毫秒级的，完全够用；
    再大就该换 Qdrant 了——这正是它作为降级方案而非生产方案的边界。
    """

    backend = "local"

    def __init__(self, path: Path = VECTORS_FILE) -> None:
        self.path = path
        self._ids: list[str] = []
        self._matrix: np.ndarray | None = None

    async def ensure_collection(self, dim: int) -> None:
        self._dim = dim

    def load(self) -> int:
        if not self.path.exists():
            return 0
        with np.load(self.path, allow_pickle=False) as data:
            self._ids = [str(i) for i in data["ids"]]
            self._matrix = data["vectors"].astype(np.float32)
        return len(self._ids)

    def save(self) -> None:
        if self._matrix is None or not self._ids:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 同 ingest.EmbeddingCache：定长字符串数组，不能用 dtype=object，
        # 否则读取端在 allow_pickle=False 下取不回来
        np.savez_compressed(
            self.path,
            ids=np.array(self._ids),
            vectors=self._matrix.astype(np.float32),
        )

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        if not chunks:
            return 0

        incoming = np.asarray(vectors, dtype=np.float32)
        new_ids = [c.id for c in chunks]

        existing = {cid: i for i, cid in enumerate(self._ids)}
        for cid in new_ids:
            if cid not in existing:
                existing[cid] = len(self._ids)
                self._ids.append(cid)

        merged = np.zeros((len(self._ids), incoming.shape[1]), dtype=np.float32)
        if self._matrix is not None:
            merged[: self._matrix.shape[0]] = self._matrix

        for row, cid in zip(incoming, new_ids):
            merged[existing[cid]] = row

        self._matrix = merged
        return len(chunks)

    async def search(self, vector: list[float], top_k: int) -> list[tuple[str, float]]:
        if self._matrix is None or not self._ids:
            return []

        query = np.asarray(vector, dtype=np.float32)
        # 向量都已 L2 归一化，点积即余弦相似度
        scores = self._matrix @ query
        k = min(top_k, len(scores))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [(self._ids[i], float(scores[i])) for i in top]

    async def count(self) -> int:
        return len(self._ids)

    async def is_ready(self) -> bool:
        return True


# ---------------------------------------------------------------- Qdrant 实现


class QdrantVectorStore:
    """Qdrant 生产实现。失败时由上层 fallback 到 LocalVectorStore。"""

    backend = "qdrant"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.collection = settings.qdrant_collection
        self._client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            from qdrant_client import AsyncQdrantClient

            self._client = AsyncQdrantClient(
                url=self.settings.qdrant_url,
                api_key=self.settings.qdrant_api_key or None,
                timeout=20.0,
            )
        return self._client

    async def ensure_collection(self, dim: int) -> None:
        from qdrant_client.models import Distance, VectorParams

        if await self.client.collection_exists(self.collection):
            return
        await self.client.create_collection(
            collection_name=self.collection,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        # 正文过滤（按来源、按生效状态）走 payload 索引，避免全表扫
        for field_name, schema in (
            ("source", "keyword"),
            ("heading_path", "keyword"),
        ):
            try:
                await self.client.create_payload_index(self.collection, field_name, schema)
            except Exception as exc:  # noqa: BLE001 — 索引重复创建会报错，忽略即可
                logger.debug("创建 payload 索引 %s 跳过：%s", field_name, exc)

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        from qdrant_client.models import PointStruct

        points = [
            PointStruct(id=_stable_point_id(chunk.id), vector=vec, payload=chunk.to_payload())
            for chunk, vec in zip(chunks, vectors)
        ]
        await self.client.upsert(collection_name=self.collection, points=points, wait=True)
        return len(points)

    async def search(self, vector: list[float], top_k: int) -> list[tuple[str, float]]:
        response = await self.client.query_points(
            collection_name=self.collection,
            query=vector,
            limit=top_k,
            with_payload=True,
        )
        return [
            (str(point.payload.get("id", point.id)), float(point.score))
            for point in response.points
        ]

    async def count(self) -> int:
        result = await self.client.count(collection_name=self.collection, exact=True)
        return int(result.count)

    async def is_ready(self) -> bool:
        try:
            await self.client.get_collections()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Qdrant 不可用：%s", exc)
            return False


def _stable_point_id(chunk_id: str) -> int:
    """Qdrant 只接受 int/UUID 作为 point id，这里把字符串稳定映射成 63 位整数。"""
    import hashlib

    digest = hashlib.blake2b(chunk_id.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") >> 1


# ---------------------------------------------------------------- 工厂

_store: VectorStore | None = None


def build_store(settings: Settings | None = None) -> VectorStore:
    s = settings or get_settings()
    if s.qdrant_url:
        return QdrantVectorStore(s)
    return LocalVectorStore()


async def get_store() -> VectorStore:
    """返回可用的向量库。配置了 Qdrant 但连不上时自动降级到本地，并打警告。"""
    global _store
    if _store is not None:
        return _store

    settings = get_settings()
    candidate = build_store(settings)

    if isinstance(candidate, QdrantVectorStore) and not await candidate.is_ready():
        logger.warning("QDRANT_URL 已配置但连接失败，降级为本地向量库（仅影响本次进程）")
        candidate = LocalVectorStore()

    _store = candidate
    return _store


def reset_store() -> None:
    global _store
    _store = None
