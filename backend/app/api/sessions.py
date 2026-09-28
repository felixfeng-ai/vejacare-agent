"""会话与消息读取。"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.db.session import get_db
from app.schemas import fail, ok
from app.utils import to_iso
from fastapi import Depends

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _message_out(message) -> dict:
    try:
        meta = json.loads(message.meta_json or "{}")
    except json.JSONDecodeError:
        meta = {}

    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "intent": message.intent,
        "created_at": to_iso(message.created_at),
        "meta": meta,
    }


@router.get("")
async def list_sessions(db: AsyncSession = Depends(get_db)) -> dict:
    return ok({"sessions": await repository.list_sessions(db)})


@router.get("/{session_id}/messages")
async def get_messages(session_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    session = await repository.get_session(db, session_id)
    if session is None:
        return fail("SESSION_NOT_FOUND", "会话不存在")

    messages = await repository.list_messages(db, session_id)

    # 有待处理工单时，前端据此继续轮询等人工回复
    pending = await repository.get_pending_escalation(db, session_id)

    return ok(
        {
            "session_id": session_id,
            "status": session.status,
            "title": session.title,
            "awaiting_human": pending is not None,
            "escalation_id": pending.id if pending else None,
            "messages": [_message_out(m) for m in messages],
        }
    )


@router.delete("/{session_id}")
async def delete_session(session_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    removed = await repository.delete_session(db, session_id)
    if not removed:
        return fail("SESSION_NOT_FOUND", "会话不存在")

    # 检查点里可能还挂着 interrupt 状态，一并清掉
    from app.graph.checkpointer import delete_thread

    await delete_thread(session_id)
    return ok({"session_id": session_id, "deleted": True})
