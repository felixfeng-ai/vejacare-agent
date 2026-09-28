"""全局配置。

所有外部依赖（LLM / Embedding / 向量库 / Reranker）都通过这里的环境变量切换实现，
业务代码只依赖抽象接口，不关心底层是哪家服务。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = BACKEND_DIR / "data" / "knowledge"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        # 必须显式声明 utf-8：Windows 默认 GBK，中文注释会直接读崩
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 应用 ----------
    app_name: str = "VeyaCare"
    debug: bool = True
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ---------- 数据库 ----------
    database_url: str = "sqlite+aiosqlite:///./vejacare.db"

    # ---------- LLM ----------
    llm_provider: str = "mock"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_classifier_model: str = ""
    llm_temperature_classify: float = 0.0
    llm_temperature_generate: float = 0.3

    # ---------- Embedding ----------
    embedding_provider: str = "hashing"
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-v4"
    embedding_dim: int = 0

    # ---------- 向量库 ----------
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    qdrant_collection: str = "vejacare_kb"

    # ---------- 检索 ----------
    retrieve_top_k: int = 20
    rerank_top_n: int = 5
    rrf_k: int = 60
    chunk_size: int = 420
    chunk_overlap: int = 80

    # ---------- Reranker ----------
    rerank_provider: str = "none"
    rerank_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    rerank_api_key: str = ""
    rerank_model: str = "gte-rerank-v2"

    # ---------- 转人工策略 ----------
    escalation_max_unresolved_turns: int = 2
    escalation_max_dissatisfaction: int = 2

    # ---------- 派生属性 ----------
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def classifier_model(self) -> str:
        """分类任务优先用单独的（更便宜更快的）模型。"""
        return self.llm_classifier_model or self.llm_model

    @property
    def is_mock_llm(self) -> bool:
        return self.llm_provider.lower() == "mock"

    @property
    def is_offline(self) -> bool:
        """离线模式：LLM 与 Embedding 都不需要外网。"""
        return self.is_mock_llm and self.embedding_provider.lower() == "hashing"

    @field_validator("chunk_overlap")
    @classmethod
    def _overlap_less_than_size(cls, v: int, info) -> int:
        size = info.data.get("chunk_size", 420)
        if v >= size:
            raise ValueError(f"CHUNK_OVERLAP({v}) 必须小于 CHUNK_SIZE({size})")
        return v

    @field_validator("rerank_top_n")
    @classmethod
    def _top_n_not_exceed_top_k(cls, v: int, info) -> int:
        top_k = info.data.get("retrieve_top_k", 20)
        if v > top_k:
            raise ValueError(f"RERANK_TOP_N({v}) 不能大于 RETRIEVE_TOP_K({top_k})")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
