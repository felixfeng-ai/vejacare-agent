"""转人工接口的落库测试。

`tests/test_graph.py` 覆盖了图的挂起/恢复，但**没有覆盖 HTTP 这一层**：
`POST /api/chat/stream` → 建单挂起 → `POST /api/escalations/{id}/reply` → 收尾。
而这一层正是写入真正落库的地方，此前有个隐蔽缺陷：

写操作走 `Depends(get_db)`，`get_db` 过去从不提交，「人工回复落库」与「工单置为已解决」
两处写入在请求结束时被静默回滚，只有同一函数里用 `session_scope()` 写的那部分
（收尾文本、会话状态）留了下来。结果是数据自相矛盾——会话已经是 active，
工单却永远停在 pending，人工回复在前端刷新后消失。

这里的用例一律从 chat 接口进，不走 graph fixture 直接驱动图：
只有走完 API 层，用户与助手的消息才会真正写进 `messages` 表，
「人工接手时能看到完整对话」这一条才测得到。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.db import repository
from tests.helpers import (
    ESCALATE_MESSAGE,
    chat,
    escalate,
    fresh_db,
    parse_sse,
)

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------- 人工回复

async def test_human_reply_is_persisted_and_ticket_resolved(client: AsyncClient, agent_client: AsyncClient):
    """人工回复走完接口后：消息落库、工单置为已解决、会话回到 active。"""
    session_id, escalation_id = await escalate(client)

    resp = await agent_client.post(
        f"/api/escalations/{escalation_id}/reply",
        json={"reply": "已为您加急处理，24 小时内会有物流更新", "agent": "客服小美"},
    )
    body = resp.json()
    assert resp.status_code == 200, resp.text
    assert body["success"] is True, body
    assert body["data"]["status"] == "resolved"
    assert body["data"]["request_feedback"] is True

    async with fresh_db() as db:
        escalation = await repository.get_escalation(db, escalation_id)
        assert escalation.status == "resolved", "工单没有置为已解决"
        assert escalation.human_reply == "已为您加急处理，24 小时内会有物流更新"
        assert escalation.agent_name == "客服小美"
        assert escalation.resolved_at is not None, "缺少解决时间"

        messages = await repository.list_messages(db, session_id)
        human_msgs = [m for m in messages if m.role == "human_agent"]
        assert len(human_msgs) == 1, f"人工回复应当落库 1 条，实际 {len(human_msgs)} 条"
        assert human_msgs[0].content == "已为您加急处理，24 小时内会有物流更新"

        session = await repository.get_session(db, session_id)
        assert session.status == "active", "会话状态没有回到 active"

async def test_closing_message_is_persisted(client: AsyncClient, agent_client: AsyncClient):
    """Agent 的收尾回复也要落库，否则前端一刷新就只剩人工那句。"""
    session_id, escalation_id = await escalate(client)

    resp = await agent_client.post(f"/api/escalations/{escalation_id}/reply", json={"reply": "已处理"})
    closing = resp.json()["data"]["closing_message"]
    assert closing, "应当产出收尾回复"

    async with fresh_db() as db:
        messages = await repository.list_messages(db, session_id)
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        assert any(m.content == closing for m in assistant_msgs), "收尾回复没有落库"

async def test_user_message_is_persisted_before_escalation(client: AsyncClient):
    """用户那句话本身也要落库，否则人工接手时看不到用户在问什么。"""
    session_id, _ = await escalate(client)

    async with fresh_db() as db:
        messages = await repository.list_messages(db, session_id)
        user_msgs = [m for m in messages if m.role == "user"]
        assert any(m.content == ESCALATE_MESSAGE for m in user_msgs)

async def test_reply_to_missing_ticket_is_rejected(client: AsyncClient, agent_client: AsyncClient):
    resp = await agent_client.post("/api/escalations/esc-does-not-exist/reply", json={"reply": "你好"})
    body = resp.json()
    assert body["success"] is False, body
    assert body["error"]["code"] == "ESCALATION_NOT_FOUND"

async def test_reply_twice_is_rejected(client: AsyncClient, agent_client: AsyncClient):
    """工单被处理过就不能再回复，否则人工台会重复响应。"""
    _, escalation_id = await escalate(client)

    first = await agent_client.post(f"/api/escalations/{escalation_id}/reply", json={"reply": "第一次"})
    assert first.json()["success"] is True

    second = await agent_client.post(f"/api/escalations/{escalation_id}/reply", json={"reply": "第二次"})
    body = second.json()
    assert body["success"] is False, body
    assert body["error"]["code"] == "ESCALATION_ALREADY_RESOLVED"

async def test_empty_reply_is_rejected(client: AsyncClient, agent_client: AsyncClient):
    """空回复在校验层拦掉。"""
    _, escalation_id = await escalate(client)
    resp = await agent_client.post(f"/api/escalations/{escalation_id}/reply", json={"reply": ""})
    assert resp.status_code == 422

# ---------------------------------------------------------------- 挂起期间

async def test_ai_stays_quiet_while_awaiting_human(client: AsyncClient):
    """转人工后、人工接手前，AI 不能再插话，否则会和人工客服抢答。"""
    session_id, _ = await escalate(client)

    resp = await client.post(
        "/api/chat/stream", json={"session_id": session_id, "message": "在吗？"}
    )
    events = parse_sse(resp.text)

    error = next((e for e in events if e.get("type") == "error"), None)
    assert error is not None, f"应当拒绝继续对话，收到的事件：{events}"
    assert error["code"] == "AWAITING_HUMAN"
    assert not any(e.get("type") == "token" for e in events), "AI 不该再产出发言"

async def test_chat_resumes_after_human_handles_it(client: AsyncClient, agent_client: AsyncClient):
    """人工处理完，会话回到 active，用户再说话 AI 要能正常接。"""
    session_id, escalation_id = await escalate(client)
    await agent_client.post(f"/api/escalations/{escalation_id}/reply", json={"reply": "已处理"})

    resp = await client.post(
        "/api/chat/stream", json={"session_id": session_id, "message": "我的包裹到哪了？订单号 SO20260928001"}
    )
    events = parse_sse(resp.text)

    assert not any(e.get("type") == "error" for e in events), f"不该被拦，事件：{events}"
    assert any(e.get("type") == "token" for e in events), "AI 应当恢复应答"
    assert events[-1]["type"] == "done"

# ---------------------------------------------------------------- 只标记完成

async def test_resolve_without_reply_is_persisted(client: AsyncClient, agent_client: AsyncClient):
    """「无需回复，直接标记完成」同样要落库。"""
    session_id, escalation_id = await escalate(client)

    resp = await agent_client.post(f"/api/escalations/{escalation_id}/resolve")
    assert resp.json()["success"] is True

    async with fresh_db() as db:
        escalation = await repository.get_escalation(db, escalation_id)
        assert escalation.status == "resolved", "工单没有置为已解决"

        session = await repository.get_session(db, session_id)
        assert session.status == "active"

# ---------------------------------------------------------------- 人工台读取

async def test_pending_list_only_shows_unhandled(client: AsyncClient, agent_client: AsyncClient):
    """人工台的工作列表只该看到未处理的工单。"""
    _, escalation_id = await escalate(client)

    pending = (await agent_client.get("/api/escalations", params={"status": "pending"})).json()
    assert pending["success"] is True
    assert escalation_id in [e["id"] for e in pending["data"]["escalations"]]

    await agent_client.post(f"/api/escalations/{escalation_id}/resolve")

    after = (await agent_client.get("/api/escalations", params={"status": "pending"})).json()
    assert escalation_id not in [e["id"] for e in after["data"]["escalations"]], (
        "已处理的工单不该还留在待办里"
    )

async def test_ticket_detail_carries_context(client: AsyncClient, agent_client: AsyncClient):
    """人工接手时要能直接看到上下文，不必让用户重述。"""
    session_id, escalation_id = await escalate(client)

    detail = (await agent_client.get(f"/api/escalations/{escalation_id}")).json()
    assert detail["success"] is True

    data = detail["data"]
    assert data["session_id"] == session_id
    assert data["summary"], "缺少给人工看的一句话摘要"

    context = data["context"]
    assert context["intent"] == "human_agent"
    assert context["messages"], "上下文里应当带完整对话"
    assert any(m["content"] == ESCALATE_MESSAGE for m in context["messages"])
