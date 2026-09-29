"""转人工接口的生命周期测试。

`tests/test_graph.py` 覆盖图的挂起/恢复，这一层覆盖 HTTP：
`/api/chat/stream` → 建单挂起 → `/reply`（可连发）→ `/close` → 收尾。

这个文件里大半是用例是**回归守卫**，守的是三个真实发生过的缺陷：

1. 写操作走 `Depends(get_db)`，而 `get_db` 过去从不提交——「人工回复落库」与「工单置为
   已解决」在请求结束时被静默回滚，只有同一函数里用 `session_scope()` 写的那部分留了
   下来。接口报成功，数据自相矛盾。
2. 回复与结单曾是同一个动作：人工说一句「稍等，我查询下」就等于宣告处理完毕，工单
   当场关掉、会话交还 AI，那句占位话还被 LLM 抄成了「AI 的收尾回复」。
3. 等待人工期间用户说的话**根本没落库**：`escalated` 分支在 `add_message` 之前就 return。

一律从 chat 接口进，不走 graph fixture 直接驱动图：只有走完 API 层，用户与助手的消息
才会真正写进 `messages` 表，「人工接手时能看到完整对话」这一条才测得到。
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


async def _reply(agent_client: AsyncClient, escalation_id: str, text: str, agent: str = "客服小美"):
    return await agent_client.post(
        f"/api/escalations/{escalation_id}/reply", json={"reply": text, "agent": agent}
    )


# ---------------------------------------------------------------- 人工回复

async def test_human_reply_is_persisted_without_closing(client: AsyncClient, agent_client: AsyncClient):
    """回一句只算「说了一句话」：消息落库，工单仍是待处理。"""
    session_id, escalation_id = await escalate(client)

    resp = await _reply(agent_client, escalation_id, "已为您加急处理，24 小时内会有物流更新")
    body = resp.json()
    assert resp.status_code == 200, resp.text
    assert body["success"] is True, body
    assert body["data"]["status"] == "pending", "回复不该把工单结掉"

    async with fresh_db() as db:
        escalation = await repository.get_escalation(db, escalation_id)
        assert escalation.status == "pending", "工单不该被回复动作结掉"
        assert escalation.human_reply == "已为您加急处理，24 小时内会有物流更新"
        assert escalation.agent_name == "客服小美"
        assert escalation.resolved_at is None, "还没结单，不该有解决时间"

        messages = await repository.list_messages(db, session_id)
        human_msgs = [m for m in messages if m.role == "human_agent"]
        assert len(human_msgs) == 1, f"人工回复应当落库 1 条，实际 {len(human_msgs)} 条"
        assert human_msgs[0].content == "已为您加急处理，24 小时内会有物流更新"

        session = await repository.get_session(db, session_id)
        assert session.status == "escalated", "还没结单，会话不该回到 active"


async def test_human_can_reply_several_times(client: AsyncClient, agent_client: AsyncClient):
    """人工可以连说几句，中间不会被结单。

    「稍等，我查询下」→「查到了，明天下午到」是最正常的两句话。以前第一句就把工单
    关掉了，第二句会被 ESCALATION_ALREADY_RESOLVED 挡在门外。
    """
    session_id, escalation_id = await escalate(client)

    first = await _reply(agent_client, escalation_id, "稍等，我查询下")
    assert first.json()["success"] is True, first.text
    assert first.json()["data"]["status"] == "pending"

    second = await _reply(agent_client, escalation_id, "查到了，明天下午到")
    assert second.json()["success"] is True, second.text

    async with fresh_db() as db:
        escalation = await repository.get_escalation(db, escalation_id)
        assert escalation.status == "pending", "连发两条之后工单仍应是待处理"

        messages = await repository.list_messages(db, session_id)
        contents = [m.content for m in messages if m.role == "human_agent"]
        assert contents == ["稍等，我查询下", "查到了，明天下午到"], (
            f"两条人工回复都该按顺序留在对话里，实际 {contents}"
        )


async def test_reply_to_missing_ticket_is_rejected(client: AsyncClient, agent_client: AsyncClient):
    resp = await agent_client.post("/api/escalations/esc-does-not-exist/reply", json={"reply": "你好"})
    body = resp.json()
    assert body["success"] is False, body
    assert body["error"]["code"] == "ESCALATION_NOT_FOUND"


async def test_empty_reply_is_rejected(client: AsyncClient, agent_client: AsyncClient):
    """空回复在校验层拦掉。"""
    _, escalation_id = await escalate(client)
    resp = await agent_client.post(f"/api/escalations/{escalation_id}/reply", json={"reply": ""})
    assert resp.status_code == 422


# ---------------------------------------------------------------- 结单

async def test_close_resolves_and_persists_a_closing_message(
    client: AsyncClient, agent_client: AsyncClient
):
    """结单：工单置为已解决、会话回到 active、收尾消息落库。"""
    session_id, escalation_id = await escalate(client)
    await _reply(agent_client, escalation_id, "已为您加急处理")

    resp = await agent_client.post(f"/api/escalations/{escalation_id}/close")
    body = resp.json()
    assert body["success"] is True, body
    assert body["data"]["status"] == "resolved"
    assert body["data"]["request_feedback"] is True

    async with fresh_db() as db:
        escalation = await repository.get_escalation(db, escalation_id)
        assert escalation.status == "resolved", "工单没有置为已解决"
        assert escalation.human_reply == "已为您加急处理", "结单不该抹掉人工回复"
        assert escalation.resolved_at is not None, "缺少解决时间"

        session = await repository.get_session(db, session_id)
        assert session.status == "active", "会话状态没有回到 active"

        messages = await repository.list_messages(db, session_id)
        assistant_msgs = [m.content for m in messages if m.role == "assistant"]
        assert any(m == body["data"]["closing_message"] for m in assistant_msgs), (
            "收尾回复没有落库，前端一刷新就只剩人工那句"
        )


async def test_closing_is_not_a_copy_of_the_human_reply(
    client: AsyncClient, agent_client: AsyncClient
):
    """收尾不能是人工那句话的复读。

    实测过的现场：人工说「稍等，我查询下」，AI 收尾输出「收到，人工同事已经为您处理。
    我这边补充一下：稍等，我查询下」，而工单就以这句占位话结单了。收尾改由接口确定性
    拼装、不再交给 LLM 之后，这类输出从结构上不可能再出现。
    """
    session_id, escalation_id = await escalate(client)
    await _reply(agent_client, escalation_id, "稍等，我查询下")

    closing = (await agent_client.post(f"/api/escalations/{escalation_id}/close")).json()["data"][
        "closing_message"
    ]
    assert closing, "结单应当有一句收尾"
    assert "稍等，我查询下" not in closing, f"收尾复读了人工的原话：{closing}"

    async with fresh_db() as db:
        messages = await repository.list_messages(db, session_id)
        assert not any(m.role == "assistant" and "稍等，我查询下" in m.content for m in messages), (
            "对话里出现了复读人工原话的 AI 消息"
        )


async def test_close_without_reply_produces_no_closing_message(
    client: AsyncClient, agent_client: AsyncClient
):
    """人工一句话没说就结单：没话说就别说，不产出空的 AI 气泡。"""
    session_id, escalation_id = await escalate(client)

    resp = await agent_client.post(f"/api/escalations/{escalation_id}/close")
    assert resp.json()["data"]["closing_message"] == ""

    async with fresh_db() as db:
        messages = await repository.list_messages(db, session_id)
        assert not any(m.role == "assistant" and m.content.strip() == "" for m in messages)


async def test_close_twice_is_rejected(client: AsyncClient, agent_client: AsyncClient):
    _, escalation_id = await escalate(client)
    assert (await agent_client.post(f"/api/escalations/{escalation_id}/close")).json()["success"] is True

    body = (await agent_client.post(f"/api/escalations/{escalation_id}/close")).json()
    assert body["success"] is False, body
    assert body["error"]["code"] == "ESCALATION_ALREADY_RESOLVED"


async def test_reply_after_close_is_rejected(client: AsyncClient, agent_client: AsyncClient):
    """结单之后不能再回复，否则人工台会给一个已经关掉的工单追加发言。"""
    _, escalation_id = await escalate(client)
    await agent_client.post(f"/api/escalations/{escalation_id}/close")

    body = (await _reply(agent_client, escalation_id, "再补一句")).json()
    assert body["success"] is False, body
    assert body["error"]["code"] == "ESCALATION_ALREADY_RESOLVED"


# ---------------------------------------------------------------- 挂起期间

async def test_ai_stays_quiet_while_awaiting_human(client: AsyncClient):
    """转人工后、人工接手前，AI 不能再插话，否则会和人工客服抢答。"""
    session_id, _ = await escalate(client)

    resp = await client.post("/api/chat/stream", json={"session_id": session_id, "message": "在吗？"})
    events = parse_sse(resp.text)

    error = next((e for e in events if e.get("type") == "error"), None)
    assert error is not None, f"应当拒绝继续对话，收到的事件：{events}"
    assert error["code"] == "AWAITING_HUMAN"
    assert not any(e.get("type") == "token" for e in events), "AI 不该再产出发言"


async def test_user_message_while_waiting_is_persisted_and_counted(
    client: AsyncClient, agent_client: AsyncClient
):
    """等待人工期间用户补充的话要落库，并且客服端要能看出「有新消息」。

    静默丢数据的回归守卫：`escalated` 分支原来在 `add_message` 之前就 return，
    用户补的订单号、地址既不进对话记录也不进工单，直接消失。而客服端唯一的信号
    就是这个未读数——工单详情是点开那一刻的快照，不会自己更新。
    """
    session_id, escalation_id = await escalate(client)
    supplement = "订单号是 SO20260928001"

    events = await chat(client, session_id, supplement)
    assert any(e.get("code") == "AWAITING_HUMAN" for e in events), f"应当提示已转达人工：{events}"

    async with fresh_db() as db:
        messages = await repository.list_messages(db, session_id)
        assert any(m.content == supplement for m in messages), "用户在等待期间补的话被丢掉了"

    listing = (await agent_client.get("/api/escalations", params={"status": "pending"})).json()
    row = next(e for e in listing["data"]["escalations"] if e["id"] == escalation_id)
    assert row["unread_count"] == 1, "客服端看不出用户又说话了"


async def test_ai_stays_quiet_after_reply_until_closed(client: AsyncClient, agent_client: AsyncClient):
    """人工回了一句但还没结单：用户能说话，接话的不是 AI。"""
    session_id, escalation_id = await escalate(client)
    await _reply(agent_client, escalation_id, "稍等，我查询下")

    events = await chat(client, session_id, "好的，那我等着")
    assert not any(e.get("type") == "token" for e in events), "人工还没结单，AI 不该抢答"
    assert any(e.get("code") == "AWAITING_HUMAN" for e in events)

    async with fresh_db() as db:
        session = await repository.get_session(db, session_id)
        assert session.status == "escalated"
        messages = await repository.list_messages(db, session_id)
        assert any(m.content == "好的，那我等着" for m in messages), "用户的话要落库，客服才看得到"


async def test_chat_resumes_after_ticket_is_closed(client: AsyncClient, agent_client: AsyncClient):
    """结单之后会话回到 active，用户再说话 AI 要能正常接。"""
    session_id, escalation_id = await escalate(client)
    await _reply(agent_client, escalation_id, "已处理")
    await agent_client.post(f"/api/escalations/{escalation_id}/close")

    events = await chat(client, session_id, "我的包裹到哪了？订单号 SO20260928001")
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


async def test_resolve_unparks_the_graph(client: AsyncClient, agent_client: AsyncClient):
    """不回复直接完成，也必须把挂起的图唤醒。

    这里以前只改会话状态、不唤醒图，留下一个坏状态：会话是 active、图却还挂在
    `interrupt()` 上。而且那个坏状态只在**下一次对话**时才暴露，所以这条用例走一遍
    「结单 → 用户再说话」才算数。
    """
    session_id, escalation_id = await escalate(client)
    await agent_client.post(f"/api/escalations/{escalation_id}/resolve")

    events = await chat(client, session_id, "那我的包裹现在到哪了")
    assert not any(e.get("type") == "error" for e in events), f"图没被唤醒，事件：{events}"
    assert any(e.get("type") == "token" for e in events), "AI 应当恢复应答"


# ---------------------------------------------------------------- 人工台读取

async def test_pending_list_only_shows_unhandled(client: AsyncClient, agent_client: AsyncClient):
    """人工台的工作列表只该看到未处理的工单。"""
    _, escalation_id = await escalate(client)

    pending = (await agent_client.get("/api/escalations", params={"status": "pending"})).json()
    assert pending["success"] is True
    assert escalation_id in [e["id"] for e in pending["data"]["escalations"]]

    # 回复过但没结单的，仍然属于待办——这正是拆开之后新增的那个状态
    await _reply(agent_client, escalation_id, "稍等")
    still_pending = (await agent_client.get("/api/escalations", params={"status": "pending"})).json()
    assert escalation_id in [e["id"] for e in still_pending["data"]["escalations"]], (
        "回复过但未结单的工单不该从待办里消失"
    )

    await agent_client.post(f"/api/escalations/{escalation_id}/close")

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
    assert data["unread_count"] == 0, "详情也要带未读数，缺了它界面只能显示 0"

    context = data["context"]
    assert context["intent"] == "human_agent"
    assert context["messages"], "上下文里应当带完整对话"
    assert any(m["content"] == ESCALATE_MESSAGE for m in context["messages"])


async def test_detail_unread_count_matches_the_queue(
    client: AsyncClient, agent_client: AsyncClient
):
    """队列说有几条新消息，点进去详情得是同一个数。

    两个接口各算各的时候漏过一处：队列带了 `unread_count`，详情没带。
    界面上就是「列表挂着『2 条新消息』，点进去一条也没有」——客服只能以为是自己看错了。
    """
    session_id, escalation_id = await escalate(client)
    await chat(client, session_id, "订单号 SO20260928001")

    queue = (await agent_client.get("/api/escalations?status=pending")).json()["data"]
    row = next(r for r in queue["escalations"] if r["id"] == escalation_id)

    detail = (await agent_client.get(f"/api/escalations/{escalation_id}")).json()["data"]

    assert row["unread_count"] == 1, "用户补了一句，队列该计到"
    assert detail["unread_count"] == row["unread_count"], "队列与详情对不上"


async def test_unread_count_is_zero_once_closed(client: AsyncClient, agent_client: AsyncClient):
    """结单之后不再有「新消息」这回事——会话已经回到 AI 手上。"""
    session_id, escalation_id = await escalate(client)
    await chat(client, session_id, "订单号 SO20260928001")
    await agent_client.post(f"/api/escalations/{escalation_id}/close")

    detail = (await agent_client.get(f"/api/escalations/{escalation_id}")).json()["data"]
    assert detail["unread_count"] == 0


async def test_summary_reason_matches_the_ticket(client: AsyncClient, agent_client: AsyncClient):
    """摘要里的触发原因必须和工单记录的 reason 一致。

    以前摘要在建单那一刻自己读 state 里的 `escalation_reason`，而落库用的是
    `or "user_requested"` 兜底值，于是同一张工单详情写「用户主动要求人工」、
    摘要写「触发原因：未明确」，同一份原因两处对不上。
    """
    _, escalation_id = await escalate(client)

    data = (await agent_client.get(f"/api/escalations/{escalation_id}")).json()["data"]
    assert data["reason_label"] in data["summary"], (
        f"摘要「{data['summary']}」里没有工单的触发原因「{data['reason_label']}」"
    )
