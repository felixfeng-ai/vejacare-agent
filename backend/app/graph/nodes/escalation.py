"""节点 6：转人工（Human-in-the-loop）。

拆成两个节点是刻意的：

- `escalation_notice`：建工单、发 escalated 事件、产出一句「已为您转接人工」。
  **它正常返回**，所以这条消息会被 checkpoint 提交、用户看得到。
- `escalation_wait`：调 `interrupt()` 挂起整张图。人工回复通过 `Command(resume=...)` 唤醒它。

为什么不能合成一个节点：`interrupt()` 抛出后，该节点已做的 state 变更**全部丢弃**，
resume 时节点从头重跑。如果建工单和 interrupt 写在同一个节点里，
要么工单建两次，要么那条「已转接」消息消失。分开写两个问题都不存在。

工单创建的幂等性另有保障：见 repository.get_pending_escalation。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage

from langgraph.types import interrupt

from app.db.repository import (
    create_escalation,
    ensure_session,
    get_pending_escalation,
    set_session_status,
)
from app.db.session import session_scope
from app.graph.emitter import emit, emit_node
from app.graph.prompts import ESCALATION_REASONS, ESCALATION_REPLY_TEMPLATE
from app.graph.state import AgentState, resolve_session_id
from app.utils import truncate

logger = logging.getLogger(__name__)


def _build_summary(state: AgentState, reason: str) -> str:
    """给人工客服看的一句话摘要：用户要什么 + AI 卡在哪。

    `reason` 由调用方传进来，且必须是**已经兜底过**的那个值。这里原来自己读
    state 里的 `escalation_reason`，而落库走的是 `or "user_requested"` 的结果——
    建单那一刻 state 里是空的，于是同一张工单详情写着「用户主动要求人工」、
    摘要却写着「触发原因：未明确」，两处对不上。
    """
    intent = state.get("intent", "")
    slots = state.get("slots") or {}
    tool_summaries = [t.get("summary", "") for t in (state.get("tool_results") or [])]

    parts = [f"意图：{intent}"]
    if slots.get("order_no"):
        parts.append(f"订单号：{slots['order_no']}")
    if tool_summaries:
        parts.append(f"已查到：{truncate(tool_summaries[0], 80)}")
    parts.append(f"触发原因：{ESCALATION_REASONS.get(reason, '未明确')}")
    return "；".join(parts)


def _build_context(state: AgentState) -> dict[str, Any]:
    """落库的完整上下文，人工客服接手时不必让用户重述。"""
    return {
        "intent": state.get("intent", ""),
        "slots": state.get("slots") or {},
        "kb_docs": [
            {"title": d.get("title"), "source": d.get("source")}
            for d in (state.get("kb_docs") or [])
        ],
        "tool_results": state.get("tool_results") or [],
        "unresolved_turns": state.get("unresolved_turns", 0),
        "dissatisfied_count": state.get("dissatisfied_count", 0),
        "turn_count": state.get("turn_count", 0),
    }


async def escalation_notice(state: AgentState) -> dict:
    """建工单 + 告诉用户已转接。正常返回，消息会被持久化。"""
    emit_node("escalation", "正在为您转接人工客服")

    session_id = resolve_session_id(state)
    reason = state.get("escalation_reason") or "user_requested"
    notice = ESCALATION_REPLY_TEMPLATE

    escalation_id = state.get("escalation_id", "")

    async with session_scope() as db:
        # 幂等：同一个会话已有 pending 工单就复用，不重复建
        existing = await get_pending_escalation(db, session_id)
        if existing is not None:
            escalation_id = existing.id
        else:
            # 先保证会话行存在。set_session_status 对不存在的会话是静默 no-op，
            # 少这一步就会建出「工单挂着、会话查无此人」的孤儿数据。
            await ensure_session(db, session_id)
            escalation = await create_escalation(
                db,
                session_id=session_id,
                reason=reason,
                summary=_build_summary(state, reason),
                context=_build_context(state),
            )
            escalation_id = escalation.id
            await set_session_status(db, session_id, "escalated")
            logger.info("会话 %s 转人工，工单 %s，原因 %s", session_id, escalation_id, reason)

    emit(
        {
            "type": "escalated",
            "escalation_id": escalation_id,
            "reason": reason,
            "reason_label": ESCALATION_REASONS.get(reason, "转人工"),
            "ticket_id": escalation_id,
        }
    )
    # 前端把这句话渲染成 AI 气泡，用户知道发生了什么
    emit({"type": "token", "text": notice})

    return {
        "escalated": True,
        "awaiting_human": True,
        "escalation_id": escalation_id,
        "escalation_reason": reason,
        "final_text": notice,
        "messages": [AIMessage(content=notice)],
    }


async def escalation_wait(state: AgentState) -> dict:
    """挂起整张图，等人工回复。

    resume 时本节点从头重跑，`interrupt()` 返回 `/reply` 接口传进来的载荷。
    节点内没有任何副作用，所以重跑是安全的。
    """
    payload = interrupt(
        {
            "type": "awaiting_human",
            "escalation_id": state.get("escalation_id", ""),
            "session_id": resolve_session_id(state),
            "reason": state.get("escalation_reason", ""),
        }
    )

    # resume 可以传字符串，也可以传 {"reply": ..., "agent": ...}。
    # 这里只取 reply：署名（agent）由上层接口负责，节点不再拿它组装任何给 LLM 的话。
    if isinstance(payload, str):
        reply = payload
    else:
        reply = str((payload or {}).get("reply", ""))

    logger.info("收到人工回复，恢复会话 %s", state.get("session_id"))

    # resume 之后去 feedback → END，**不再经过 responder**。
    #
    # 这里原来把人工那句话包成 SystemMessage 注入给 LLM「做收尾」，提示词还写着
    # 「不要重复人工已经说过的话」。但人工说完了就是说完了，模型没有信息可生成，
    # 唯一的产出就是复读——实测人工说「稍等，我查询下」，AI 收尾把这句原样抄了一遍，
    # 还补了句「如还有其他问题，随时找我」。省掉这一步，"AI 复读人工的话"这一整类
    # 问题从结构上消失，而不是靠提示词祈祷。
    #
    # 收尾文案改由 /close 接口确定性拼装，也不再往 state 里塞 SystemMessage。
    return {
        "awaiting_human": False,
        "escalated": False,
        "human_reply": reply,
        "final_text": "",
    }
