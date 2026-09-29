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

# 后台口令：固定的测试值，与生产无关。生产口令由部署脚本随机生成。
# 之所以必须显式设置而不是靠默认值：默认值是空串 = 后台整体关闭，
# 那所有受保护接口都会返回 503，鉴权测试会集体"通过"得毫无意义。
os.environ["CONSOLE_AGENT_PASSWORD"] = TEST_AGENT_PASSWORD = "test-agent-pwd"
os.environ["CONSOLE_ADMIN_PASSWORD"] = TEST_ADMIN_PASSWORD = "test-admin-pwd"
os.environ["CONSOLE_TOKEN_SECRET"] = "test-token-secret"

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


@pytest.fixture(autouse=True)
def _reset_login_failures():
    """登录失败计数是进程内的全局字典，测试之间会互相污染。

    没有这个夹具，一个"连续失败触发锁定"的用例会把后面的登录用例一起锁掉——
    而且报错会指向后面的用例，排查起来南辕北辙。
    """
    from app.security import reset_login_failures

    reset_login_failures()
    yield
    reset_login_failures()


def _auth_header(role: str) -> dict[str, str]:
    from app.security import issue_token

    token, _ = issue_token(role, "测试客服")
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def agent_client():
    """带客服令牌的客户端。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import app
    from app.security import ROLE_AGENT

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=_auth_header(ROLE_AGENT),
    ) as ac:
        yield ac


@pytest_asyncio.fixture
async def admin_client():
    """带管理令牌的客户端。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import app
    from app.security import ROLE_ADMIN

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=_auth_header(ROLE_ADMIN),
    ) as ac:
        yield ac


@pytest.fixture
def thread_config():
    from app.graph.builder import thread_config as make_config

    return make_config
