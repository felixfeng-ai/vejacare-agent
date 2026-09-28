"""节点 4：生成回复（流式）。

这是唯一产出用户可见文本的节点。前面所有节点的产出（知识片段、工具结果、人工回复）
都在这里被组织进 prompt——**LLM 只能看到这里给它的东西**，
这是防止它编造物流状态和退款金额的结构性保证。
"""

from __future__ import annotations

import json
import logging

from langchain_core.messages import AIMessage, HumanMessage

from app.graph.emitter import emit, emit_node
from app.graph.prompts import RESPONDER_SYSTEM, RESPONDER_USER
from app.graph.state import INTENT_LABELS, AgentState
from app.llm import get_llm

logger = logging.getLogger(__name__)

_EMPTY = "（无）"

#: 生成彻底失败时的兜底话术。宁可让用户重试，也不能静默返回空消息。
_FALLBACK_REPLY = "抱歉，我这边刚才没能整理出回复，麻烦您再说一次，或者我帮您转接人工客服。"


def _kb_context(docs: list[dict]) -> str:
    if not docs:
        return _EMPTY
    blocks = []
    for doc in docs:
        header = f"[{doc.get('heading_path') or doc.get('title') or doc.get('source')}]"
        blocks.append(f"{header}\n{doc.get('text', '')}")
    return "\n---\n".join(blocks)


def _tool_context(results: list[dict]) -> str:
    if not results:
        return _EMPTY
    lines = []
    for item in results:
        status = "查询成功" if item.get("ok") else "查询失败"
        lines.append(f"- 工具 {item.get('name')}（{status}）：{item.get('summary', '')}")
        if item.get("ok") and item.get("data"):
            lines.append(f"  原始数据：{json.dumps(item['data'], ensure_ascii=False)}")
    return "\n".join(lines)


def _human_context(state: AgentState) -> str:
    reply = state.get("human_reply")
    return reply if reply else _EMPTY


async def responder(state: AgentState) -> dict:
    emit_node("responder", "正在为您整理回复")

    messages = state.get("messages", [])
    question = next(
        (m.content for m in reversed(messages) if isinstance(m, HumanMessage)), ""
    )
    intent = state.get("intent", "chitchat")

    prompt = RESPONDER_USER.format(
        intent_label=INTENT_LABELS.get(intent, "其他"),
        slots=json.dumps(state.get("slots") or {}, ensure_ascii=False),
        kb_context=_kb_context(state.get("kb_docs") or []),
        tool_context=_tool_context(state.get("tool_results") or []),
        human_context=_human_context(state),
        question=question,
    )

    chunks: list[str] = []
    try:
        async for piece in get_llm().stream(
            task="respond",
            system=RESPONDER_SYSTEM,
            user=prompt,
            hints={"question": question, "intent": intent},
        ):
            chunks.append(piece)
            emit({"type": "token", "text": piece})
    except Exception as exc:  # noqa: BLE001 — 生成失败要给用户一句人话，不能白屏
        logger.exception("回复生成失败")
        del exc
        if not chunks:
            emit({"type": "token", "text": _FALLBACK_REPLY})
            chunks.append(_FALLBACK_REPLY)

    text = "".join(chunks).strip() or _FALLBACK_REPLY

    return {
        "messages": [AIMessage(content=text)],
        "final_text": text,
    }
