"""Embedding 抽象层。

支持三种来源：
- OpenAI 兼容接口（OpenAI / DashScope / 任意兼容端点），生产用
- 本地 sentence-transformers（未安装依赖时自动跳过，不写进 requirements 以免拖累部署）
- hashing：确定性哈希向量，**只用于离线测试与无网络演示**，不具备语义泛化能力
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Protocol

import numpy as np
from openai import AsyncOpenAI

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: 中文字符 bigram + 英文/数字单词
_TOKEN_RE = re.compile(r"[a-zA-Z]+|\d+")
_CJK_RE = re.compile(r"[一-鿿]")


class Embedder(Protocol):
    """所有实现都必须给出确定的 dim，并在 embed_* 里返回等长向量。"""

    provider: str
    model: str
    dim: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...

    def is_ready(self) -> bool: ...


# ---------------------------------------------------------------- 分词


def tokenize(text: str) -> list[str]:
    """中英混排分词。中文出字符 bigram（无词典依赖，召回足够），英文出小写单词。"""
    lowered = text.lower()
    tokens: list[str] = _TOKEN_RE.findall(lowered)

    cjk_chars = _CJK_RE.findall(lowered)
    tokens.extend(cjk_chars)
    tokens.extend(a + b for a, b in zip(cjk_chars, cjk_chars[1:]))

    return tokens


# ---------------------------------------------------------------- hashing


class HashingEmbedder:
    """签名字符串哈希向量。

    同样的 token 落同样的维度、同样的符号，所以重叠越多的两段文本余弦相似度越高。
    它做不了「同义词」层面的语义匹配——离线自测够用，**不要**把它当成生产方案。
    """

    provider = "hashing"

    #: 512 维在「碰撞率」和「文件体积」之间比较平衡
    DEFAULT_DIM = 512

    def __init__(self, dim: int | None = None, model: str = "hashing-v1") -> None:
        self.dim = dim or self.DEFAULT_DIM
        self.model = model

    def is_ready(self) -> bool:
        return True

    def _embed_one(self, text: str) -> list[float]:
        vec = np.zeros(self.dim, dtype=np.float32)
        for token in tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[index] += sign

        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        return vec.tolist()

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed_one(text)


# ---------------------------------------------------------------- OpenAI 兼容


class OpenAICompatEmbedder:
    """任意 OpenAI 兼容的 /v1/embeddings 端点。"""

    #: 部分模型对单次请求的条数有限制，超出就分批
    BATCH_SIZE = 16

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.provider = settings.embedding_provider
        self.model = settings.embedding_model
        self._client: AsyncOpenAI | None = None
        self._dim = settings.embedding_dim or 0

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url=self.settings.embedding_base_url,
                api_key=self.settings.embedding_api_key or self.settings.llm_api_key or "not-needed",
                timeout=60.0,
                max_retries=2,
            )
        return self._client

    @property
    def dim(self) -> int:
        return self._dim

    @dim.setter
    def dim(self, value: int) -> None:
        self._dim = value

    def is_ready(self) -> bool:
        return bool(self.settings.embedding_api_key or self.settings.llm_api_key)

    async def _call(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.BATCH_SIZE):
            batch = texts[i : i + self.BATCH_SIZE]
            resp = await self.client.embeddings.create(model=self.model, input=batch)
            # 兼容端点不保证返回顺序，按 index 排回去
            ordered = sorted(resp.data, key=lambda d: d.index)
            out.extend([list(d.embedding) for d in ordered])

        if out and not self._dim:
            self._dim = len(out[0])
        return out

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._call(texts)

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._call([text])
        return vectors[0]

    async def probe_dim(self) -> int:
        """首次入库前探测真实维度，用于建 Qdrant collection。"""
        if not self._dim:
            await self._call(["维度探测"])
        return self._dim


# ---------------------------------------------------------------- 工厂

_embedder: Embedder | None = None


def build_embedder(settings: Settings | None = None) -> Embedder:
    s = settings or get_settings()
    provider = s.embedding_provider.lower()

    if provider == "hashing":
        return HashingEmbedder(dim=s.embedding_dim or None)

    if provider in {"openai", "dashscope", "qwen", "ollama", "compatible"}:
        return OpenAICompatEmbedder(s)

    raise ValueError(f"未知的 EMBEDDING_PROVIDER：{provider}")


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = build_embedder()
    return _embedder


def reset_embedder() -> None:
    """测试用：清掉单例，让下次调用重新读配置。"""
    global _embedder
    _embedder = None
