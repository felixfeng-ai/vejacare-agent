"""订单种子数据。

真实系统里这些数据来自订单服务，工具通过 HTTP 调用它。
演示项目落成本地表，让"查订单/查物流/算退货运费"这三个工具**真的有数据可查**，
而不是返回硬编码的假结果——这是本项目能演示完整闭环的前提。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderEvent

logger = logging.getLogger(__name__)

_BASE = datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc)


def _days_ago(days: int, hours: int = 0) -> datetime:
    return _BASE - timedelta(days=days, hours=hours)


#: 覆盖「运输中 / 清关中 / 已签收 / 待发货 / 退款中 / 已取消」六种状态，
#: 让每种意图都有真实数据可查。
_SEED_ORDERS: list[dict] = [
    {
        "order_no": "SO20260928001",
        "user_email": "emily.chen@example.com",
        "status": "in_transit",
        "sku": "wp2026091",
        "product_name": "法式方领碎花连衣裙",
        "size": "M",
        "amount": 89.90,
        "shipping_fee": 0.0,
        "country": "美国",
        "city": "Los Angeles",
        "tracking_no": "LP00123456789",
        "carrier": "4PX",
        "paid_at": _days_ago(6),
        "shipped_at": _days_ago(5),
        "estimated_delivery": "2026-10-02",
        "events": [
            (5, "深圳", "包裹已出库，等待揽收", "shipped"),
            (5, "深圳", "已揽收，发往广州分拨中心", "in_transit"),
            (4, "广州", "已到达广州国际分拨中心", "in_transit"),
            (3, "广州", "航班已起飞，发往美国", "in_transit"),
            (1, "Los Angeles", "已到达美国洛杉矶分拨中心", "in_transit"),
        ],
    },
    {
        "order_no": "SO20260915007",
        "user_email": "lukas.mueller@example.com",
        "status": "customs",
        "sku": "mn2026044",
        "product_name": "男士羊毛混纺大衣",
        "size": "L",
        "amount": 156.00,
        "shipping_fee": 9.99,
        "country": "德国",
        "city": "Berlin",
        "tracking_no": "LP00987654321",
        "carrier": "DHL eCommerce",
        "paid_at": _days_ago(13),
        "shipped_at": _days_ago(12),
        "estimated_delivery": "2026-10-05",
        "events": [
            (12, "杭州", "包裹已出库", "shipped"),
            (11, "杭州", "已到达杭州国际分拨中心", "in_transit"),
            (9, "法兰克福", "航班已到达德国法兰克福", "in_transit"),
            (4, "Berlin", "到达海关，等待清关审核", "customs"),
            (2, "Berlin", "清关中，请耐心等待", "customs"),
        ],
    },
    {
        "order_no": "SO20260901012",
        "user_email": "sophie.taylor@example.com",
        "status": "delivered",
        "sku": "bg2026077",
        "product_name": "真皮托特包",
        "size": "均码",
        "amount": 45.50,
        "shipping_fee": 0.0,
        "country": "英国",
        "city": "London",
        "tracking_no": "LP00555123456",
        "carrier": "Royal Mail",
        "paid_at": _days_ago(27),
        "shipped_at": _days_ago(26),
        "estimated_delivery": "2026-09-20",
        "events": [
            (26, "广州", "包裹已出库", "shipped"),
            (22, "London", "已到达英国，等待清关", "customs"),
            (19, "London", "清关完成，已交付本地派送", "in_transit"),
            (17, "London", "已签收，签收人：S. TAYLOR", "delivered"),
        ],
    },
    {
        "order_no": "SO20260926003",
        "user_email": "emily.chen@example.com",
        "status": "paid",
        "sku": "sh2026110",
        "product_name": "厚底老爹鞋",
        "size": "38",
        "amount": 32.00,
        "shipping_fee": 2.99,
        "country": "美国",
        "city": "San Francisco",
        "tracking_no": "",
        "carrier": "",
        "paid_at": _days_ago(2),
        "shipped_at": None,
        "estimated_delivery": "2026-10-08",
        "events": [
            (2, "系统", "订单支付成功，等待仓库拣货", "paid"),
        ],
    },
    {
        "order_no": "SO20260910009",
        "user_email": "carlos.silva@example.com",
        "status": "refunding",
        "sku": "wp2026055",
        "product_name": "针织开衫两件套",
        "size": "S",
        "amount": 210.00,
        "shipping_fee": 0.0,
        "country": "巴西",
        "city": "São Paulo",
        "tracking_no": "LP00777888999",
        "carrier": "Correios",
        "paid_at": _days_ago(18),
        "shipped_at": _days_ago(17),
        "estimated_delivery": "2026-10-12",
        "events": [
            (17, "广州", "包裹已出库", "shipped"),
            (15, "São Paulo", "已到达巴西，等待清关", "customs"),
            (8, "São Paulo", "用户申请退货，退款处理中", "refunding"),
        ],
    },
    {
        "order_no": "SO20260820005",
        "user_email": "ahmed.ali@example.com",
        "status": "cancelled",
        "sku": "mn2026002",
        "product_name": "商务免烫衬衫（三件装）",
        "size": "XL",
        "amount": 75.00,
        "shipping_fee": 0.0,
        "country": "沙特",
        "city": "Riyadh",
        "tracking_no": "",
        "carrier": "",
        "paid_at": _days_ago(38),
        "shipped_at": None,
        "estimated_delivery": "",
        "events": [
            (38, "系统", "订单支付成功", "paid"),
            (37, "系统", "超时未支付确认，订单已取消", "cancelled"),
        ],
    },
]


async def seed_orders(session: AsyncSession) -> int:
    """幂等灌入。已有数据就跳过，不会覆盖用户在演示中产生的改动。"""
    existing = await session.scalar(select(func.count()).select_from(Order))
    if existing:
        logger.info("订单表已有 %d 条数据，跳过种子灌入", existing)
        return 0

    for spec in _SEED_ORDERS:
        events = spec.pop("events", [])
        order = Order(**spec)
        for days, location, description, status in events:
            order.events.append(
                OrderEvent(
                    occurred_at=_days_ago(days),
                    location=location,
                    description=description,
                    status=status,
                )
            )
        session.add(order)

    await session.flush()
    logger.info("已灌入 %d 条演示订单", len(_SEED_ORDERS))
    return len(_SEED_ORDERS)
