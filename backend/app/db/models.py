"""数据模型。

六张表：
- orders      模拟的订单主数据（真实系统里这来自订单服务，这里落库方便演示工具调用）
- order_events 物流轨迹
- sessions    会话
- messages    消息（含人工客服回复）
- escalations 转人工工单
- feedbacks   满意度评价
- tickets     用户工单
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ---------------------------------------------------------------- 订单主数据


class Order(TimestampMixin, Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    user_email: Mapped[str] = mapped_column(String(128), default="")

    status: Mapped[str] = mapped_column(String(24), default="paid")
    # paid / shipped / in_transit / customs / delivered / cancelled / refunding / refunded

    sku: Mapped[str] = mapped_column(String(32), default="")
    product_name: Mapped[str] = mapped_column(String(200), default="")
    size: Mapped[str] = mapped_column(String(16), default="")
    quantity: Mapped[int] = mapped_column(Integer, default=1)

    amount: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    shipping_fee: Mapped[float] = mapped_column(Float, default=0.0)

    country: Mapped[str] = mapped_column(String(64), default="")
    city: Mapped[str] = mapped_column(String(64), default="")

    tracking_no: Mapped[str] = mapped_column(String(32), default="", index=True)
    carrier: Mapped[str] = mapped_column(String(32), default="")

    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    estimated_delivery: Mapped[str] = mapped_column(String(16), default="")

    events: Mapped[list["OrderEvent"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )


class OrderEvent(Base):
    __tablename__ = "order_events"
    __table_args__ = (Index("ix_order_events_order_id_ts", "order_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    location: Mapped[str] = mapped_column(String(128), default="")
    description: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(24), default="")

    order: Mapped[Order] = relationship(back_populates="events")


# ---------------------------------------------------------------- 会话


class ChatSession(TimestampMixin, Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(128), default="新的对话")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # active / escalated / closed

    last_intent: Mapped[str] = mapped_column(String(24), default="")
    turn_count: Mapped[int] = mapped_column(Integer, default=0)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="selectin"
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_session_created", "session_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    # user / assistant / human_agent / system

    content: Mapped[str] = mapped_column(Text, default="")
    intent: Mapped[str] = mapped_column(String(24), default="")
    meta_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[ChatSession] = relationship(back_populates="messages")


# ---------------------------------------------------------------- 转人工


class Escalation(TimestampMixin, Base):
    __tablename__ = "escalations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    reason: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending / resolved

    summary: Mapped[str] = mapped_column(Text, default="")
    context_json: Mapped[str] = mapped_column(Text, default="{}")
    human_reply: Mapped[str] = mapped_column(Text, default="")
    agent_name: Mapped[str] = mapped_column(String(64), default="")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------- 满意度


class Feedback(Base):
    __tablename__ = "feedbacks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[str] = mapped_column(String(64), default="")
    rating: Mapped[int] = mapped_column(Integer, default=0)
    comment: Mapped[str] = mapped_column(Text, default="")
    intent: Mapped[str] = mapped_column(String(24), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- 工单


class Ticket(TimestampMixin, Base):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    order_no: Mapped[str] = mapped_column(String(32), default="")
    category: Mapped[str] = mapped_column(String(32), default="general")
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
