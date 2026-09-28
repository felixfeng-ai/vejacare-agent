"""图装配。

    START
      │
      ▼
  intent_classifier ──(human_agent)──► escalation_notice ──► escalation_wait ══╗
      │                                                       (interrupt 挂起)  ║
      ├──(闲聊)──────────────────────────────────────────────┐                 ║
      │                                                       │                 ║
      └──(需知识)──► kb_retriever ──(需实时数据)──► tool_executor ──► responder ◄╝
                          │                                            │
                          └────────────(通用政策)──────────────────────┘
                                                                       │
                                                                       ▼
                                                                turn_evaluator
                                                                       │
                        ┌──────────────(触发转人工)─────────────────────┤
                        │                                              │
                        ▼                                              ├──(已解决)──► feedback ──► END
                 escalation_notice                                     │
                        │                                              └──(待用户补充)──► END
                        ▼
                 escalation_wait ──(人工回复后 resume)──► responder
"""

from __future__ import annotations

import logging
from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from app.graph.nodes.escalation import escalation_notice, escalation_wait
from app.graph.nodes.evaluator import feedback_marker, route_after_evaluation, turn_evaluator
from app.graph.nodes.intent import intent_classifier, route_after_intent
from app.graph.nodes.responder import responder
from app.graph.nodes.retriever import kb_retriever, route_after_retrieval
from app.graph.nodes.tools import tool_executor
from app.graph.state import AgentState

logger = logging.getLogger(__name__)


def build_graph(checkpointer=None):
    """编译图。checkpointer 为 None 时图仍可跑，但 interrupt() 会直接报错。"""
    builder = StateGraph(AgentState)

    builder.add_node("intent_classifier", intent_classifier)
    builder.add_node("kb_retriever", kb_retriever)
    builder.add_node("tool_executor", tool_executor)
    builder.add_node("responder", responder)
    builder.add_node("turn_evaluator", turn_evaluator)
    builder.add_node("escalation_notice", escalation_notice)
    builder.add_node("escalation_wait", escalation_wait)
    builder.add_node("feedback", feedback_marker)

    builder.add_edge(START, "intent_classifier")

    builder.add_conditional_edges(
        "intent_classifier",
        route_after_intent,
        {
            "escalation": "escalation_notice",
            "kb_retriever": "kb_retriever",
            "responder": "responder",
        },
    )

    builder.add_conditional_edges(
        "kb_retriever",
        route_after_retrieval,
        {
            "escalation": "escalation_notice",
            "tool_executor": "tool_executor",
            "responder": "responder",
        },
    )

    builder.add_edge("tool_executor", "responder")
    builder.add_edge("responder", "turn_evaluator")

    builder.add_conditional_edges(
        "turn_evaluator",
        route_after_evaluation,
        {
            "escalation": "escalation_notice",
            "feedback": "feedback",
            "end": END,
        },
    )

    # 转人工：notice 正常返回（消息落库）→ wait 挂起 → 人工回复后 resume 继续
    builder.add_edge("escalation_notice", "escalation_wait")
    builder.add_edge("escalation_wait", "responder")
    builder.add_edge("feedback", END)

    return builder.compile(checkpointer=checkpointer)


_graph = None


async def get_graph():
    """进程级单例。编译图有开销，且 checkpointer 必须全局唯一。"""
    global _graph
    if _graph is None:
        from app.graph.checkpointer import get_checkpointer

        checkpointer = await get_checkpointer()
        _graph = build_graph(checkpointer)
        logger.info("Agent 图编译完成")
    return _graph


def reset_graph() -> None:
    """测试用。"""
    global _graph
    _graph = None


def thread_config(session_id: str) -> dict:
    """LangGraph 用 thread_id 隔离不同会话的状态。"""
    return {"configurable": {"thread_id": session_id}}
