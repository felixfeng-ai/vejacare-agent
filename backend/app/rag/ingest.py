"""知识库离线入库。

流程：读 markdown → 解析 frontmatter → 分块 → embedding（带内容哈希缓存）→ 写向量库 + BM25。

关于「文档版本管理」的取舍：
分块 id 形如 `logistics.md::7`，文件中间插一段就会导致后续 id 全部错位，
增量 upsert 会留下一堆孤儿向量。所以这里**每次全量重建索引**保证正确性，
但加了内容哈希级别的 embedding 缓存——文本没变的 chunk 不会重复调 embedding 接口。
索引重建是毫秒级的，embedding 调用才是成本和延迟所在，这个组合拿到了两头的好处。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.config import KNOWLEDGE_DIR, Settings, get_settings
from app.rag.bm25 import BM25Index
from app.rag.chunker import Chunk, load_documents
from app.rag.embedder import Embedder, get_embedder
from app.rag.store import INDEX_DIR, ChunkRepository, VectorStore, get_store

logger = logging.getLogger(__name__)

CACHE_FILE = INDEX_DIR / "embedding_cache.npz"


# ---------------------------------------------------------------- frontmatter


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """极简 frontmatter 解析。只支持 `key: value` 标量，够用且不引入 YAML 依赖。"""
    if not text.startswith("---"):
        return {}, text

    lines = text.splitlines()
    end = next((i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if end is None:
        return {}, text

    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip("\"'")

    return meta, "\n".join(lines[end + 1 :])


# ---------------------------------------------------------------- embedding 缓存


class EmbeddingCache:
    """内容哈希 → 向量。避免每次重建索引都重新调一遍 embedding 接口。"""

    def __init__(self, path: Path = CACHE_FILE) -> None:
        self.path = path
        self._map: dict[str, list[float]] = {}

    def load(self) -> int:
        if not self.path.exists():
            return 0
        try:
            with np.load(self.path, allow_pickle=False) as data:
                keys = [str(k) for k in data["keys"]]
                matrix = data["vectors"].astype(np.float32)
            self._map = {k: matrix[i].tolist() for i, k in enumerate(keys)}
        except Exception as exc:  # noqa: BLE001 — 缓存损坏就当没有，重新算
            logger.warning("embedding 缓存读取失败，将全量重算：%s", exc)
            self._map = {}
        return len(self._map)

    def save(self) -> None:
        if not self._map:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        keys = list(self._map.keys())
        matrix = np.asarray([self._map[k] for k in keys], dtype=np.float32)
        # 必须存成 numpy 的定长字符串数组（dtype '<U..'）。
        # 用 dtype=object 的话，读取端 allow_pickle=False 会直接报错——
        # 而 allow_pickle=True 反序列化外部文件是明确的安全风险，不能开。
        np.savez_compressed(self.path, keys=np.array(keys), vectors=matrix)

    def get(self, text: str) -> list[float] | None:
        return self._map.get(_hash_text(text))

    def put(self, text: str, vector: list[float]) -> None:
        self._map[_hash_text(text)] = vector


def _hash_text(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


# ---------------------------------------------------------------- 报告


@dataclass
class IngestReport:
    documents: int = 0
    chunks: int = 0
    embedded: int = 0          # 实际调了 embedding 接口的条数
    cache_hits: int = 0
    skipped_deprecated: int = 0
    changed_documents: list[str] = field(default_factory=list)
    vector_backend: str = ""
    bm25_size: int = 0

    def to_dict(self) -> dict:
        return {
            "documents": self.documents,
            "chunks": self.chunks,
            "embedded": self.embedded,
            "cache_hits": self.cache_hits,
            "skipped_deprecated": self.skipped_deprecated,
            "changed_documents": self.changed_documents,
            "vector_backend": self.vector_backend,
            "bm25_size": self.bm25_size,
        }

    def summary(self) -> str:
        return (
            f"文档 {self.documents} 篇 / 分块 {self.chunks} 条 / "
            f"新算向量 {self.embedded} 条（缓存命中 {self.cache_hits}）"
            f"{f' / 跳过失效文档 {self.skipped_deprecated} 条' if self.skipped_deprecated else ''} "
            f"→ 向量库 {self.vector_backend}，BM25 索引 {self.bm25_size} 条"
        )


# ---------------------------------------------------------------- 主流程


def _collect_chunks(
    knowledge_dir: Path, settings: Settings, report: IngestReport
) -> tuple[list[Chunk], dict[str, str]]:
    """读取文档并切块，顺带记录每篇的版本指纹。"""
    if not knowledge_dir.exists():
        return [], {}

    from app.rag.chunker import chunk_markdown

    chunks: list[Chunk] = []
    fingerprints: dict[str, str] = {}

    for path in sorted(knowledge_dir.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(raw)
        fingerprints[path.name] = _hash_text(raw)

        if meta.get("status", "active").lower() in {"deprecated", "archived", "失效"}:
            report.skipped_deprecated += 1
            logger.info("跳过失效文档：%s（status=%s）", path.name, meta.get("status"))
            continue

        report.documents += 1
        doc_title = meta.get("title") or path.stem

        for chunk in chunk_markdown(
            body,
            source=path.name,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            doc_title=doc_title,
        ):
            chunk.metadata = {
                "doc_title": doc_title,
                "doc_version": meta.get("version", "1.0"),
                "status": meta.get("status", "active"),
                "effective_from": meta.get("effective_from", ""),
                "effective_until": meta.get("effective_until", ""),
                "doc_hash": fingerprints[path.name],
            }
            chunks.append(chunk)

    return chunks, fingerprints


async def _embed_chunks(
    chunks: list[Chunk], embedder: Embedder, cache: EmbeddingCache, report: IngestReport
) -> list[list[float]]:
    """只对缓存里没有的文本调 embedding 接口。"""
    pending_texts: list[str] = []
    pending_index: list[int] = []
    vectors: list[list[float] | None] = [None] * len(chunks)

    for i, chunk in enumerate(chunks):
        cached = cache.get(chunk.text)
        if cached is not None:
            vectors[i] = cached
            report.cache_hits += 1
        else:
            pending_texts.append(chunk.text)
            pending_index.append(i)

    if pending_texts:
        logger.info("需要新算向量的分块：%d 条", len(pending_texts))
        fresh = await embedder.embed_documents(pending_texts)
        for idx, text, vector in zip(pending_index, pending_texts, fresh):
            vectors[idx] = vector
            cache.put(text, vector)
        report.embedded = len(pending_texts)

    missing = [i for i, v in enumerate(vectors) if v is None]
    if missing:
        raise RuntimeError(f"有 {len(missing)} 条分块未拿到向量，入库中止")

    return [v for v in vectors if v is not None]  # type: ignore[misc]


async def ingest(
    knowledge_dir: Path | None = None,
    settings: Settings | None = None,
    store: VectorStore | None = None,
    embedder: Embedder | None = None,
    previous_fingerprints: dict[str, str] | None = None,
) -> IngestReport:
    """重建整个知识库索引。幂等，可反复执行。"""
    settings = settings or get_settings()
    directory = knowledge_dir or KNOWLEDGE_DIR
    embedder = embedder or get_embedder()
    store = store or await get_store()

    report = IngestReport()
    chunks, fingerprints = _collect_chunks(directory, settings, report)
    report.chunks = len(chunks)

    if previous_fingerprints:
        report.changed_documents = sorted(
            name
            for name, digest in fingerprints.items()
            if previous_fingerprints.get(name) != digest
        )

    if not chunks:
        logger.warning("知识库目录 %s 下没有可用文档", directory)
        ChunkRepository().save([])
        return report

    cache = EmbeddingCache()
    cache.load()
    vectors = await _embed_chunks(chunks, embedder, cache, report)
    cache.save()

    dim = len(vectors[0])
    await store.ensure_collection(dim)
    await store.upsert(chunks, vectors)
    report.vector_backend = store.backend

    # 本地向量库需要手动落盘，Qdrant 落盘由服务端负责
    if hasattr(store, "save"):
        store.save()  # type: ignore[attr-defined]

    repo = ChunkRepository()
    repo.save(chunks)

    index = BM25Index()
    report.bm25_size = index.build(chunks)
    _persist_bm25(index, chunks)

    _write_manifest(fingerprints, report)
    return report


# ---------------------------------------------------------------- 进程内索引热加载


def _persist_bm25(index: BM25Index, chunks: list[Chunk]) -> None:
    """把 BM25 索引塞进全局单例，让 API 进程无需重启即可用上新数据。

    跨进程时（API 与 ingest 是两个进程）靠的是启动时从 chunks.jsonl 重建，
    见 `warmup()`。
    """
    from app.rag import bm25 as bm25_module

    bm25_module._index = index  # noqa: SLF001 — 模块级单例，这里就是它的装配点


def _write_manifest(fingerprints: dict[str, str], report: IngestReport) -> None:
    """记录本次入库的文档指纹，供下次 ingest 比对出「哪些文档变了」。"""
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    (INDEX_DIR / "manifest.json").write_text(
        json.dumps(
            {
                "fingerprints": fingerprints,
                "report": report.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def read_manifest() -> dict:
    path = INDEX_DIR / "manifest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


async def warmup() -> dict:
    """API 启动时调用：把分块与 BM25 索引加载进内存。

    向量库不预热（Qdrant 在远端，本地向量库按需 mmap），只加载文本侧。
    """
    repo = ChunkRepository()
    count = repo.load()

    if count:
        index = BM25Index()
        index.build(repo.all())
        _persist_bm25(index, repo.all())
        logger.info("检索索引预热完成：%d 条分块", count)
    else:
        logger.warning("知识库为空，请先运行：python -m scripts.ingest")

    # 本地向量库从磁盘恢复
    store = await get_store()
    if hasattr(store, "load"):
        store.load()  # type: ignore[attr-defined]

    return {"chunks": count, "backend": store.backend}


def run_ingest_sync(**kwargs) -> IngestReport:
    """给 CLI 用的同步包装。"""
    return asyncio.run(ingest(**kwargs))
