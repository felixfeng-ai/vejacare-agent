"""会话读取与删除的接口测试。

删除这条此前和满意度接口同源：`DELETE` 走 `Depends(get_db)`，而 `get_db` 过去从不提交，
于是删掉的会话、消息、工单在请求结束时全部回滚，接口却返回 `deleted: true`，
前端一刷新删掉的对话又回来了——比报错更难查。

同时覆盖「有工单待处理时会话要告诉前端继续轮询」这条契约。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.db import repository
from tests.helpers import chat, escalate, fresh_db, new_session_id

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------- 读取


async def test_session_appears_in_list_after_chat(client: AsyncClient):
    session_id = new_session_id()
    await chat(client, session_id, "我的包裹到哪了？订单号 SO20260928001")

    body = (await client.get("/api/sessions")).json()
    assert body["success"] is True
    row = next((s for s in body["data"]["sessions"] if s["session_id"] == session_id), None)
    assert row is not None, "新会话没有出现在列表里"
    assert row["last_intent"] == "logistics"
    assert row["message_count"] >= 2, "用户与助手两条消息都该在"


async def test_messages_carry_roles_and_intent(client: AsyncClient):
    session_id = new_session_id()
    await chat(client, session_id, "我的包裹到哪了？订单号 SO20260928001")

    body = (await client.get(f"/api/sessions/{session_id}/messages")).json()
    data = body["data"]

    assert data["session_id"] == session_id
    assert data["status"] == "active"
    assert data["awaiting_human"] is False
    assert data["escalation_id"] is None

    roles = [m["role"] for m in data["messages"]]
    assert "user" in roles and "assistant" in roles
    assert all(m["content"] for m in data["messages"]), "不该有空消息"


async def test_messages_for_missing_session_is_rejected(client: AsyncClient):
    body = (await client.get("/api/sessions/sess-nope/messages")).json()
    assert body["success"] is False, body
    assert body["error"]["code"] == "SESSION_NOT_FOUND"


async def test_awaiting_human_is_flagged_for_polling(client: AsyncClient):
    """转人工后必须告诉前端「还在等人」，前端据此继续轮询。"""
    session_id, escalation_id = await escalate(client)

    data = (await client.get(f"/api/sessions/{session_id}/messages")).json()["data"]
    assert data["awaiting_human"] is True
    assert data["escalation_id"] == escalation_id
    assert data["status"] == "escalated"


async def test_still_awaiting_human_after_a_reply(client: AsyncClient, agent_client: AsyncClient):
    """人工回了一句但没结单：会话仍算「在等人工」。

    这条守卫的是拆分之后的语义。「人工回复」不再等于「处理完毕」——人工可以连说几句，
    在人工自己点「结束会话」之前，AI 都该继续闭嘴、前端都该继续轮询同步新消息。
    以前这里是反过来的：回一句就把标志摘掉、会话回 active，人工后面再说什么都没人收。
    """
    session_id, escalation_id = await escalate(client)
    await agent_client.post(f"/api/escalations/{escalation_id}/reply", json={"reply": "稍等，我查询下"})

    data = (await client.get(f"/api/sessions/{session_id}/messages")).json()["data"]
    assert data["awaiting_human"] is True, "还没结单，仍应在等人工"
    assert data["escalation_id"] == escalation_id
    assert data["status"] == "escalated"


async def test_awaiting_human_clears_after_close(client: AsyncClient, agent_client: AsyncClient):
    """结单后标志才摘掉——前端据此停止轮询、解锁输入。"""
    session_id, escalation_id = await escalate(client)
    await agent_client.post(f"/api/escalations/{escalation_id}/reply", json={"reply": "已处理"})
    await agent_client.post(f"/api/escalations/{escalation_id}/close")

    data = (await client.get(f"/api/sessions/{session_id}/messages")).json()["data"]
    assert data["awaiting_human"] is False
    assert data["escalation_id"] is None
    assert data["status"] == "active"


# ---------------------------------------------------------------- 删除


async def test_delete_actually_removes_the_session(client: AsyncClient):
    """删除必须真的落库。"""
    session_id = new_session_id()
    await chat(client, session_id, "你好")

    resp = await client.delete(f"/api/sessions/{session_id}")
    body = resp.json()
    assert body["success"] is True, body
    assert body["data"]["deleted"] is True

    # 换个新会话去读，只有真删了才读不到
    async with fresh_db() as db:
        assert await repository.get_session(db, session_id) is None, "会话没有被真的删掉"

    after = (await client.get("/api/sessions")).json()
    assert session_id not in [s["session_id"] for s in after["data"]["sessions"]]

    again = (await client.get(f"/api/sessions/{session_id}/messages")).json()
    assert again["success"] is False


async def test_delete_takes_messages_and_tickets_with_it(client: AsyncClient):
    """会话删了，挂在它上面的消息与工单不能变成孤儿。"""
    session_id, escalation_id = await escalate(client)

    assert (await client.delete(f"/api/sessions/{session_id}")).json()["success"] is True

    async with fresh_db() as db:
        assert await repository.list_messages(db, session_id) == [], "消息成了孤儿"
        assert await repository.get_escalation(db, escalation_id) is None, "工单成了孤儿"
        assert await repository.get_pending_escalation(db, session_id) is None


async def test_delete_missing_session_is_rejected(client: AsyncClient):
    body = (await client.delete("/api/sessions/sess-nope")).json()
    assert body["success"] is False, body
    assert body["error"]["code"] == "SESSION_NOT_FOUND"


async def test_deleting_one_session_leaves_others_alone(client: AsyncClient):
    """别把别人的会话一起删了。"""
    keep = new_session_id()
    drop = new_session_id()
    await chat(client, keep, "你好")
    await chat(client, drop, "你好")

    await client.delete(f"/api/sessions/{drop}")

    async with fresh_db() as db:
        assert await repository.get_session(db, keep) is not None, "误删了其它会话"
