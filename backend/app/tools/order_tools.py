"""客服工具的具体实现。

每个 handler 签名统一为 `(db, args) -> {"ok": bool, "summary": str, "data": dict}`：
- `summary` 是给前端展示与 LLM 引用的一句话中文结论
- `data` 是结构化原始结果，回填进 State 供 responder 引用

**所有返回值都来自真实查询**，没有硬编码的假结果——查不到就如实说查不到。
客服场景里编造物流状态是最严重的事故，工具层必须守住这条线。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderEvent, Ticket
from app.utils import gen_id, to_iso

logger = logging.getLogger(__name__)

#: 订单状态 → 中文文案。与 data/knowledge/logistics.md 的状态口径保持一致。
ORDER_STATUS_TEXT: dict[str, str] = {
    "paid": "已支付，等待仓库出库",
    "shipped": "已出库",
    "in_transit": "运输中",
    "customs": "清关中",
    "delivered": "已签收",
    "cancelled": "已取消",
    "refunding": "退款处理中",
    "refunded": "已退款",
}

#: 退货运费价目表。**改动这里必须同步更新 data/knowledge/return-refund.md**，
#: 否则 LLM 依据知识库说出的数字会和工具算出来的对不上。
RETURN_FEE_TABLE: dict[str, tuple[float, str]] = {
    "美国": (4.99, "USD"),
    "英国": (4.50, "EUR"),
    "德国": (4.50, "EUR"),
    "法国": (4.50, "EUR"),
    "西班牙": (5.50, "EUR"),
    "意大利": (5.50, "EUR"),
    "日本": (600.0, "JPY"),
    "沙特": (8.00, "USD"),
    "阿联酋": (8.00, "USD"),
    "巴西": (25.00, "BRL"),
}
RETURN_FEE_DEFAULT: tuple[float, str] = (6.99, "USD")

#: 平台责任的退货原因 → 免运费
PLATFORM_FAULT_REASONS: dict[str, str] = {
    "quality": "商品质量问题",
    "wrong_item": "发错货",
    "missing_item": "少发货",
    "damaged": "物流破损",
    "size_guide_wrong": "平台尺码推荐错误",
}

#: 退款到账时效（工作日），与 return-refund.md 一致
REFUND_CHANNEL_DAYS: dict[str, str] = {
    "credit_card": "3–7 个工作日",
    "paypal": "1–3 个工作日",
    "debit_card": "5–10 个工作日",
    "balance": "即时到账",
}


class ToolError(Exception):
    """工具执行中的可预期错误，会被转成 ok=False 的结果返回给 LLM。"""


async def _find_order(db: AsyncSession, order_no: str | None, tracking_no: str | None) -> Order:
    if order_no:
        order = await db.scalar(select(Order).where(Order.order_no == order_no.upper()))
    elif tracking_no:
        order = await db.scalar(select(Order).where(Order.tracking_no == tracking_no.upper()))
    else:
        raise ToolError("缺少订单号或物流单号")

    if order is None:
        raise ToolError("未找到该订单，请确认订单号是否正确")
    return order


# ---------------------------------------------------------------- query_order


async def query_order(db: AsyncSession, args: dict) -> dict:
    order = await _find_order(db, args.get("order_no"), args.get("tracking_no"))

    data = {
        "order_no": order.order_no,
        "status": order.status,
        "status_text": ORDER_STATUS_TEXT.get(order.status, order.status),
        "product_name": order.product_name,
        "sku": order.sku,
        "size": order.size,
        "quantity": order.quantity,
        "amount": order.amount,
        "currency": order.currency,
        "shipping_fee": order.shipping_fee,
        "country": order.country,
        "city": order.city,
        "paid_at": to_iso(order.paid_at),
        "shipped_at": to_iso(order.shipped_at),
        "estimated_delivery": order.estimated_delivery,
    }

    summary = (
        f"订单 {order.order_no}（{order.product_name}，尺码 {order.size}）"
        f"当前状态：{data['status_text']}，金额 {order.currency} {order.amount:.2f}"
    )
    return {"ok": True, "summary": summary, "data": data}


# ---------------------------------------------------------------- query_logistics


async def query_logistics(db: AsyncSession, args: dict) -> dict:
    order = await _find_order(db, args.get("order_no"), args.get("tracking_no"))

    if not order.tracking_no:
        return {
            "ok": True,
            "summary": (
                f"订单 {order.order_no} 尚未出库，暂无物流单号。"
                f"当前状态：{ORDER_STATUS_TEXT.get(order.status, order.status)}"
            ),
            "data": {
                "order_no": order.order_no,
                "status": order.status,
                "status_text": ORDER_STATUS_TEXT.get(order.status, order.status),
                "tracking_no": None,
                "shipped": False,
            },
        }

    events = sorted(order.events, key=lambda e: e.occurred_at)
    latest = events[-1] if events else None

    data = {
        "order_no": order.order_no,
        "tracking_no": order.tracking_no,
        "carrier": order.carrier,
        "status": order.status,
        "status_text": ORDER_STATUS_TEXT.get(order.status, order.status),
        "latest": latest.description if latest else "",
        "latest_location": latest.location if latest else "",
        "latest_time": to_iso(latest.occurred_at) if latest else None,
        "eta": order.estimated_delivery or None,
        "traces": [
            {
                "time": to_iso(e.occurred_at),
                "location": e.location,
                "description": e.description,
                "status": e.status,
            }
            for e in events[-8:]
        ],
    }

    summary = f"承运商 {order.carrier}，最新轨迹：{data['latest']}（{data['latest_location']}）"
    if order.estimated_delivery:
        summary += f"，预计 {order.estimated_delivery} 送达"

    return {"ok": True, "summary": summary, "data": data}


# ---------------------------------------------------------------- calc_refund_fee


async def calc_refund_fee(db: AsyncSession, args: dict) -> dict:
    order = await _find_order(db, args.get("order_no"), None)
    reason = str(args.get("reason") or "change_of_mind")

    platform_fault = reason in PLATFORM_FAULT_REASONS

    if platform_fault:
        fee, currency = 0.0, order.currency
        fee_text = "平台承担来回运费，您无需支付任何费用"
    else:
        fee, currency = RETURN_FEE_TABLE.get(order.country, RETURN_FEE_DEFAULT)
        fee_text = f"从退款中扣除 {currency} {fee:.2f} 作为退回运费"

    # 未出库的订单直接取消即可，不涉及退货运费
    if order.status in {"paid", "cancelled"}:
        fee, currency = 0.0, order.currency
        fee_text = "订单尚未出库，可直接取消并全额退款，无需寄回商品"

    refund_amount = max(order.amount - fee, 0.0)

    data = {
        "order_no": order.order_no,
        "reason": reason,
        "reason_text": PLATFORM_FAULT_REASONS.get(reason, "个人原因（不喜欢/不想要/尺码不合适）"),
        "platform_fault": platform_fault,
        "country": order.country,
        "return_fee": fee,
        "return_fee_currency": currency,
        "fee_text": fee_text,
        "order_amount": order.amount,
        "estimated_refund": round(refund_amount, 2),
        "currency": order.currency,
        "refund_timeline": "仓库验收 1–3 个工作日 → 平台发起退款 24 小时内 → 银行入账 3–7 个工作日",
        "refund_channels": REFUND_CHANNEL_DAYS,
        "note": "跨境退货整体约 7–15 个工作日到账",
    }

    summary = f"{fee_text}；预计退款 {order.currency} {refund_amount:.2f}"

    return {"ok": True, "summary": summary, "data": data}


# ---------------------------------------------------------------- create_ticket


_TICKET_CATEGORY_TEXT = {
    "quality": "商品质量",
    "wrong_item": "发错货",
    "missing_item": "少发货",
    "damaged": "物流破损",
    "refund_dispute": "退款争议",
    "logistics": "物流查件",
    "other": "其他",
}


async def create_ticket(db: AsyncSession, args: dict) -> dict:
    order_no = (args.get("order_no") or "").upper()
    category = str(args.get("category") or "other")
    description = str(args.get("description") or "")

    if not description.strip():
        raise ToolError("工单描述不能为空")

    if order_no:
        # 有订单号就校验存在性，避免建出一堆查无此单的无效工单
        exists = await db.scalar(select(Order.id).where(Order.order_no == order_no))
        if not exists:
            raise ToolError(f"未找到订单 {order_no}，无法建单")

    ticket = Ticket(
        id=gen_id("tk"),
        session_id=str(args.get("session_id") or ""),
        order_no=order_no,
        category=category,
        description=description[:1000],
    )
    db.add(ticket)
    await db.flush()

    category_text = _TICKET_CATEGORY_TEXT.get(category, "其他")
    data = {
        "ticket_id": ticket.id,
        "order_no": order_no or None,
        "category": category,
        "category_text": category_text,
        "status": "open",
        "description": description[:200],
    }

    if category == "logistics":
        summary = f"已提交物流查件工单 {ticket.id}，承运商将在 3 个工作日内回复"
    else:
        summary = f"已为您创建{category_text}工单 {ticket.id}，专项团队将在 24 小时内跟进"

    return {"ok": True, "summary": summary, "data": data}
