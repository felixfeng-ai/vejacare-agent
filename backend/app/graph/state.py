"""LangGraph 图状态定义。

State 是整个 Agent 的单一数据源：每个节点读取它、返回增量补丁，LangGraph 负责合并。
`messages` 用 `add_messages` reducer 做追加合并，其余字段默认「后写覆盖」。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.config import get_config
from langgraph.graph.message import add_messages

# ---------------------------------------------------------------- 意图枚举

Intent = Literal[
    "logistics",       # 物流跟踪
    "return_refund",   # 退换货
    "customs_duty",    # 关税政策
    "size_fit",        # 尺码选择
    "payment",         # 支付失败
    "coupon",          # 优惠券
    "order_change",    # 订单修改
    "human_agent",     # 用户明确要求人工
    "chitchat",        # 闲聊/寒暄/无法归类
]

INTENT_LABELS: dict[str, str] = {
    "logistics": "物流跟踪",
    "return_refund": "退换货",
    "customs_duty": "关税政策",
    "size_fit": "尺码选择",
    "payment": "支付失败",
    "coupon": "优惠券",
    "order_change": "订单修改",
    "human_agent": "转人工",
    "chitchat": "闲聊",
}

#: 需要查知识库的意图（闲聊和转人工不走 RAG）
INTENTS_NEEDING_KB: frozenset[str] = frozenset(
    {"logistics", "return_refund", "customs_duty", "size_fit", "payment", "coupon", "order_change"}
)

# ---------------------------------------------------------------- 槽位

SlotKey = Literal["order_no", "tracking_no", "sku", "country", "amount", "email"]


class Slots(TypedDict, total=False):
    """从用户话里抽出的关键实体。全 Optional，抽不到就是缺。"""

    order_no: str | None
    tracking_no: str | None
    sku: str | None
    country: str | None
    amount: float | None
    email: str | None


# ---------------------------------------------------------------- 子结构


class KbDoc(TypedDict, total=False):
    """一条检索命中的知识片段。"""

    id: str
    title: str
    source: str
    text: str
    snippet: str
    score: float
    vector_rank: int | None
    bm25_rank: int | None
    rerank_score: float | None


class ToolResult(TypedDict, total=False):
    """一次工具调用的结果。"""

    name: str
    args: dict[str, Any]
    ok: bool
    summary: str          # 给前端展示的中文一句话
    data: dict[str, Any]  # 结构化原始结果，回填给 LLM 用
    error: str | None


# ---------------------------------------------------------------- 主状态


class AgentState(TypedDict, total=False):
    """贯穿整张图的会话状态。"""

    # --- 对话 ---
    messages: Annotated[list[BaseMessage], add_messages]
    session_id: str

    # --- 意图理解 ---
    intent: Intent
    slots: Slots

    # --- 检索与工具 ---
    kb_docs: list[KbDoc]
    tool_results: list[ToolResult]

    # --- 转人工 ---
    escalated: bool
    escalation_reason: str
    escalation_id: str
    awaiting_human: bool       # 图被 interrupt() 挂起，等人工接入
    human_reply: str | None    # 人工客服的回复，resume 时注入

    # --- 本轮评估 ---
    turn_count: int
    unresolved_turns: int      # 同一问题连续未解决的轮数
    dissatisfied_count: int    # 用户连续表达不满的次数
    resolved: bool             # 本轮是否解决了用户的问题
    needs_clarification: bool  # 是否在等用户补充信息

    # --- 收尾 ---
    final_text: str
    request_feedback: bool
    satisfied: bool | None
    feedback_rating: int | None
    feedback_comment: str | None


def resolve_session_id(state: AgentState) -> str:
    """取本次调用的会话 id：先看 state，再回退到 checkpointer 的 thread_id。

    两条路取到的是同一个值——API 层就是用同一个 session_id 建的会话和 thread。
    但入口不止 API 一个（脚本、评测、测试都可能只给 thread_id），
    只认 state 的话，漏传就会静默把工单挂到空字符串上，人工台再也找不到它。

    这里刻意用 `get_config()` 从上下文取，而不是在节点签名上加 `config` 参数：
    本文件所有模块都有 `from __future__ import annotations`，注解在运行时是字符串，
    LangGraph 认不出 `config: RunnableConfig`，参数永远收到 None（只会多一条 warning）。
    """
    session_id = state.get("session_id")
    if session_id:
        return str(session_id)

    try:
        configurable = get_config().get("configurable") or {}
    except RuntimeError:
        # 不在图里执行（如单测直接调节点）时没有上下文，只能认 state
        return ""

    return str(configurable.get("thread_id") or "")


def new_state(session_id: str, messages: list[BaseMessage] | None = None) -> AgentState:
    """构造一次全新调用的初始状态。"""
    return AgentState(
        session_id=session_id,
        messages=messages or [],
        intent="chitchat",
        slots=Slots(),
        kb_docs=[],
        tool_results=[],
        escalated=False,
        escalation_reason="",
        escalation_id="",
        awaiting_human=False,
        human_reply=None,
        turn_count=0,
        unresolved_turns=0,
        dissatisfied_count=0,
        resolved=False,
        needs_clarification=False,
        final_text="",
        request_feedback=False,
        satisfied=None,
        feedback_rating=None,
        feedback_comment=None,
    )
