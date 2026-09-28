"""测试夹具。

关键点：**在导入 app 之前**改写环境变量。
app.config.get_settings 是 lru_cache 的，各模块又有单例，
晚一步设就会拿到开发用的配置（真实的 .env、真实的数据库文件）。
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- 环境隔离（必须在 app 导入之前）
_TEST_DB = Path(__file__).resolve().parent.parent / "test_vejacare.db"

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB.as_posix()}"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["EMBEDDING_PROVIDER"] = "hashing"
os.environ["RERANK_PROVIDER"] = "none"
os.environ["QDRANT_URL"] = ""
os.environ["DEBUG"] = "false"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from app.config import get_settings  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _clean_test_db():
    """整个测试会话开始前删掉旧测试库，结束后再删一次，不污染开发数据。"""
    if _TEST_DB.exists():
        _TEST_DB.unlink()
    yield
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(_TEST_DB) + suffix)
        if candidate.exists():
            candidate.unlink()


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _prepared_app(_clean_test_db):
    """建表 + 灌种子 + 索引预热 + 编译图，一次做完给所有测试用。"""
    from app.db.session import dispose_engine, init_db
    from app.graph.builder import get_graph
    from app.rag.ingest import warmup

    await init_db()
    await warmup()
    await get_graph()

    yield

    from app.graph.checkpointer import close_checkpointer

    await close_checkpointer()
    await dispose_engine()


@pytest_asyncio.fixture
async def db():
    """一个测试独占的事务会话。"""
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def graph():
    """编译好的图，带 checkpointer（转人工测试必需）。"""
    from app.graph.builder import get_graph

    return await get_graph()


@pytest_asyncio.fixture
async def client():
    """直接打 ASGI 应用的异步客户端，用于接口级测试。

    刻意不走 lifespan：建表、灌种子、索引预热已经由 `_prepared_app` 做过一次，
    再跑一遍会重复灌种子并重建索引。
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
def thread_config():
    from app.graph.builder import thread_config as make_config

    return make_config
