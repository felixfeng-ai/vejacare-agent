"""数据访问层。

所有 SQL 都收敛在这里，节点和 API 只调函数。换 PostgreSQL、加缓存、加读写分离，
都只改这一层。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatSession, Escalation, Feedback, Message, Ticket
from app.utils import gen_id, truncate

logger = logging.getLogger(__name__)

SESSION_TITLE_LIMIT = 24


# ---------------------------------------------------------------- 会话与消息


async def get_session(db: AsyncSession, session_id: str) -> ChatSession | None:
    return await db.get(ChatSession, session_id)


async def ensure_session(db: AsyncSession, session_id: str, first_message: str = "") -> ChatSession:
    """取会话，不存在就建一个。标题取首句，方便左侧历史栏辨认。"""
    session = await db.get(ChatSession, session_id)
    if session is not None:
        return session

    session = ChatSession(
        id=session_id,
        title=truncate(first_message, SESSION_TITLE_LIMIT) if first_message else "新的对话",
        status="active",
    )
    db.add(session)
    await db.flush()
    return session


async def add_message(
    db: AsyncSession,
    *,
    session_id: str,
    role: str,
    content: str,
    intent: str = "",
    meta: dict[str, Any] | None = None,
    message_id: str | None = None,
) -> Message:
    message = Message(
        id=message_id or gen_id("m"),
        session_id=session_id,
        role=role,
        content=content,
        intent=intent,
        meta_json=json.dumps(meta or {}, ensure_ascii=False),
    )
    db.add(message)

    session = await db.get(ChatSession, session_id)
    if session is not None:
        session.updated_at = datetime.now(timezone.utc)
        if intent:
            session.last_intent = intent
        if role == "user":
            session.turn_count += 1

    await db.flush()
    return message


async def list_messages(db: AsyncSession, session_id: str) -> list[Message]:
    result = await db.scalars(
        select(Message).where(Message.session_id == session_id).order_by(Message.created_at)
    )
    return list(result)


async def list_sessions(db: AsyncSession, limit: int = 50) -> list[dict]:
    stmt = (
        select(ChatSession, func.count(Message.id).label("message_count"))
        .outerjoin(Message, Message.session_id == ChatSession.id)
        .group_by(ChatSession.id)
        .order_by(ChatSession.updated_at.desc())
        .limit(limit)
    )
    rows = await db.execute(stmt)
    from app.utils import to_iso

    return [
        {
            "session_id": session.id,
            "title": session.title,
            "status": session.status,
            "last_intent": session.last_intent,
            "message_count": count,
            "updated_at": to_iso(session.updated_at),
        }
        for session, count in rows.all()
    ]


async def delete_session(db: AsyncSession, session_id: str) -> bool:
    session = await db.get(ChatSession, session_id)
    if session is None:
        return False
    # 级联由 relationship 的 cascade 负责；escalations/tickets 无外键，手动清
    await db.execute(delete(Escalation).where(Escalation.session_id == session_id))
    await db.execute(delete(Feedback).where(Feedback.session_id == session_id))
    await db.execute(delete(Ticket).where(Ticket.session_id == session_id))
    await db.delete(session)
    return True


async def set_session_status(db: AsyncSession, session_id: str, status: str) -> None:
    session = await db.get(ChatSession, session_id)
    if session is not None:
        session.status = status
        session.updated_at = datetime.now(timezone.utc)


# ---------------------------------------------------------------- 转人工


async def create_escalation(
    db: AsyncSession,
    *,
    session_id: str,
    reason: str,
    summary: str,
    context: dict[str, Any],
) -> Escalation:
    escalation = Escalation(
        id=gen_id("esc"),
        session_id=session_id,
        reason=reason,
        status="pending",
        summary=summary,
        context_json=json.dumps(context, ensure_ascii=False, default=str),
    )
    db.add(escalation)
    await db.flush()
    return escalation


async def get_pending_escalation(db: AsyncSession, session_id: str) -> Escalation | None:
    """取该会话尚未处理的转人工工单。

    **必须幂等**：LangGraph 的 interrupt() 在 resume 时会让节点从头重跑一遍，
    没有这个查询就会给同一次转人工建出两张工单。
    """
    return await db.scalar(
        select(Escalation)
        .where(Escalation.session_id == session_id, Escalation.status == "pending")
        .order_by(Escalation.created_at.desc())
        .limit(1)
    )


async def get_escalation(db: AsyncSession, escalation_id: str) -> Escalation | None:
    return await db.get(Escalation, escalation_id)


async def list_escalations(db: AsyncSession, status: str | None = None) -> list[Escalation]:
    stmt = select(Escalation).order_by(Escalation.created_at.desc())
    if status:
        stmt = stmt.where(Escalation.status == status)
    result = await db.scalars(stmt.limit(100))
    return list(result)


async def resolve_escalation(
    db: AsyncSession, escalation: Escalation, *, human_reply: str = "", agent_name: str = ""
) -> Escalation:
    escalation.status = "resolved"
    escalation.human_reply = human_reply
    escalation.agent_name = agent_name
    escalation.resolved_at = datetime.now(timezone.utc)
    await db.flush()
    return escalation


# ---------------------------------------------------------------- 满意度


async def add_feedback(
    db: AsyncSession,
    *,
    session_id: str,
    message_id: str,
    rating: int,
    comment: str,
    intent: str = "",
) -> Feedback:
    feedback = Feedback(
        id=gen_id("fb"),
        session_id=session_id,
        message_id=message_id,
        rating=rating,
        comment=comment,
        intent=intent,
    )
    db.add(feedback)
    await db.flush()
    return feedback


async def satisfaction_metrics(db: AsyncSession) -> dict:
    """满意度看板。演示用，SQL 直接算，量大了应该走离线数仓。"""
    total_sessions = await db.scalar(select(func.count()).select_from(ChatSession)) or 0
    total_escalations = await db.scalar(select(func.count()).select_from(Escalation)) or 0

    ratings = list(await db.scalars(select(Feedback.rating)))
    rated_sessions = len(ratings)

    distribution = {str(i): 0 for i in range(1, 6)}
    for rating in ratings:
        key = str(max(1, min(5, rating)))
        distribution[key] += 1

    intent_rows = await db.execute(
        select(ChatSession.last_intent, func.count(ChatSession.id))
        .where(ChatSession.last_intent != "")
        .group_by(ChatSession.last_intent)
        .order_by(func.count(ChatSession.id).desc())
        .limit(8)
    )

    return {
        "total_sessions": total_sessions,
        "rated_sessions": rated_sessions,
        "avg_rating": round(sum(ratings) / rated_sessions, 2) if ratings else None,
        "rating_distribution": distribution,
        "escalation_count": total_escalations,
        "escalation_rate": round(total_escalations / total_sessions, 4) if total_sessions else 0.0,
        "top_intents": [
            {"intent": intent, "count": count} for intent, count in intent_rows.all()
        ],
    }
