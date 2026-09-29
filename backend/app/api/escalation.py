"""转人工工单 API。

生命周期拆成两个动作，**不要再合并回去**：

- `/reply`：记一条人工回复。可重复调用，工单保持 pending。
- `/close`：结单。唤醒挂起的图 → 给用户一句确定性收尾 → 会话回到 active。

两者合并成一个动作时，人工说一句「稍等，我查询下」就等于宣告处理完毕——工单当场
关掉、会话交还 AI，而那句占位话还被 LLM 抄成了「AI 的收尾回复」。回复是「我说了
一句话」，结单是「这事办完了」，不是一回事。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.db.session import get_db, session_scope
from app.graph.builder import get_graph, thread_config
from app.graph.prompts import ESCALATION_REASONS
from app.schemas import EscalationReplyRequest, fail, ok
from app.security import ROLE_ADMIN, ROLE_AGENT, require_role
from app.utils import to_iso

logger = logging.getLogger(__name__)

# 整个工单路由都归后台：队列、详情、回复、结单没有一个是给用户端用的。
# 挂在 router 上而不是逐个函数上——四个接口的可见性是同一件事，
# 分散写就有"新加一个接口忘了加依赖"的漏网机会。
router = APIRouter(
    prefix="/api/escalations",
    tags=["escalations"],
    dependencies=[Depends(require_role(ROLE_AGENT, ROLE_ADMIN))],
)


def _escalation_out(escalation, *, include_context: bool = False) -> dict:
    payload = {
        "id": escalation.id,
        "session_id": escalation.session_id,
        "reason": escalation.reason,
        "reason_label": ESCALATION_REASONS.get(escalation.reason, "转人工"),
        "status": escalation.status,
        "summary": escalation.summary,
        "human_reply": escalation.human_reply,
        "agent_name": escalation.agent_name,
        "created_at": to_iso(escalation.created_at),
        "resolved_at": to_iso(escalation.resolved_at),
    }
    if include_context:
        try:
            payload["context"] = json.loads(escalation.context_json or "{}")
        except json.JSONDecodeError:
            payload["context"] = {}
    return payload


async def _unread_count(db: AsyncSession, escalation) -> int:
    """工单建好之后用户又说了几句。已结单的恒为 0。

    未结单的工单才关心「用户又说话了没有」：用户在等待人工期间补的订单号、地址、
    诉求全在这些消息里，而工单详情是点开那一刻的快照、不会自己更新。没有这个计数，
    客服不主动刷新就永远不知道用户又开口了。

    队列和详情都要带这个字段——详情缺了它，界面就只能显示 0，
    于是「列表说有新消息、点进去一条新消息都看不到」。
    """
    if escalation.status != "pending":
        return 0
    return await repository.count_user_messages_since(
        db, escalation.session_id, escalation.created_at
    )


@router.get("")
async def list_escalations(
    status: str | None = None, db: AsyncSession = Depends(get_db)
) -> dict:
    rows = await repository.list_escalations(db, status)
    items = []
    for row in rows:
        payload = _escalation_out(row)
        payload["unread_count"] = await _unread_count(db, row)
        items.append(payload)
    return ok({"escalations": items})


@router.get("/{escalation_id}")
async def get_escalation(escalation_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    escalation = await repository.get_escalation(db, escalation_id)
    if escalation is None:
        return fail("ESCALATION_NOT_FOUND", "工单不存在")

    payload = _escalation_out(escalation, include_context=True)
    payload["unread_count"] = await _unread_count(db, escalation)
    messages = await repository.list_messages(db, escalation.session_id)
    payload["context"]["messages"] = [
        {"role": m.role, "content": m.content, "created_at": to_iso(m.created_at)}
        for m in messages
    ]
    return ok(payload)


@router.post("/{escalation_id}/reply")
async def reply_escalation(
    escalation_id: str, payload: EscalationReplyRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    """记一条人工回复。**不结单、不唤醒图**——那个人工要显式点「结束会话」。

    可重复调用：人工先回一句「稍等，我查询下」，查到了再回一句结论，两句都留在
    对话里。此前这两句里的第一句就会把工单关掉，第二句根本发不出来。
    """
    escalation = await repository.get_escalation(db, escalation_id)
    if escalation is None:
        return fail("ESCALATION_NOT_FOUND", "工单不存在")
    if escalation.status != "pending":
        return fail("ESCALATION_ALREADY_RESOLVED", "该工单已结单")

    # 人工回复落库，前端据此渲染成「人工客服」气泡
    await repository.add_message(
        db,
        session_id=escalation.session_id,
        role="human_agent",
        content=payload.reply,
        meta={"agent": payload.agent},
    )
    await repository.record_human_reply(
        db, escalation, reply=payload.reply, agent_name=payload.agent
    )

    return ok(
        {
            "escalation_id": escalation_id,
            "session_id": escalation.session_id,
            "status": "pending",
            "agent": payload.agent,
        }
    )


@router.post("/{escalation_id}/close")
async def close_escalation(escalation_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """结单。人工唯一主动结束工单的入口，人工一句没回过也能调（＝无需回复直接完成）。"""
    escalation = await repository.get_escalation(db, escalation_id)
    if escalation is None:
        return fail("ESCALATION_NOT_FOUND", "工单不存在")
    if escalation.status != "pending":
        return fail("ESCALATION_ALREADY_RESOLVED", "该工单已结单")

    return ok({"escalation_id": escalation_id, **(await _close(db, escalation))})


@router.post("/{escalation_id}/resolve")
async def resolve_escalation(escalation_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """「无需回复，直接完成」。与 `/close` 是同一套结单逻辑。

    保留这个别名是为了兼容已经发出去的前端产物（用户浏览器里可能还留着旧 bundle），
    它和 `/close` 唯一的差别就是人工没回复过，于是不产出收尾文案。

    以前这里只改状态、**不唤醒图**，留下一个坏状态：会话是 active、图却还挂在
    `interrupt()` 上，用户下一条消息会撞上去。现在两条路都走 `_close`。
    """
    escalation = await repository.get_escalation(db, escalation_id)
    if escalation is None:
        return fail("ESCALATION_NOT_FOUND", "工单不存在")
    if escalation.status != "pending":
        return fail("ESCALATION_ALREADY_RESOLVED", "该工单已结单")

    result = await _close(db, escalation)
    return ok({"escalation_id": escalation_id, "status": result["status"]})


async def _close(db: AsyncSession, escalation) -> dict:
    """结单的共用实现：落状态 → 唤醒图 → 补一句收尾。

    顺序是有讲究的：**先提交、再唤醒图**。节点内部会另开 `session_scope()` 写库，
    SQLite 同一时刻只允许一个写事务，本会话此刻持有未提交的写锁，两边会撞成
    `database is locked`（实测直接 500）。而且结单语义先落盘也更安全：
    万一图恢复失败，结单不会跟着回滚。
    """
    session_id = escalation.session_id
    agent_name = escalation.agent_name or ""
    last_reply = escalation.human_reply or ""

    await repository.resolve_escalation(db, escalation)
    await repository.set_session_status(db, session_id, "active")
    await db.commit()

    await _resume_graph(session_id, last_reply, agent_name)

    closing_text = _closing_text(last_reply, agent_name)
    if closing_text:
        async with session_scope() as write_db:
            await repository.add_message(
                write_db, session_id=session_id, role="assistant", content=closing_text
            )

    return {
        "session_id": session_id,
        "status": "resolved",
        "agent": agent_name,
        "closing_message": closing_text,
        "request_feedback": True,
    }


async def _resume_graph(session_id: str, reply: str, agent_name: str) -> None:
    """唤醒挂起在 `interrupt()` 的图。

    **必须做**，哪怕人工一句话都没说：图此刻停在中断点上，只把会话状态改成 active
    就不管了的话，用户下一条消息会撞上一个未恢复的图。原来的 `/resolve` 就漏了这一步。
    """
    try:
        graph = await get_graph()
        await graph.ainvoke(
            Command(resume={"reply": reply, "agent": agent_name}),
            thread_config(session_id),
        )
    except Exception:  # noqa: BLE001 — 图恢复失败不该让结单失败，人工的处理已经落了库
        logger.exception("恢复图失败 session=%s", session_id)


def _closing_text(last_reply: str, agent_name: str) -> str:
    """结单时给用户的一句收尾。**刻意不走 LLM。**

    人工已经把话说完了，模型在这件事上没有信息可生成，产出必然是改写或复读——实测
    人工说「稍等，我查询下」，AI 收尾把这句原样抄了一遍还加了句「如还有其他问题，
    随时找我」。省掉这次调用，"AI 说了什么"就变成可预测、可测试的，人工的原话也
    已经作为「人工客服」气泡摆在上面了，不需要再复述。

    人工没回复过（"无需回复，直接完成"）时不产出任何收尾消息——没话说就别说。
    """
    if not last_reply:
        return ""
    who = f"（{agent_name}）" if agent_name else ""
    return f"人工客服{who}已处理完毕。还有其他问题随时找我。"
