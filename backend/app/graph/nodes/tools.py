"""节点 3：工具调用。

走 LLM 原生 function calling（`tools=` 参数），模型自己决定调哪个工具、传什么参数。
端点不支持 tools 时会自动降级到 JSON 决策通道，见 app/llm.py。

工具执行结果一律回填 State，不管成功失败——失败时 responder 仍要能回一句人话。
"""

from __future__ import annotations

import json
import logging

from langchain_core.messages import HumanMessage

from app.db.session import session_scope
from app.graph.emitter import emit, emit_node
from app.graph.prompts import TOOL_DECISION_SYSTEM, TOOL_DECISION_USER
from app.graph.state import AgentState, ToolResult
from app.llm import get_llm
from app.tools import registry
from app.utils import truncate

logger = logging.getLogger(__name__)

#: 一次只允许调一个工具，避免并发查库 + 结果互相覆盖
MAX_TOOL_CALLS = 1


def _kb_digest(docs: list[dict]) -> str:
    if not docs:
        return "（无）"
    return "\n".join(f"- {truncate(d.get('snippet', ''), 80)}" for d in docs[:3])


async def tool_executor(state: AgentState) -> dict:
    emit_node("tool_executor", "正在查询您的订单信息")

    messages = state.get("messages", [])
    question = next(
        (m.content for m in reversed(messages) if isinstance(m, HumanMessage)), ""
    )
    intent = state.get("intent", "chitchat")
    slots = state.get("slots") or {}

    decision = await get_llm().with_tools(
        system=TOOL_DECISION_SYSTEM,
        user=TOOL_DECISION_USER.format(
            intent=intent,
            slots=json.dumps(slots, ensure_ascii=False),
            kb_digest=_kb_digest(state.get("kb_docs") or []),
            question=question,
        ),
        tools=registry.openai_tools(),
        hints={"intent": intent, "slots": slots, "question": question},
    )

    calls = decision.get("tool_calls") or []
    if not calls:
        logger.info("模型判断无需调用工具：%s", truncate(decision.get("text", ""), 100))
        return {"tool_results": []}

    results: list[ToolResult] = []
    async with session_scope() as db:
        for call in calls[:MAX_TOOL_CALLS]:
            result = await _run_one(db, call, session_id=state.get("session_id", ""))
            results.append(result)

    return {"tool_results": results}


async def _run_one(db, call: dict, *, session_id: str) -> ToolResult:
    """执行单个工具，前后各发一次事件，让前端能看到卡片从「查询中」变「已完成」。"""
    name = call.get("name", "")
    args = dict(call.get("args") or {})
    # 建工单需要会话 id 做关联，由后端注入而不是让模型生成
    if name == "create_ticket":
        args.setdefault("session_id", session_id)

    emit({"type": "tool", "name": name, "status": "running", "args": args})

    outcome = await registry.execute(db, name, args)
    ok = bool(outcome.get("ok"))

    emit(
        {
            "type": "tool",
            "name": name,
            "status": "done" if ok else "error",
            "args": args,
            "result": outcome.get("data") or {},
            "summary": outcome.get("summary", ""),
        }
    )

    if not ok:
        logger.warning("工具 %s 失败：%s", name, outcome.get("error"))

    return ToolResult(
        name=name,
        args=args,
        ok=ok,
        summary=outcome.get("summary", ""),
        data=outcome.get("data") or {},
        error=outcome.get("error"),
    )


def route_after_tools(state: AgentState) -> str:
    """工具跑完直接进 responder；工具失败也进 responder（由它决定怎么措辞）。"""
    return "responder"
