"""节点 → 前端的事件通道。

节点不直接碰 SSE，只调 `emit()`；由 langgraph 的 custom stream 把事件带到 API 层。
好处：节点可以脱离 HTTP 单独跑测试（此时 `emit` 静默丢弃事件，不报错）。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def emit(event: dict[str, Any]) -> None:
    """发一个事件。非流式上下文（单元测试直连节点）下静默忽略。"""
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except Exception:  # noqa: BLE001 — 不在图执行上下文中
        return

    if writer is None:
        return

    try:
        writer(event)
    except Exception as exc:  # noqa: BLE001 — 事件发不出去不能影响业务
        logger.debug("事件发送失败 %s: %s", event.get("type"), exc)


def emit_node(node: str, label: str) -> None:
    """节点开始执行的进度事件。"""
    emit({"type": "node", "node": node, "label": label})
