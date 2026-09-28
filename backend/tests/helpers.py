"""接口级测试的共用工具。

`tests/test_graph.py` 直接驱动图，不需要这些；凡是走 HTTP 的测试都用这里的东西，
免得每个文件各抄一份 SSE 解析。
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

from httpx import AsyncClient

from app.db.session import get_session_factory

#: 一句必定触发转人工的话。mock 模式按关键词命中 human_agent 意图。
ESCALATE_MESSAGE = "我要转人工，找你们真人客服"


@asynccontextmanager
async def fresh_db():
    """开一个全新的数据库会话去读。

    刻意不复用请求里的 session：只有真的提交过，新会话才读得到，
    这正是「接口报成功但没落库」这类问题的判据。
    """
    async with get_session_factory()() as session:
        yield session


def parse_sse(text: str) -> list[dict]:
    """把 SSE 响应体拆成事件列表。"""
    events = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload:
            continue
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return events


def new_session_id() -> str:
    return f"t-{uuid.uuid4().hex[:8]}"


async def chat(client: AsyncClient, session_id: str, message: str) -> list[dict]:
    """打一次对话接口，返回解析后的事件列表。

    httpx 的 ASGITransport 会把整个 SSE 响应收完再返回，
    拿不到逐块到达的时序，但事件内容与落库结果都是真的。
    """
    resp = await client.post(
        "/api/chat/stream", json={"session_id": session_id, "message": message}
    )
    assert resp.status_code == 200, resp.text
    return parse_sse(resp.text)


async def escalate(client: AsyncClient) -> tuple[str, str]:
    """走真实对话接口触发转人工，返回 (session_id, escalation_id)。"""
    session_id = new_session_id()
    events = await chat(client, session_id, ESCALATE_MESSAGE)

    payload = next((e for e in events if e.get("type") == "escalated"), None)
    assert payload is not None, f"没有触发转人工，收到的事件：{events}"
    return session_id, payload["escalation_id"]
