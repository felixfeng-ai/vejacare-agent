"""节点 5：本轮评估 + 转人工判定。

转人工的三个触发条件（对应 prompt 与配置里的阈值）：
1. 用户明确要求人工 —— 立即转，不计数
2. 用户连续 N 次表达不满 —— 累计触发
3. 同一问题连续 N 轮未解决 —— 累计触发

"AI 正在索要订单号"这类澄清轮**不计入未解决**——那是正常的信息收集，不是失败。
这个区分很重要，否则 Agent 问两次订单号就把用户转给人工了。
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage

from app.config import get_settings
from app.graph.emitter import emit_node
from app.graph.prompts import TURN_EVAL_SYSTEM, TURN_EVAL_USER
from app.graph.state import AgentState
from app.llm import get_llm

logger = logging.getLogger(__name__)

_FALLBACK = {
    "resolved": False,
    "needs_clarification": False,
    "dissatisfied": False,
    "wants_human": False,
}


async def turn_evaluator(state: AgentState) -> dict:
    emit_node("turn_evaluator", "正在确认问题是否已解决")

    settings = get_settings()
    messages = state.get("messages", [])
    question = next(
        (m.content for m in reversed(messages) if isinstance(m, HumanMessage)), ""
    )
    answer = state.get("final_text") or next(
        (m.content for m in reversed(messages) if isinstance(m, AIMessage)), ""
    )

    verdict = await get_llm().json_safe(
        task="turn_eval",
        system=TURN_EVAL_SYSTEM,
        user=TURN_EVAL_USER.format(question=question, answer=answer),
        fallback=_FALLBACK,
        hints={"question": question, "answer": answer},
    )

    resolved = bool(verdict.get("resolved"))
    needs_clarification = bool(verdict.get("needs_clarification"))
    dissatisfied = bool(verdict.get("dissatisfied"))
    wants_human = bool(verdict.get("wants_human"))

    # --- 计数器维护 ---
    unresolved_turns = state.get("unresolved_turns", 0)
    if not resolved and not needs_clarification and not wants_human:
        unresolved_turns += 1
    elif resolved:
        unresolved_turns = 0  # 解决了就清零，下次是新问题

    dissatisfied_count = state.get("dissatisfied_count", 0) + 1 if dissatisfied else 0

    # --- 转人工判定 ---
    # 人工刚接手完的收尾轮不参与判定：用户自人工回复后还没再说过话，
    # 拿着那句还带"人工"二字的原话再评一次必然误判成 wants_human。
    # route_after_evaluation 已经在这种情况下拒绝重新转人工，这里必须口径一致，
    # 否则会写下一个"路由说没转、状态说转了"的自相矛盾 escalated=True。
    closing_out = bool(state.get("human_reply"))
    reason = ""
    if not closing_out:
        if wants_human:
            reason = "user_requested"
        elif dissatisfied_count >= settings.escalation_max_dissatisfaction:
            reason = "dissatisfied"
        elif unresolved_turns >= settings.escalation_max_unresolved_turns:
            reason = "unresolved"

    logger.info(
        "本轮评估 resolved=%s clarification=%s dissatisfied=%s → 未解决轮数=%d 不满次数=%d 转人工=%s",
        resolved,
        needs_clarification,
        dissatisfied,
        unresolved_turns,
        dissatisfied_count,
        reason or "否",
    )

    return {
        "resolved": resolved,
        "needs_clarification": needs_clarification,
        "unresolved_turns": unresolved_turns,
        "dissatisfied_count": dissatisfied_count,
        "escalated": bool(reason) or state.get("escalated", False),
        "escalation_reason": reason or state.get("escalation_reason", ""),
    }


def route_after_evaluation(state: AgentState) -> str:
    """条件边：转人工 / 弹满意度 / 结束本轮等用户下一句。"""
    # 人工刚接手完的那一轮走收尾，不再重新评估、也不再重复触发转人工
    # （用户自人工回复后还没再说过话，这时候再评估一次必然误判）
    if state.get("human_reply"):
        return "feedback"

    if state.get("escalated") and not state.get("awaiting_human"):
        return "escalation"

    if state.get("resolved"):
        return "feedback"

    return "end"


async def feedback_marker(state: AgentState) -> dict:
    """节点 6（轻量）：标记可以邀请用户评分。

    不做数据库写入——真正的评分记录在用户提交 /api/feedback 时才落库，
    避免"弹了卡片但用户没评"产生一堆空记录污染满意度统计。
    """
    emit_node("feedback", "正在收尾")
    return {"request_feedback": True}
