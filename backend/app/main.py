"""FastAPI 应用入口。"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import chat, console, escalation, feedback, health, sessions
from app.config import get_settings
from app.schemas import fail
from app.security import ApiHttpError

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    """统一日志格式。中文日志依赖 setup_console_encoding() 先把控制台切成 UTF-8。"""
    from app.utils import setup_console_encoding

    setup_console_encoding()

    logging.basicConfig(
        level=logging.DEBUG if get_settings().debug else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    # 第三方库的 INFO 太吵，压到 WARNING
    for noisy in ("httpx", "httpcore", "openai", "urllib3", "qdrant_client", "jieba"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.db.session import dispose_engine, init_db
    from app.graph.checkpointer import close_checkpointer, get_checkpointer
    from app.rag.ingest import warmup

    setup_logging()
    settings = get_settings()

    logger.info("=" * 62)
    logger.info("  %s 跨境 AI 客服 Agent 启动中", settings.app_name)
    logger.info("  LLM      : %s / %s", settings.llm_provider, settings.llm_model)
    logger.info("  Embedding: %s / %s", settings.embedding_provider, settings.embedding_model)
    logger.info("  Reranker : %s", settings.rerank_provider)
    if settings.is_offline:
        logger.warning("  当前为【离线演示模式】：LLM 与 Embedding 均不调用外部 API，")
        logger.warning("  回答由规则模板生成，仅用于跑通链路。配置 API key 后重启即为真实模式。")
    logger.info("=" * 62)

    await init_db()
    await get_checkpointer()
    stats = await warmup()
    logger.info("检索索引：%d 条分块（向量库 %s）", stats["chunks"], stats["backend"])

    if stats["chunks"] == 0:
        logger.warning("知识库为空！请先执行：python -m scripts.ingest")

    yield

    await close_checkpointer()
    await dispose_engine()
    logger.info("%s 已关闭", settings.app_name)


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=f"{settings.app_name} — 跨境 AI 客服 Agent",
        description=(
            "基于 LangGraph 的跨境电商客服 Agent：意图识别 → 混合检索 RAG → 工具调用 "
            "→ 流式生成 → 转人工（Human-in-the-loop）→ 满意度闭环"
        ),
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Accel-Buffering"],
    )

    @app.exception_handler(ApiHttpError)
    async def _api_http_error(_: Request, exc: ApiHttpError) -> JSONResponse:
        """鉴权失败走真 HTTP 状态码，但响应体仍是统一信封。

        两件事都要：状态码对了，前端的 401 拦截器才能把用户弹去登录页，
        网关与监控也才看得见"有人在撞后台"；信封对了，前端解包逻辑就只有一套。
        """
        return JSONResponse(status_code=exc.status_code, content=fail(exc.code, exc.message))

    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(sessions.router)
    app.include_router(escalation.router)
    app.include_router(feedback.router)
    app.include_router(console.router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        return {
            "app": settings.app_name,
            "docs": "/docs",
            "health": "/api/health",
            "offline_demo": settings.is_offline,
        }

    return app


app = create_app()
