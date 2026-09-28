"""跨模块共用的小工具。"""

from __future__ import annotations

import secrets
import sys
from datetime import datetime, timezone


def setup_console_encoding() -> None:
    """把 stdout/stderr 强制成 UTF-8。

    Windows 控制台默认 GBK，任何 print/logging 里的中文都会抛 UnicodeEncodeError。
    API 进程和 CLI 脚本都要在入口处调一次，否则日志和命令行输出全是乱码（或直接崩）。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — 某些环境下 stdout 不可重配
            pass


def gen_id(prefix: str) -> str:
    """带前缀的短 id。演示项目不追求全局唯一性，够用且可读即可。"""
    return f"{prefix}_{secrets.token_urlsafe(9)}"


def to_iso(value: datetime | None) -> str | None:
    """统一输出 UTC ISO 8601。naive datetime 按 UTC 处理。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
