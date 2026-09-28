"""转人工工单 API。

`/reply` 是让挂起的图继续跑起来的地方：把人工回复落库 → `Command(resume=...)` 唤醒图 →
Agent 做收尾回复 → 收尾内容也落库。前端轮询 `/api/sessions/{id}/messages` 就能看到。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.db.session import get_db, session_scope
from app.graph.builder import get_graph, thread_config
from app.graph.prompts import ESCALATION_REASONS
from app.schemas import EscalationReplyRequest, fail, ok
from app.utils import to_iso

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/escalations", tags=["escalations"])


def _escalation_out(escalation, *, include_context: bool = False) -> dict:
    payload = {
        "id": escalation.id,
        "session_id": escalation.session_id,
        "reason": escalation.reason,
        "reason_label": ESCALATION_REASONS.get(escalation.reason, "转人工"),
        "status": escalation.status,
        "summary": escalation.summary,
        "human_reply": escalation.human_reply,
        "agent_name": escalation.agent_name,
        "created_at": to_iso(escalation.created_at),
        "resolved_at": to_iso(escalation.resolved_at),
    }
    if include_context:
        try:
            payload["context"] = json.loads(escalation.context_json or "{}")
        except json.JSONDecodeError:
            payload["context"] = {}
    return payload


@router.get("")
async def list_escalations(
    status: str | None = None, db: AsyncSession = Depends(get_db)
) -> dict:
    rows = await repository.list_escalations(db, status)
    return ok({"escalations": [_escalation_out(e) for e in rows]})


@router.get("/{escalation_id}")
async def get_escalation(escalation_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    escalation = await repository.get_escalation(db, escalation_id)
    if escalation is None:
        return fail("ESCALATION_NOT_FOUND", "工单不存在")

    payload = _escalation_out(escalation, include_context=True)
    messages = await repository.list_messages(db, escalation.session_id)
    payload["context"]["messages"] = [
        {"role": m.role, "content": m.content, "created_at": to_iso(m.created_at)}
        for m in messages
    ]
    return ok(payload)


@router.post("/{escalation_id}/reply")
async def reply_escalation(
    escalation_id: str, payload: EscalationReplyRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    escalation = await repository.get_escalation(db, escalation_id)
    if escalation is None:
        return fail("ESCALATION_NOT_FOUND", "工单不存在")
    if escalation.status != "pending":
        return fail("ESCALATION_ALREADY_RESOLVED", "该工单已被处理")

    session_id = escalation.session_id

    # 1) 人工回复落库，前端据此渲染成「人工客服」气泡
    await repository.add_message(
        db,
        session_id=session_id,
        role="human_agent",
        content=payload.reply,
        meta={"agent": payload.agent},
    )
    await repository.resolve_escalation(
        db, escalation, human_reply=payload.reply, agent_name=payload.agent
    )

    # 1.5) 必须在这里把事务提交掉，不能等请求结束。
    # 下面唤醒图时，节点内部会另开 `session_scope()` 写库；SQLite 同一时刻只允许一个写事务，
    # 本会话此刻持有未提交的写锁，两边会撞成 `database is locked`（实测会直接 500）。
    # 而且这两处写入是「人工回复已送达」的完整语义，先落盘再唤醒图也更安全：
    # 万一图恢复失败，人工的回复不会跟着一起回滚。
    await db.commit()

    # 2) 唤醒挂起的图，让 Agent 做收尾
    closing_text = ""
    try:
        graph = await get_graph()
        result = await graph.ainvoke(
            Command(resume={"reply": payload.reply, "agent": payload.agent}),
            thread_config(session_id),
        )
        closing_text = _extract_reply_text(result)
    except Exception as exc:  # noqa: BLE001 — 图恢复失败不影响工单状态，人工回复已经送达
        logger.exception("恢复图失败 session=%s", session_id)
        closing_text = ""
        del exc

    # 3) 收尾内容落库 + 会话状态回到 active
    async with session_scope() as write_db:
        if closing_text:
            await repository.add_message(
                write_db, session_id=session_id, role="assistant", content=closing_text
            )
        await repository.set_session_status(write_db, session_id, "active")

    return ok(
        {
            "escalation_id": escalation_id,
            "session_id": session_id,
            "status": "resolved",
            "agent": payload.agent,
            "closing_message": closing_text,
            "request_feedback": True,
        }
    )


@router.post("/{escalation_id}/resolve")
async def resolve_escalation(escalation_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """只标记完成，不发回复。用于人工判断「无需回复」的场景。"""
    escalation = await repository.get_escalation(db, escalation_id)
    if escalation is None:
        return fail("ESCALATION_NOT_FOUND", "工单不存在")

    await repository.resolve_escalation(db, escalation)
    await repository.set_session_status(db, escalation.session_id, "active")

    return ok({"escalation_id": escalation_id, "status": "resolved"})


def _extract_reply_text(result: dict) -> str:
    """从图恢复后的最终状态里取出收尾回复。"""
    if not isinstance(result, dict):
        return ""

    text = result.get("final_text")
    if text:
        return str(text).strip()

    # 兜底：取最后一条 AI 消息
    from langchain_core.messages import AIMessage

    for message in reversed(result.get("messages") or []):
        if isinstance(message, AIMessage) and message.content:
            return str(message.content).strip()
    return ""
