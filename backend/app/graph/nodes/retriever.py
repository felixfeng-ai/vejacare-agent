"""节点 2：知识库检索。

本身很薄——真正的检索逻辑在 rag/hybrid.py。节点只负责：
拿意图和槽位去检索、把命中片段发到前端、写回 State。
"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage

from app.graph.emitter import emit, emit_node
from app.graph.state import AgentState
from app.rag.hybrid import get_retriever

logger = logging.getLogger(__name__)

#: 低于这个 RRF 分数的片段可信度太低，不如不给 LLM 免得它硬编
MIN_SCORE = 0.008


async def kb_retriever(state: AgentState) -> dict:
    emit_node("kb_retriever", "正在检索知识库")

    messages = state.get("messages", [])
    question = next(
        (m.content for m in reversed(messages) if isinstance(m, HumanMessage)), ""
    )
    history = [m.content for m in messages if isinstance(m, HumanMessage)][:-1]

    try:
        docs = await get_retriever().retrieve(
            question=question,
            intent=state.get("intent", "chitchat"),
            slots=state.get("slots") or {},
            history=history,
        )
    except Exception as exc:  # noqa: BLE001 — 检索挂了不能整通对话挂掉
        logger.exception("知识库检索失败")
        docs = []
        emit({"type": "error", "code": "RETRIEVAL_FAILED", "message": "知识库检索异常，已跳过"})
        del exc

    docs = [d for d in docs if d.get("score", 0) >= MIN_SCORE]

    emit(
        {
            "type": "kb",
            "docs": [
                {
                    "title": d.get("title", ""),
                    "source": d.get("source", ""),
                    "score": round(float(d.get("score", 0.0)), 4),
                    "snippet": d.get("snippet", ""),
                }
                for d in docs
            ],
        }
    )

    logger.info("检索命中 %d 条，最高分 %.4f", len(docs), docs[0]["score"] if docs else 0.0)
    return {"kb_docs": docs}


def route_after_retrieval(state: AgentState) -> str:
    """条件边：需不需要查工具。

    这里用规则判断而非再调一次 LLM——意图已经很明确了，
    "物流/退换货/订单修改/支付"这几类只要有订单号就该去查实时数据。
    真正的参数决策交给 tool_executor 的 function calling。
    """
    intent = state.get("intent")
    slots = state.get("slots") or {}

    if intent == "human_agent":
        return "escalation"

    needs_tool = {
        "logistics",       # 查轨迹
        "return_refund",   # 算退货运费
        "order_change",    # 改单前先确认订单状态
        "payment",         # 核验订单是否创建成功
        "customs_duty",    # 需要目的国与申报金额
    }

    if intent in needs_tool and (slots.get("order_no") or slots.get("tracking_no")):
        return "tool_executor"

    return "responder"
