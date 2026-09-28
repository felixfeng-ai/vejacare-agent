"""LangGraph 检查点存储。

interrupt()/resume 要求图必须挂 checkpointer——挂起时的完整状态（含消息历史）
存在这里，人工回复后靠它把图恢复到挂起点继续跑。

用 SQLite 文件持久化而不是 MemorySaver：服务重启后挂起的会话不能丢，
这是转人工场景的硬要求（用户可能等几小时才有人工接入）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.config import BACKEND_DIR

logger = logging.getLogger(__name__)

CHECKPOINT_DB = BACKEND_DIR / "data" / "checkpoints.db"

_saver: AsyncSqliteSaver | None = None
_conn: aiosqlite.Connection | None = None


async def get_checkpointer() -> AsyncSqliteSaver:
    global _saver, _conn
    if _saver is not None:
        return _saver

    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    _conn = await aiosqlite.connect(CHECKPOINT_DB)
    _saver = AsyncSqliteSaver(_conn)
    await _saver.setup()
    logger.info("检查点存储就绪：%s", CHECKPOINT_DB.name)
    return _saver


async def close_checkpointer() -> None:
    global _saver, _conn
    if _conn is not None:
        await _conn.close()
    _saver = None
    _conn = None


async def delete_thread(session_id: str) -> None:
    """删除会话时一并清掉检查点，避免挂起的状态变成孤儿。"""
    saver = await get_checkpointer()
    try:
        await saver.adelete_thread(session_id)
    except Exception as exc:  # noqa: BLE001 — 老版本没有这个方法
        logger.warning("清理检查点失败（session=%s）：%s", session_id, exc)


def checkpoint_path() -> Path:
    return CHECKPOINT_DB
