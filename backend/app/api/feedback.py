"""满意度闭环与指标看板。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.db.session import get_db
from app.schemas import FeedbackRequest, fail, ok
from app.security import ROLE_ADMIN, require_role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["feedback"])


@router.post("/feedback")
async def submit_feedback(payload: FeedbackRequest, db: AsyncSession = Depends(get_db)) -> dict:
    session = await repository.get_session(db, payload.session_id)
    if session is None:
        return fail("SESSION_NOT_FOUND", "会话不存在")

    feedback = await repository.add_feedback(
        db,
        session_id=payload.session_id,
        message_id=payload.message_id,
        rating=payload.rating,
        comment=payload.comment,
        intent=session.last_intent,
    )

    # 低分会话打标，便于质检抽查与后续做 bad case 分析
    if payload.rating <= 2:
        logger.warning(
            "低分评价 session=%s rating=%d comment=%s",
            payload.session_id,
            payload.rating,
            payload.comment[:120],
        )

    return ok({"feedback_id": feedback.id, "rating": payload.rating})


# 看板只给管理者：这是经营数据，客服看单条工单就够了，不该看到整体满意度走势。
# 注意不能像 escalations 那样挂在 router 上——同一个 router 下的 /feedback
# 是用户端提交评价用的，必须保持公开。
@router.get(
    "/metrics/satisfaction",
    dependencies=[Depends(require_role(ROLE_ADMIN))],
)
async def satisfaction(db: AsyncSession = Depends(get_db)) -> dict:
    metrics = await repository.satisfaction_metrics(db)

    # 自动解决率 = 没转人工的会话占比
    total = metrics["total_sessions"] or 0
    escalated = metrics["escalation_count"]
    metrics["auto_resolved_rate"] = round((total - escalated) / total, 4) if total else 0.0

    return ok(metrics)
