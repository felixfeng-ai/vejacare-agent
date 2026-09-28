"""节点 1：意图识别 + 槽位抽取。

一次 LLM 结构化输出同时拿到两样东西，省一次往返。
分类用 temperature=0 且走更便宜的分类模型（LLM_CLASSIFIER_MODEL），
这是客服场景的关键成本优化点——分类调用量远大于生成调用量。
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage

from app.graph.emitter import emit, emit_node
from app.graph.prompts import INTENT_CLASSIFY_SYSTEM, INTENT_CLASSIFY_USER
from app.graph.state import INTENT_LABELS, AgentState, Slots
from app.llm import get_llm
from app.utils import truncate

logger = logging.getLogger(__name__)

#: 意图分类失败时的兜底：宁可走 RAG 兜底也不能默认转人工
_FALLBACK = {"intent": "chitchat", "slots": {}}

_VALID_SLOTS = ("order_no", "tracking_no", "sku", "country", "amount", "email")
_HISTORY_TURNS = 4


def _user_history(messages: list) -> list[str]:
    """取最近的用户发言，用于指代消解与多轮上下文。"""
    return [m.content for m in messages if isinstance(m, HumanMessage)][-_HISTORY_TURNS:]


def _last_user_message(messages: list) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message.content
    return ""


def _normalize_slots(raw: dict) -> Slots:
    """只保留已知槽位键，去掉模型可能编出来的多余字段。"""
    slots: Slots = {}
    for key in _VALID_SLOTS:
        value = raw.get(key)
        if value in (None, "", "null", "None", "unknown"):
            continue
        if key == "amount":
            try:
                slots[key] = float(value)
            except (TypeError, ValueError):
                continue
        else:
            slots[key] = str(value).strip()
    return slots


def _normalize_intent(raw: str) -> str:
    intent = str(raw or "").strip().lower()
    return intent if intent in INTENT_LABELS else "chitchat"


async def intent_classifier(state: AgentState) -> dict:
    emit_node("intent_classifier", "正在理解您的问题")

    messages = state.get("messages", [])
    question = _last_user_message(messages)
    history = _user_history(messages)[:-1]  # 去掉本轮这句

    history_text = "\n".join(f"- {truncate(h, 120)}" for h in history) or "（无）"
    prompt = INTENT_CLASSIFY_USER.format(
        history_turns=len(history), history=history_text, question=question
    )

    result = await get_llm().json_safe(
        task="intent",
        system=INTENT_CLASSIFY_SYSTEM,
        user=prompt,
        fallback=_FALLBACK,
        hints={"question": question, "history": history},
    )

    intent = _normalize_intent(result.get("intent"))
    slots = _normalize_slots(result.get("slots") or {})

    # 用户明确要求人工时，即使槽位为空也要保证转人工链路能拿到上下文
    logger.info("意图识别：intent=%s slots=%s", intent, slots)

    emit(
        {
            "type": "intent",
            "intent": intent,
            "intent_label": INTENT_LABELS[intent],
            "slots": slots,
        }
    )

    return {
        "intent": intent,  # type: ignore[typeddict-item]
        "slots": slots,
        "turn_count": state.get("turn_count", 0) + 1,
        "kb_docs": [],
        "tool_results": [],
        # 新一轮开始，清掉上一轮的转人工痕迹。
        # 不清的话 escalation 标记会跨轮残留，导致每一轮都被路由到转人工节点。
        "escalated": False,
        "escalation_reason": "",
        "escalation_id": "",
        "human_reply": None,
        # 评分邀请同理：feedback 节点只会置 True，没有谁负责置回 False，
        # 而它跟着 checkpoint 跨轮存活。不清的话某一轮弹过评分卡之后，
        # 之后每一轮都会再弹一次（chat.py 每轮都读这个字段）。
        "request_feedback": False,
    }


def route_after_intent(state: AgentState) -> str:
    """意图后的条件边：转人工 / 闲聊直答 / 其余走检索。"""
    from app.graph.state import INTENTS_NEEDING_KB

    if state.get("intent") == "human_agent":
        return "escalation"
    if state.get("intent") in INTENTS_NEEDING_KB:
        return "kb_retriever"
    return "responder"


def make_ai_message(text: str) -> AIMessage:
    return AIMessage(content=text)
