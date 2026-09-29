"""流式对话端点。

SSE 事件的产生有两路：
- 图节点通过 `emit()` 写的 custom 事件（node / intent / kb / tool / escalated / token）
- 本层在流结束后补的收尾事件（feedback_request / done / error）

助手消息的持久化**以 token 累加结果为准**而不是读最终 state：
两者应当一致，但 token 是用户实际看到的东西，以它为准能保证"存下来的 = 看到的"。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from langchain_core.messages import HumanMessage
from sse_starlette.sse import EventSourceResponse

from app.db import repository
from app.db.session import session_scope
from app.graph.builder import get_graph, thread_config
from app.schemas import ChatRequest, fail
from app.utils import gen_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])


def _sse(event: dict[str, Any]) -> dict[str, str]:
    return {"event": "message", "data": json.dumps(event, ensure_ascii=False, default=str)}


@router.post("/chat/stream")
async def chat_stream(payload: ChatRequest, request: Request) -> EventSourceResponse:
    session_id = payload.session_id or gen_id("s")
    user_text = payload.message.strip()

    async def event_stream() -> AsyncIterator[dict[str, str]]:
        try:
            async with session_scope() as db:
                session = await repository.ensure_session(db, session_id, user_text)

                # 已转人工、人工还没结单时，不让 AI 继续插话，避免和人工客服抢答。
                #
                # 但这句话**必须先落库**再返回提示。以前这里在 add_message 之前就 return 了，
                # 于是用户在等待期间补的订单号、地址、具体诉求既不进对话记录也不进工单，
                # 直接消失——又一个「接口正常返回、数据其实没落库」。而客服端正是靠这条
                # 消息才知道用户又说话了（工单列表的 unread_count），丢了它客服永远看不到。
                if session.status == "escalated":
                    await repository.add_message(
                        db, session_id=session_id, role="user", content=user_text
                    )
                    yield _sse(
                        {
                            "type": "error",
                            "code": "AWAITING_HUMAN",
                            "message": "已收到，正在转达人工客服。人工回复后即可继续对话。",
                        }
                    )
                    yield _sse({"type": "done", "message_id": "", "escalated": True})
                    return

                await repository.add_message(
                    db, session_id=session_id, role="user", content=user_text
                )

            yield _sse({"type": "session", "session_id": session_id})

            graph = await get_graph()
            config = thread_config(session_id)
            # 只传本轮新增的消息：其余状态由 checkpointer 恢复，
            # 传完整 state 会把上一轮的计数器、检索结果全冲掉
            graph_input = {"messages": [HumanMessage(content=user_text)], "session_id": session_id}

            collected: list[str] = []
            intent = ""
            escalated = False

            async for event in graph.astream(graph_input, config, stream_mode="custom"):
                if await request.is_disconnected():
                    logger.info("客户端断开，中止会话 %s 的本轮流式输出", session_id)
                    return

                if not isinstance(event, dict):
                    continue

                if event.get("type") == "token":
                    collected.append(str(event.get("text", "")))
                elif event.get("type") == "intent":
                    intent = str(event.get("intent", ""))
                elif event.get("type") == "escalated":
                    escalated = True

                yield _sse(event)

            final_state = await graph.aget_state(config)
            values = final_state.values or {}
            message_id = gen_id("m")
            answer = "".join(collected).strip()

            async with session_scope() as db:
                if answer:
                    await repository.add_message(
                        db,
                        session_id=session_id,
                        role="assistant",
                        content=answer,
                        intent=intent or str(values.get("intent", "")),
                        message_id=message_id,
                    )

            if values.get("request_feedback"):
                yield _sse({"type": "feedback_request", "message_id": message_id})

            yield _sse(
                {
                    "type": "done",
                    "message_id": message_id,
                    "intent": intent or str(values.get("intent", "")),
                    "escalated": escalated,
                }
            )

        except Exception as exc:  # noqa: BLE001 — 流已经开始，只能以事件形式报错
            logger.exception("对话流异常 session=%s", session_id)
            yield _sse(
                {
                    "type": "error",
                    "code": "STREAM_FAILED",
                    "message": f"服务出现异常，请稍后重试（{type(exc).__name__}）",
                }
            )
            yield _sse({"type": "done", "message_id": "", "escalated": False})

    return EventSourceResponse(
        event_stream(),
        # 关掉代理缓冲：Nginx 默认会攒够一批再下发，打字机效果就没了
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@router.get("/chat/probe")
async def probe() -> dict:
    """诊断用：确认图能编译、checkpointer 能落盘。"""
    try:
        await get_graph()
    except Exception as exc:  # noqa: BLE001
        return fail("GRAPH_BUILD_FAILED", str(exc))
    return {"success": True, "data": {"graph": "ready"}, "error": None}
