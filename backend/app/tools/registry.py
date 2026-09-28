"""工具注册表。

工具以 OpenAI function calling 的 JSON Schema 形式声明，同时挂上本地 handler。
新增一个工具只需：写 handler → 在 `_SPECS` 里登记 → 完事，图节点不用改。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.tools import order_tools
from app.tools.order_tools import ToolError

logger = logging.getLogger(__name__)

Handler = Callable[[AsyncSession, dict], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="query_order",
        description=(
            "查询订单详情：状态、商品、尺码、金额、下单时间、目的地。"
            "用户问订单相关问题时先调这个确认订单当前状态。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "订单号，形如 SO20260928001"},
            },
            "required": ["order_no"],
        },
        handler=order_tools.query_order,
    ),
    ToolSpec(
        name="query_logistics",
        description=(
            "查询物流轨迹与预计送达时间。用户问「包裹到哪了」「为什么没更新」时调用。"
            "order_no 与 tracking_no 至少提供一个。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "订单号"},
                "tracking_no": {"type": "string", "description": "物流单号"},
            },
        },
        handler=order_tools.query_logistics,
    ),
    ToolSpec(
        name="calc_refund_fee",
        description=(
            "计算退货运费与预计退款金额。用户问「退货要花多少钱」「能退多少」时调用。"
            "reason 取值：change_of_mind（不喜欢/不想要/尺码不合适）、quality（质量问题）、"
            "wrong_item（发错货）、missing_item（少发货）、damaged（物流破损）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "订单号"},
                "reason": {
                    "type": "string",
                    "enum": [
                        "change_of_mind",
                        "quality",
                        "wrong_item",
                        "missing_item",
                        "damaged",
                        "size_guide_wrong",
                    ],
                    "description": "退货原因",
                },
            },
            "required": ["order_no"],
        },
        handler=order_tools.calc_refund_fee,
    ),
    ToolSpec(
        name="create_ticket",
        description=(
            "创建工单转交专项团队跟进。适用于商品质量问题、少发漏发、破损、"
            "需要承运商查件等无法当场解决、需要后续跟进的场景。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "关联订单号，没有可留空"},
                "category": {
                    "type": "string",
                    "enum": [
                        "quality",
                        "wrong_item",
                        "missing_item",
                        "damaged",
                        "refund_dispute",
                        "logistics",
                        "other",
                    ],
                    "description": "工单类别",
                },
                "description": {"type": "string", "description": "问题描述，包含用户诉求"},
            },
            "required": ["category", "description"],
        },
        handler=order_tools.create_ticket,
    ),
)

_REGISTRY: dict[str, ToolSpec] = {spec.name: spec for spec in _SPECS}


def all_specs() -> list[ToolSpec]:
    return list(_SPECS)


def openai_tools() -> list[dict[str, Any]]:
    """给 LLM function calling 用的工具声明。"""
    return [spec.to_openai_schema() for spec in _SPECS]


def tool_names() -> list[str]:
    return [spec.name for spec in _SPECS]


async def execute(db: AsyncSession, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """执行工具。任何异常都被收敛成 ok=False 的结果，绝不让图崩掉。

    这条约束很重要：工具失败时 LLM 仍然要能回一句「查询失败，我帮您转人工」，
    而不是整个请求 500。
    """
    spec = _REGISTRY.get(name)
    if spec is None:
        logger.warning("模型请求了未注册的工具：%s", name)
        return {
            "ok": False,
            "summary": "系统暂时无法处理该查询",
            "data": {},
            "error": f"未知工具 {name}",
        }

    clean_args = {k: v for k, v in (args or {}).items() if v not in (None, "", [])}

    try:
        result = await spec.handler(db, clean_args)
    except ToolError as exc:
        logger.info("工具 %s 业务失败：%s", name, exc)
        return {"ok": False, "summary": str(exc), "data": {}, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — 工具层兜底，不让异常穿透到图
        logger.exception("工具 %s 执行异常", name)
        return {
            "ok": False,
            "summary": "查询时出现异常，请稍后重试或转人工",
            "data": {},
            "error": str(exc),
        }

    return {**result, "error": None}
