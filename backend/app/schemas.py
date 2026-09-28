"""请求/响应模型与统一响应信封。

信封格式见 docs/API.md：{success, data, error}。
统一出口的好处是前端只需写一次解包逻辑，错误也不会以各种形状漏出去。
"""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ErrorDetail(BaseModel):
    code: str
    message: str


class Envelope(BaseModel, Generic[T]):
    success: bool = True
    data: T | None = None
    error: ErrorDetail | None = None


def ok(data: Any = None) -> dict:
    return {"success": True, "data": data, "error": None}


def fail(code: str, message: str) -> dict:
    return {"success": False, "data": None, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------- 对话


class ChatRequest(BaseModel):
    session_id: str | None = Field(default=None, description="不传则新建会话")
    message: str = Field(min_length=1, max_length=4000)
    locale: str = Field(default="zh-CN", max_length=16)


# ---------------------------------------------------------------- 转人工


class EscalationReplyRequest(BaseModel):
    reply: str = Field(min_length=1, max_length=4000)
    agent: str = Field(default="人工客服", max_length=64)


# ---------------------------------------------------------------- 满意度


class FeedbackRequest(BaseModel):
    session_id: str
    message_id: str = ""
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=1000)


# ---------------------------------------------------------------- 会话


class SessionSummary(BaseModel):
    session_id: str
    title: str
    status: str
    last_intent: str = ""
    message_count: int = 0
    updated_at: str | None = None


class MessageOut(BaseModel):
    id: str
    role: Literal["user", "assistant", "human_agent", "system"]
    content: str
    intent: str = ""
    created_at: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
