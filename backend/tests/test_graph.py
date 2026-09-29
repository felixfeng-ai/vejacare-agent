"""端到端图流程测试。

这些用例是整个项目最重要的回归网：改任何节点、任何 prompt、任何检索参数，
只要把主链路跑挂了，这里就会红。
"""

from __future__ import annotations

import uuid

import pytest
from langchain_core.messages import HumanMessage
from langgraph.types import Command

pytestmark = pytest.mark.asyncio


def _input(text: str) -> dict:
    return {"messages": [HumanMessage(content=text)]}


async def _run(graph, config, text: str) -> dict:
    return await graph.ainvoke(_input(text), config)


def _last_ai_text(state: dict) -> str:
    from langchain_core.messages import AIMessage

    for message in reversed(state.get("messages") or []):
        if isinstance(message, AIMessage) and message.content:
            return str(message.content)
    return ""


def _ai_count(state: dict) -> int:
    """数 AI 消息条数。用来判断某一轮**有没有新增** AI 发言，比看最后一条可靠。"""
    from langchain_core.messages import AIMessage

    return len([m for m in state.get("messages") or [] if isinstance(m, AIMessage)])


# ---------------------------------------------------------------- 主链路


async def test_logistics_query_full_chain(graph, thread_config):
    """完整链路：意图识别 → RAG → 工具查物流 → 生成回复。"""
    config = thread_config(f"t-{uuid.uuid4().hex[:8]}")
    state = await _run(graph, config, "我的包裹到哪了？订单号 SO20260928001")

    assert state["intent"] == "logistics"
    assert state["slots"]["order_no"] == "SO20260928001"

    # RAG 应该有命中
    assert len(state["kb_docs"]) > 0

    # 工具应该真的被调用，并且查到了真实数据
    assert len(state["tool_results"]) == 1
    tool_result = state["tool_results"][0]
    assert tool_result["name"] == "query_logistics"
    assert tool_result["ok"] is True
    assert tool_result["data"]["tracking_no"] == "LP00123456789"
    assert tool_result["data"]["carrier"] == "4PX"

    # 回复必须引用真实轨迹，而不是编一个
    reply = _last_ai_text(state)
    assert "洛杉矶" in reply or "分拨" in reply
    assert reply.strip()


async def test_chitchat_skips_retrieval_and_tools(graph, thread_config):
    """闲聊走短路：不检索、不调工具。"""
    config = thread_config(f"t-{uuid.uuid4().hex[:8]}")
    state = await _run(graph, config, "你好呀")

    assert state["intent"] == "chitchat"
    assert state["kb_docs"] == []
    assert state["tool_results"] == []
    assert _last_ai_text(state).strip()


async def test_missing_order_no_asks_instead_of_fabricating(graph, thread_config):
    """没给订单号时应该反问，而不是编一个物流状态出来。"""
    config = thread_config(f"t-{uuid.uuid4().hex[:8]}")
    state = await _run(graph, config, "我的包裹怎么还没到")

    assert state["intent"] == "logistics"
    assert not state["slots"].get("order_no")
    assert state["tool_results"] == [], "缺订单号时不该调用查询工具"

    reply = _last_ai_text(state)
    assert "订单号" in reply


async def test_unknown_order_no_reports_honestly(graph, thread_config):
    """查不到的订单要如实说查不到，不能编。"""
    config = thread_config(f"t-{uuid.uuid4().hex[:8]}")
    state = await _run(graph, config, "查一下订单 SO99999999999 的物流")

    assert len(state["tool_results"]) == 1
    assert state["tool_results"][0]["ok"] is False

    reply = _last_ai_text(state)
    assert "SO99999999999" in reply or "未找到" in reply or "没找到" in reply


async def test_return_refund_computes_fee(graph, thread_config):
    """退货运费计算走 calc_refund_fee，金额来自工具而非模型臆测。"""
    config = thread_config(f"t-{uuid.uuid4().hex[:8]}")
    state = await _run(graph, config, "订单 SO20260928001 我想退货，要花多少钱？")

    assert state["intent"] == "return_refund"
    assert len(state["tool_results"]) == 1

    result = state["tool_results"][0]
    assert result["name"] == "calc_refund_fee"
    assert result["ok"] is True
    # 美国站退货运费 $4.99
    assert result["data"]["return_fee"] == 4.99
    assert result["data"]["estimated_refund"] == pytest.approx(89.90 - 4.99, abs=0.01)


async def test_deprecated_doc_never_retrieved(graph, thread_config):
    """失效文档必须被过滤掉——这是知识库版本治理的核心断言。"""
    config = thread_config(f"t-{uuid.uuid4().hex[:8]}")
    state = await _run(graph, config, "你们的运费标准是多少？满多少包邮？")

    sources = {doc.get("source") for doc in state["kb_docs"]}
    assert "_deprecated-shipping-2025.md" not in sources
    # 现行文档里写的包邮门槛是 $29
    combined = "".join(doc.get("text", "") for doc in state["kb_docs"])
    assert "$29" in combined or "29" in combined


# ---------------------------------------------------------------- 转人工


async def test_escalation_interrupts_and_holds_context(graph, thread_config):
    """用户要求人工 → 图挂起，上下文完整落库。"""
    from app.db import repository
    from app.db.session import get_session_factory

    session_id = f"t-{uuid.uuid4().hex[:8]}"
    config = thread_config(session_id)
    state = await _run(graph, config, "我要转人工，找你们真人客服")

    assert state["intent"] == "human_agent"
    assert state["escalated"] is True
    assert state["awaiting_human"] is True
    assert state["escalation_id"].startswith("esc_")

    # 图确实停在 interrupt 上，而不是跑完了
    snapshot = await graph.aget_state(config)
    assert snapshot.next, "图应当停在 escalation_wait 节点上"

    # 工单落库，且带完整上下文
    factory = get_session_factory()
    async with factory() as db:
        escalation = await repository.get_escalation(db, state["escalation_id"])
        assert escalation is not None
        assert escalation.status == "pending"
        assert escalation.reason == "user_requested"
        assert escalation.summary

        session = await repository.get_session(db, session_id)
        assert session.status == "escalated"


async def test_escalation_create_is_idempotent(graph, thread_config):
    """interrupt 会让节点重跑，但绝不能建出两张工单。"""
    from app.db import repository
    from app.db.session import get_session_factory

    session_id = f"t-{uuid.uuid4().hex[:8]}"
    await _run(graph, thread_config(session_id), "转人工")
    await _run(graph, thread_config(session_id), "转人工")

    factory = get_session_factory()
    async with factory() as db:
        pending = await repository.list_escalations(db, status="pending")
        mine = [e for e in pending if e.session_id == session_id]
        assert len(mine) == 1, f"应当只有 1 张待处理工单，实际 {len(mine)} 张"


async def test_resume_after_human_reply(graph, thread_config):
    """结单后 resume：挂起被解开、人工回复进入状态、并且邀请用户评分。"""
    session_id = f"t-{uuid.uuid4().hex[:8]}"
    config = thread_config(session_id)

    state = await _run(graph, config, "我要人工客服")
    escalation_id = state["escalation_id"]
    ai_before = _ai_count(state)

    human_reply = "已为您加急处理，24 小时内会有物流更新"
    resumed = await graph.ainvoke(
        Command(resume={"reply": human_reply, "agent": "客服小美"}), config
    )

    # 挂起标志被清掉，人工回复进入状态
    assert resumed["awaiting_human"] is False
    assert resumed["escalated"] is False
    assert resumed["human_reply"] == human_reply
    assert escalation_id

    # 结单后仍然要请用户评分，否则 /admin 的满意度看板会漏掉所有转人工的会话
    assert resumed["request_feedback"] is True

    # **不再由 LLM 产出收尾**：人工已经说完，模型没有信息可生成，产出必然是复读
    # （实测人工说「稍等，我查询下」，AI 收尾把这句原样抄了一遍）。
    # 比的是"有没有新增"，不是"最后一条是不是空"——最后一条 AI 消息是更早那句
    # 「已为您转接人工客服」的通知，它本来就该留着。
    assert _ai_count(resumed) == ai_before, "resume 不该再产出任何 AI 消息，复读的源头就是它"

    # 也不该再往 state 里注入"人工说了什么"的 SystemMessage —— 那正是复读的来源
    from langchain_core.messages import SystemMessage

    assert not [m for m in resumed["messages"] if isinstance(m, SystemMessage)]


# ---------------------------------------------------------------- 多轮


async def test_multi_turn_context_is_retained(graph, thread_config):
    """第二轮不给订单号，也要能从上下文里捞出来。"""
    config = thread_config(f"t-{uuid.uuid4().hex[:8]}")

    first = await _run(graph, config, "我的包裹到哪了？订单号 SO20260928001")
    assert first["slots"]["order_no"] == "SO20260928001"

    second = await _run(graph, config, "那它什么时候能送到？")

    # 多轮上下文没丢：轮数在涨，历史消息在累积
    assert second["turn_count"] == 2
    assert len(second["messages"]) >= 4
    assert _last_ai_text(second).strip()


async def test_feedback_request_does_not_leak_into_next_turn(graph, thread_config):
    """评分邀请只在该弹的那一轮弹一次，不能跨轮残留。

    `request_feedback` 是存在 checkpoint 里的跨轮状态，而写入方（feedback 节点）
    只会把它置 True，没有任何地方负责置回 False。于是只要某一轮弹过评分卡，
    之后每一轮的 chat.py 都会再发一次 feedback_request —— 前端表现为评分卡反复出现。

    判据选转人工分支：它从意图识别直接进 escalation，整轮不经过评估器与
    feedback 节点，因此本轮本来就不该邀请评分。若不在每轮开头清理，这个字段
    会带着上一轮的 True 走到这里。
    """
    config = thread_config(f"t-{uuid.uuid4().hex[:8]}")

    first = await _run(graph, config, "我的包裹到哪了？订单号 SO20260928001")
    assert first["request_feedback"] is True, "这一轮正常结束，应当邀请评分"

    second = await _run(graph, config, "我要转人工，找你们真人客服")
    assert second["intent"] == "human_agent", "这一轮应当走转人工分支"
    assert second["request_feedback"] is False, "上一轮的评分邀请不该跨轮残留"


async def test_policy_question_does_not_borrow_previous_order(graph, thread_config):
    """纯政策问题不能拿上一轮的订单号去查真实数据。

    线上实测：先问「我的包裹到哪了？订单号 SO20260928001」，再问
    「当前平台的退换货政策是什么，什么商品允许退换货」——第二句一个字都没提订单，
    回复里却出现了「费用说明：从退款中扣除 USD 4.99 作为退回运费」。
    那 4.99 来自上一轮那个美国订单的真实运费表，数字全是真的，
    只是回答的不是用户问的问题，而用户无从分辨。

    根因是槽位跨轮继承：桩把上一轮和本轮拼成整段再抽槽位，
    而 prompt 当时写的也正是「从整段对话里抽取」。

    这里断言的是**整条链路**，不是桩的单个函数——因为真正要守住的是
    「工具不要被一个没人问的订单号触发」，那要槽位与工具决策同时对才行。
    """
    config = thread_config(f"t-{uuid.uuid4().hex[:8]}")

    first = await _run(graph, config, "我的包裹到哪了？订单号 SO20260928001")
    assert first["slots"]["order_no"] == "SO20260928001"
    # 订单号长得也像运单号（两个字母 + 一串数字），曾被一并抽成 tracking_no，
    # 前端槽位标签上于是显示成两个一样的值
    assert first["slots"].get("tracking_no") is None, "订单号不该被当成运单号"

    second = await _run(graph, config, "当前平台的退换货政策是什么，什么商品允许退换货")

    assert second["intent"] == "return_refund"
    assert not second["slots"].get("order_no"), "本轮没提订单，不该继承上一轮的订单号"
    assert second["tool_results"] == [], "没有订单号就不该调退货运费工具"

    reply = _last_ai_text(second)
    assert "SO20260928001" not in reply
    assert "4.99" not in reply, "这个数字只可能来自上一单的退货运费表"


async def test_followup_after_a_ticket_does_not_inherit_the_handoff(graph, thread_config):
    """上一轮要过人工，不代表这一轮还要人工。

    线上实测：用户先要过真人客服，工单结掉之后接着问「平台的退换货政策是怎样的」，
    AI 答完，用户追了一句「再详细点」——这一句被判成 human_agent，AI 当场闭嘴转人工。

    这句话里没有半个字提到人工。判成转人工是因为意图分类会看上文，而看上文时
    只看不筛：上一轮的「真人客服」被原样顺延到这一轮。**追问继承的是主题，不是诉求**——
    诉求是那一轮的事，说完了就完了。

    两处都得对才守得住：提示词里的规则（真模型走这条）、桩里的规则（测试走这条）。
    """
    session_id = f"t-{uuid.uuid4().hex[:8]}"
    config = thread_config(session_id)

    # 1. 先要一次人工，再把工单结掉
    first = await _run(graph, config, "我要转人工，找你们真人客服")
    assert first["intent"] == "human_agent"
    await graph.ainvoke(Command(resume={"reply": "已处理", "agent": "客服小美"}), config)

    # 2. 结单后接着问政策
    policy = await _run(graph, config, "当前平台的退换货政策是什么，什么商品允许退换货")
    assert policy["intent"] == "return_refund"

    # 3. 追问。这句必须接上「退换货」这个主题
    followup = await _run(graph, config, "再详细点")

    assert followup["intent"] != "human_agent", "追问不该继承上一轮的转人工诉求"
    assert followup["intent"] == "return_refund", "追问该接上刚聊完的那个主题"
    assert followup["escalated"] is False
    assert followup["awaiting_human"] is False

    # 也不能退化成 chitchat —— 那会绕开知识库，AI 只能凭空作答
    assert followup["kb_docs"], "追问判成闲聊就查不到知识库了"
    assert _last_ai_text(followup).strip()
